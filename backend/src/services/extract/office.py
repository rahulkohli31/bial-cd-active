"""Office → Markdown extraction (R17; ports `server/office-extract.js`).

docx and xlsx are stored as-is but NEVER inlined to the model — the server extracts them to
Markdown (a sticky text block re-sent every turn), so the payload is bounded (text cap) and
truncate-and-warn (never reject on content). Structure is validated by ZIP signature + OPC part
BEFORE any parse (a mislabelled `.zip`/`.pptx` is a clean 400).

These functions are pure extractors: openpyxl read-only streaming + a row cap keep a *legit*
sheet bounded, but they do NOT contain a decompression bomb by themselves. The A.U11 review
invariant — untrusted docx/xlsx must not OOM the shared API worker — is met by the CALLER
running `extract_office` inside the shared killable parse governor (`services/parse/governor.py`,
via the `extract_word`/`extract_excel` dispatch kinds), which adds the `assert_zip_not_bomb`
pre-filter, an rlimit, and a wall-clock kill (contained OOM/timeout → 413). Do not call
`extract_office` in-process on untrusted bytes.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any, Literal

import mammoth
import openpyxl
from markdownify import markdownify

from src.services.extract.zip_safety import looks_like_zip

WORD_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
EXCEL_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PPTX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
OFFICE_MEDIA_TYPES = frozenset({WORD_MEDIA_TYPE, EXCEL_MEDIA_TYPE})

# Per-file extracted-text cap (~100 KB ≈ 28k tokens) and per-sheet data-row cap (Express).
MAX_OFFICE_TEXT_CHARS = 100 * 1024
MAX_SHEET_ROWS = 1000

OfficeFormat = Literal["word", "excel", "powerpoint"]

# The OPC part that must be present (uncompressed in the ZIP headers) for each format —
# the discriminator, since all OOXML share the ZIP signature.
_OPC_PART: dict[OfficeFormat, bytes] = {
    "word": b"word/document.xml",
    "excel": b"xl/workbook.xml",
    "powerpoint": b"ppt/presentation.xml",
}
_FORMAT_LABEL: dict[OfficeFormat, str] = {
    "word": "Word",
    "excel": "Excel",
    "powerpoint": "PowerPoint",
}
_TRUNCATION_MARKER = "\n\n> content truncated"


class OfficeExtractError(Exception):
    """A non-Office / corrupt / unreadable file. The route maps it to 400."""


@dataclass(frozen=True)
class ExtractResult:
    format: OfficeFormat
    text: str
    truncated: bool
    truncation_note: str


def office_format_for(media_type: str) -> OfficeFormat | None:
    if media_type == WORD_MEDIA_TYPE:
        return "word"
    if media_type == EXCEL_MEDIA_TYPE:
        return "excel"
    return None


def assert_office_structure(data: bytes, office_format: OfficeFormat) -> None:
    """Fail fast unless `data` is a ZIP carrying the format-specific OPC part."""
    if not looks_like_zip(data):
        raise OfficeExtractError("Not a valid Office file (missing ZIP signature).")
    if _OPC_PART[office_format] not in data:
        raise OfficeExtractError(f"Not a valid {_FORMAT_LABEL[office_format]} file.")


def extract_office(data: bytes, media_type: str, name: str = "") -> ExtractResult:
    """Extract a docx/xlsx to Markdown. Structure-validate, extract, then cap the text."""
    office_format = office_format_for(media_type)
    if office_format is None:
        raise OfficeExtractError(f"Unsupported Office type: {media_type}")
    assert_office_structure(data, office_format)

    if office_format == "word":
        raw, sheet_truncated, note = _extract_word(data), False, ""
    else:
        raw, sheet_truncated, note = _extract_excel(data)

    if not raw:
        raw = f"_(no extractable text in {name or 'the file'})_"

    text, text_truncated = _apply_text_cap(raw)
    return ExtractResult(
        format=office_format,
        text=text,
        truncated=sheet_truncated or text_truncated,
        truncation_note=_truncation_note(sheet_truncated, text_truncated, note),
    )


def _extract_word(data: bytes) -> str:
    try:
        html = mammoth.convert_to_html(io.BytesIO(data)).value
    except Exception as exc:  # mammoth raises assorted errors on a bad docx
        raise OfficeExtractError(f"Could not read the Word document: {exc}") from exc
    markdown: str = markdownify(html, heading_style="ATX")
    return markdown.strip()


def _cell_text(value: Any) -> str:
    # openpyxl (data_only) yields the cached value — numbers/dates as typed values, never a raw
    # serial. Newlines flattened, pipes escaped so a cell can't break the Markdown table.
    if value is None:
        return ""
    return str(value).replace("\r\n", " ").replace("\n", " ").replace("|", "\\|").strip()


def _extract_excel(data: bytes) -> tuple[str, bool, str]:
    try:
        workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        raise OfficeExtractError(f"Could not read the Excel workbook: {exc}") from exc
    sections: list[str] = []
    any_truncated = False
    truncated_sheets: list[str] = []
    for sheet in workbook.worksheets:
        table, truncated = _sheet_to_markdown(sheet)
        if truncated:
            any_truncated = True
            truncated_sheets.append(sheet.title)
        sections.append(f"## Sheet: {sheet.title}\n\n{table}")
    workbook.close()
    note = ""
    if truncated_sheets:
        joined = ", ".join(f'"{name}"' for name in truncated_sheets)
        note = f"Large sheet(s) were shortened for the AI: {joined} (first {MAX_SHEET_ROWS} rows)."
    return "\n\n".join(sections).strip(), any_truncated, note


def _sheet_to_markdown(sheet: Any) -> tuple[str, bool]:
    # Collect header + up to MAX_SHEET_ROWS data rows, bounded (read-only streaming stops at
    # actual rows; the cap bounds a genuinely huge sheet).
    collected: list[tuple[Any, ...]] = []
    truncated = False
    for index, row in enumerate(sheet.iter_rows(values_only=True)):
        if index > MAX_SHEET_ROWS:  # header + MAX_SHEET_ROWS data rows already collected
            truncated = True
            break
        collected.append(row)
    if not collected or all(all(cell is None for cell in row) for row in collected):
        return "_(empty sheet)_", False

    header = collected[0]
    width = len(header) or 1
    lines = [_row_md(header, width), "| " + " | ".join("---" for _ in range(width)) + " |"]
    lines.extend(_row_md(row, width) for row in collected[1:])
    table = "\n".join(lines)
    if truncated:
        table += f"\n\n> showing first {MAX_SHEET_ROWS} rows"
    return table, truncated


def _row_md(row: tuple[Any, ...], width: int) -> str:
    cells = [_cell_text(row[i]) if i < len(row) else "" for i in range(width)]
    return "| " + " | ".join(cells) + " |"


def _apply_text_cap(text: str) -> tuple[str, bool]:
    if len(text) <= MAX_OFFICE_TEXT_CHARS:
        return text, False
    keep = MAX_OFFICE_TEXT_CHARS - len(_TRUNCATION_MARKER)
    return text[:keep] + _TRUNCATION_MARKER, True


def _truncation_note(sheet_truncated: bool, text_truncated: bool, sheet_note: str) -> str:
    if not sheet_truncated and not text_truncated:
        return ""
    parts: list[str] = []
    if sheet_note:
        parts.append(sheet_note)
    if text_truncated:
        parts.append(
            "This file is very long, so only the first ~100 KB of text was sent to the AI."
        )
    parts.append("The AI saw a shortened version; the original file you download is complete.")
    return " ".join(parts)[:800]
