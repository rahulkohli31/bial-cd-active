"""Multimodal content assembly (R12) — Anthropic-shaped content blocks → Pydantic AI content.

The SPA assembles each turn CLIENT-SIDE into Anthropic `content` (a bare string, or a list of
content blocks with the newest turn's binaries already base64-inlined) and sends it in the
`/v1/claude` body (stateless-relay contract). This maps one message's content to the Pydantic AI
content Foundry consumes:

* `{type:'text', text}`                      → `str`  (covers inline text AND office-as-markdown,
                                                        which the SPA sends as a fenced text block)
* `{type:'image', source:{base64,media}}`    → `BinaryContent(image/*)`
* `{type:'document', source:{base64,pdf}}`   → `BinaryContent(application/pdf)`  (PDFs + decks)
* `{type:'document', source:{type:'file'}}`  → skipped — under Azure-hosted Foundry there is no
                                                Files API, so this shape is never produced.

Bytes arrive already-inlined from the SPA; this never touches the object store. Malformed blocks
are skipped rather than raising — a single bad block must not abort a turn.
"""

from __future__ import annotations

import base64
import binascii
from typing import Any

from pydantic_ai import BinaryContent

from src.services.media.magic import bytes_match_declared


def to_model_content(content: Any) -> str | list[str | BinaryContent]:
    """Convert one Anthropic message `content` to Pydantic AI content. A bare string passes
    through; a block list becomes an ordered `[str | BinaryContent]` sequence. Any other shape
    (None, number, …) → empty string — this is an untrusted-input boundary, so never raise."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str | BinaryContent] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
        elif block_type in ("image", "document"):
            binary = _binary_from_block(block)
            if binary is not None:
                parts.append(binary)
    return parts


def _binary_from_block(block: dict[str, Any]) -> BinaryContent | None:
    """A base64 image/document block → `BinaryContent`, or None (file-id refs / malformed)."""
    source = block.get("source")
    if not isinstance(source, dict) or source.get("type") != "base64":
        return None
    data = source.get("data")
    media_type = source.get("media_type")
    if not isinstance(data, str) or not isinstance(media_type, str):
        return None
    try:
        raw = base64.b64decode(data, validate=False)
    except binascii.Error, ValueError:
        return None
    # Server-side allowlist + magic-byte gate. The client's declared `media_type` is untrusted
    # (Express ran `validateAttachmentBytes` on the relay too, since client checks are bypassable):
    # DROP any block whose type isn't allowlisted or whose bytes don't match it, rather than
    # forwarding unvalidated content to the model. Same allowlist as the `/v1/attachments` upload.
    if not bytes_match_declared(media_type, raw):
        return None
    return BinaryContent(data=raw, media_type=media_type)
