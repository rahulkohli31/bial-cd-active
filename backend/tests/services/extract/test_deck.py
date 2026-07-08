"""Deck (pptx) → PDF conversion (U11) — the validate-before-render pipeline + caps.

The Gotenberg HTTP call is exercised only in the `integration`-marked test (needs a sidecar);
the pipeline logic is unit-tested with an injected renderer.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from src.services.extract.deck import (
    DeckConvertError,
    convert_deck_to_pdf,
    count_pdf_pages,
    deck_attachments_enabled,
)

_GOTENBERG = "http://gotenberg:3000"


def _pptx() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("ppt/presentation.xml", "<presentation/>")
    return buffer.getvalue()


def _fake_pdf(pages: int = 1) -> bytes:
    return b"%PDF-1.4\n" + b"/Type /Page\n" * pages + b"%%EOF"


def _renderer_returning(payload: bytes):
    async def _render(data: bytes, url: str, name: str, timeout_seconds: float) -> bytes:
        return payload

    return _render


# --- count_pdf_pages ----------------------------------------------------------


def test_count_pdf_pages_ignores_page_tree_root() -> None:
    pdf = b"%PDF /Type /Page x /Type /Pages y /Type/Page z"
    assert count_pdf_pages(pdf) == 2  # two /Type /Page, the /Type /Pages root excluded


# --- pipeline (injected renderer) ---------------------------------------------


async def test_valid_deck_converts() -> None:
    result = await convert_deck_to_pdf(
        _pptx(), url=_GOTENBERG, renderer=_renderer_returning(_fake_pdf(pages=3))
    )
    assert result.media_type == "application/pdf"
    assert result.page_count == 3


async def test_non_pptx_rejected_before_render() -> None:
    with pytest.raises(DeckConvertError) as excinfo:
        await convert_deck_to_pdf(b"not a zip", url=_GOTENBERG, renderer=_renderer_returning(b""))
    assert excinfo.value.status == 415
    assert excinfo.value.code == "UNSUPPORTED_DECK"


async def test_unconfigured_renderer_rejected() -> None:
    # A valid deck but no Gotenberg URL → 503 (structure/zip pass, then the URL check).
    with pytest.raises(DeckConvertError) as excinfo:
        await convert_deck_to_pdf(_pptx(), url="", renderer=_renderer_returning(_fake_pdf()))
    assert excinfo.value.status == 503
    assert excinfo.value.code == "RENDERER_UNCONFIGURED"


async def test_non_pdf_output_rejected() -> None:
    with pytest.raises(DeckConvertError) as excinfo:
        await convert_deck_to_pdf(
            _pptx(), url=_GOTENBERG, renderer=_renderer_returning(b"not a pdf")
        )
    assert excinfo.value.status == 502
    assert excinfo.value.code == "RENDERER_BAD_OUTPUT"


async def test_too_many_pages_rejected() -> None:
    with pytest.raises(DeckConvertError) as excinfo:
        await convert_deck_to_pdf(
            _pptx(), url=_GOTENBERG, max_pages=1, renderer=_renderer_returning(_fake_pdf(pages=5))
        )
    assert excinfo.value.status == 413
    assert excinfo.value.code == "TOO_MANY_PAGES"


def test_deck_disabled_without_gotenberg_url() -> None:
    # GOTENBERG_URL is unset in the test env → deck attachments are off by default.
    assert deck_attachments_enabled() is False
