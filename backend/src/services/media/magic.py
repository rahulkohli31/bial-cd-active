"""Shared media allowlist + magic-byte gate — the SINGLE source of truth for the binary media
types an attachment may carry (Express `ALLOWED_MEDIA` / `validateAttachmentBytes`).

Both untrusted entry points apply it so a block the upload path would reject can never slip
through the chat relay unvalidated:
  * `/v1/attachments` (+ office/deck branches) — the upload path.
  * `/v1/claude` inline binaries (`services/agent/content.py`) — the stateless relay; the SPA
    base64-inlines each turn's binaries, and a DIRECT API caller is bypassable, so the server
    re-checks here too (Express validated at BOTH points for exactly this reason).
"""

from __future__ import annotations

# Allowlisted media types → magic-byte prefix. WebP is a RIFF container: the "RIFF" prefix is
# checked here and the "WEBP" form-type at offset 8 separately (see `bytes_match_declared`).
ALLOWED_MEDIA: dict[str, bytes] = {
    "image/png": bytes([0x89, 0x50, 0x4E, 0x47]),
    "image/jpeg": bytes([0xFF, 0xD8]),
    "image/gif": bytes([0x47, 0x49, 0x46, 0x38]),
    "image/webp": bytes([0x52, 0x49, 0x46, 0x46]),
    "application/pdf": bytes([0x25, 0x50, 0x44, 0x46]),
}


def magic_matches(data: bytes, magic: bytes) -> bool:
    """True iff `data` opens with the `magic` prefix."""
    return len(data) >= len(magic) and data[: len(magic)] == magic


def bytes_match_declared(media_type: str, data: bytes) -> bool:
    """True iff `media_type` is allowlisted AND `data` opens with its magic prefix (+ the WebP
    form-type at offset 8). The relay uses this to DROP a block whose declared type is not allowed
    or whose bytes belie it — never forwarding unvalidated content to the model."""
    magic = ALLOWED_MEDIA.get(media_type)
    if magic is None or not magic_matches(data, magic):
        return False
    return not (media_type == "image/webp" and data[8:12] != b"WEBP")
