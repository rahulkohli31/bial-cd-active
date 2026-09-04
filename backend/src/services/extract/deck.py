"""Deck (pptx) → PDF conversion via a Gotenberg sidecar (R17; ports `deck-convert.js`).

A deck is a VISUAL medium, so it is NOT text-extracted — it is rendered to a PDF (LibreOffice via
Gotenberg) that the model reads as vision.

THE BOUNDS SIT ON BOTH SIDES OF THE RENDER, and it matters which is which. Structure and the
zip-bomb pre-filter run BEFORE any network call, so a malformed or inflating file is refused
without touching the renderer. The output-size and PAGE caps can only run after — the page count
is read off the RENDERED PDF (`count_pdf_pages`), there being nothing cheap to count in the
pptx — so a 400-page deck is rejected having already cost one full render. That is the accepted
trade, not an oversight, and this paragraph exists because the sentence it replaces claimed the
page cap ran up front and that an oversized deck cost no render.

Every failure is a typed `DeckConvertError(status, code)` with a user-safe message (never mentions
"PDF"/"convert"). Deck attachments are gated on a configured `GOTENBERG_URL` — unset = disabled.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx

from src.config import settings
from src.services.extract.office import (
    PPTX_MEDIA_TYPE,
    OfficeExtractError,
    assert_office_structure,
)
from src.services.extract.zip_safety import FileParseError, assert_zip_not_bomb

# Defaults (Express `deck-config.js`). A deck of >100 pages is rejected (well under the
# Files-API PDF limit); the rendered PDF is capped at 50 MB; the render has a 60 s budget.
DEFAULT_MAX_DECK_PAGES = 100
MAX_PDF_BYTES = 50 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 60.0

_PDF_MAGIC = b"%PDF"
_WORD_CHARS = set(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_")


class DeckConvertError(Exception):
    """A deck that could not be converted. `status`/`code` map to the HTTP response."""

    def __init__(
        self, message: str, *, status: int = 400, code: str = "DECK_CONVERT_ERROR"
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


@dataclass(frozen=True)
class DeckResult:
    pdf: bytes
    page_count: int
    media_type: str = "application/pdf"


def gotenberg_url() -> str:
    return (settings.GOTENBERG_URL or "").strip().rstrip("/")


def deck_attachments_enabled() -> bool:
    """Deck uploads are available only when a Gotenberg sidecar is configured."""
    return bool(gotenberg_url())


def count_pdf_pages(pdf: bytes) -> int:
    """Count `/Type /Page` markers (excluding the `/Type /Pages` page-tree root), a raw-byte scan
    reliable for LibreOffice/Gotenberg output (uncompressed page dicts). Mirrors Express."""
    count = 0
    for needle in (b"/Type /Page", b"/Type/Page"):
        start = 0
        while (hit := pdf.find(needle, start)) != -1:
            after = hit + len(needle)
            if after >= len(pdf) or pdf[after] not in _WORD_CHARS:
                count += 1
            start = after
    return count


def _safe_pptx_name(name: str) -> str:
    # Gotenberg routes by extension, so the filename MUST end .pptx.
    base = re.sub(r"\.pptx$", "", name, flags=re.IGNORECASE)
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base)
    return f"{base}.pptx" if base else "deck.pptx"


async def _render_with_gotenberg(
    data: bytes, url: str, name: str, timeout_seconds: float
) -> bytes:
    """POST the pptx to Gotenberg's LibreOffice route and return the rendered PDF bytes."""
    files = {"files": (_safe_pptx_name(name), data, PPTX_MEDIA_TYPE)}
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.post(f"{url}/forms/libreoffice/convert", files=files)
    except httpx.TimeoutException as exc:
        raise DeckConvertError(
            "This deck took too long to process. Try a smaller deck.",
            status=504,
            code="RENDERER_TIMEOUT",
        ) from exc
    except httpx.HTTPError as exc:
        raise DeckConvertError(
            "Couldn't process the deck. Please try again.", status=502, code="RENDERER_UNAVAILABLE"
        ) from exc
    if response.status_code != 200:
        raise DeckConvertError(
            "Couldn't process the deck. Please try again.", status=502, code="RENDERER_UNAVAILABLE"
        )
    return response.content


async def convert_deck_to_pdf(
    data: bytes,
    *,
    name: str = "",
    url: str | None = None,
    max_pages: int = DEFAULT_MAX_DECK_PAGES,
    max_pdf_bytes: int = MAX_PDF_BYTES,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    renderer: Callable[[bytes, str, str, float], Awaitable[bytes]] = _render_with_gotenberg,
) -> DeckResult:
    """Convert a pptx to a bounded PDF. Validates structure + zip-bomb before rendering; enforces
    the output-size and page-count caps after. `renderer` is injectable for tests."""
    # 1. Structure (before any network call).
    try:
        assert_office_structure(data, "powerpoint")
    except OfficeExtractError as exc:
        raise DeckConvertError(str(exc), status=415, code="UNSUPPORTED_DECK") from exc
    # 2. Zip-bomb pre-filter.
    try:
        assert_zip_not_bomb(data)
    except FileParseError as exc:
        raise DeckConvertError(
            str(exc), status=exc.status, code=exc.code or "DECK_TOO_LARGE"
        ) from exc
    # 3. Renderer must be configured.
    resolved_url = url if url is not None else gotenberg_url()
    if not resolved_url:
        raise DeckConvertError(
            "PowerPoint attachments aren't available right now.",
            status=503,
            code="RENDERER_UNCONFIGURED",
        )
    # 4. Render.
    pdf = await renderer(data, resolved_url, name, timeout_seconds)
    # 5. Output caps.
    if len(pdf) > max_pdf_bytes:
        raise DeckConvertError(
            "This deck is too large to process. Try a smaller deck.",
            status=413,
            code="RENDERER_OUTPUT_TOO_LARGE",
        )
    if pdf[:4] != _PDF_MAGIC:
        raise DeckConvertError(
            "We couldn't process this deck. Please try re-saving it as a .pptx.",
            status=502,
            code="RENDERER_BAD_OUTPUT",
        )
    pages = count_pdf_pages(pdf)
    if pages > max_pages:
        raise DeckConvertError(
            f"This deck is {pages} pages, over the {max_pages}-page limit. "
            "Please split it or upload a smaller deck.",
            status=413,
            code="TOO_MANY_PAGES",
        )
    return DeckResult(pdf=pdf, page_count=max(1, pages))
