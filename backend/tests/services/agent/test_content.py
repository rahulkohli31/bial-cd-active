"""Multimodal content assembly — Anthropic content blocks → Pydantic AI content (U12)."""

from __future__ import annotations

import base64

from pydantic_ai import BinaryContent

from src.services.agent.content import to_model_content

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
_PDF = b"%PDF-1.4\n"


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def test_string_content_passes_through() -> None:
    assert to_model_content("just text") == "just text"


def test_text_blocks_become_strings() -> None:
    out = to_model_content([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}])
    assert out == ["a", "b"]


def test_image_block_becomes_binary_content() -> None:
    out = to_model_content(
        [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": _b64(_PNG)},
            }
        ]
    )
    assert len(out) == 1
    part = out[0]
    assert isinstance(part, BinaryContent)
    assert part.media_type == "image/png"
    assert part.data == _PNG


def test_document_block_becomes_pdf_binary() -> None:
    out = to_model_content(
        [
            {
                "type": "document",
                "source": {"type": "base64", "media_type": "application/pdf", "data": _b64(_PDF)},
            }
        ]
    )
    assert len(out) == 1
    assert isinstance(out[0], BinaryContent)
    assert out[0].media_type == "application/pdf"
    assert out[0].data == _PDF


def test_file_id_document_is_skipped() -> None:
    # Files-API (file_id) refs are never produced under Azure-hosted Foundry — skip them.
    out = to_model_content(
        [{"type": "document", "source": {"type": "file", "file_id": "file_abc"}}]
    )
    assert out == []


def test_mixed_blocks_preserve_order_and_types() -> None:
    out = to_model_content(
        [
            {"type": "text", "text": "look:"},
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": _b64(_PNG)},
            },
        ]
    )
    assert out[0] == "look:"
    assert isinstance(out[1], BinaryContent)


def test_malformed_blocks_are_skipped() -> None:
    out = to_model_content(
        [
            "not-a-dict",
            {"type": "image", "source": {"type": "base64"}},  # missing data/media_type
            {"type": "text", "text": "kept"},
        ]
    )
    assert out == ["kept"]


# --- relay allowlist + magic-byte gate (Express `validateAttachmentBytes` ran here too) --------


def test_disallowed_media_type_is_dropped() -> None:
    # A type not on the shared allowlist is refused at the relay boundary — the client's declared
    # media_type is untrusted (bypassable), so it is never forwarded to the model.
    out = to_model_content(
        [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "text/html", "data": _b64(b"<script>")},
            }
        ]
    )
    assert out == []


def test_magic_mismatch_is_dropped() -> None:
    # Declared image/png but the bytes are a PDF → the magic-byte gate drops the block.
    out = to_model_content(
        [
            {
                "type": "document",
                "source": {"type": "base64", "media_type": "image/png", "data": _b64(_PDF)},
            }
        ]
    )
    assert out == []


def test_bad_webp_form_type_is_dropped() -> None:
    # RIFF header but not "WEBP" at offset 8 → dropped (the WebP form-type check).
    riff_not_webp = b"RIFF\x00\x00\x00\x00AVI \x00\x00\x00\x00"
    out = to_model_content(
        [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/webp",
                    "data": _b64(riff_not_webp),
                },
            }
        ]
    )
    assert out == []
