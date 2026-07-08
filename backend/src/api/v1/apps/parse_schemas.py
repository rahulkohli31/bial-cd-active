"""Per-app parse request/response schemas.

The parse route emits one of two shapes keyed on `kind` — `spreadsheet` (xlsx/xls
AND csv) or `document` (word). They are modeled as a `kind`-discriminated union so
the enforced `response_model` reproduces the exact Express wire dict: every field is
declared in emit order and always present, so the output stays byte-identical with
no `exclude_none` needed (a superset-with-optionals model could not preserve order).
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from pydantic import Field

from src.schemas import CamelModel


class ParseRequest(CamelModel):
    file_id: uuid.UUID | None = None
    filename: str | None = None
    content_type: str | None = None
    base64: str | None = None
    sheet: str | None = None


class SpreadsheetParse(CamelModel):
    """xlsx / xls / csv output — keys in Express emit order."""

    kind: Literal["spreadsheet"]
    sheets: list[str]
    sheet: str
    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int
    total_rows: int
    truncated: bool
    truncation_note: str


class DocumentParse(CamelModel):
    """word output — keys in Express emit order."""

    kind: Literal["document"]
    format: str
    text: str
    truncated: bool
    truncation_note: str


# Discriminated on `kind`: FastAPI picks the variant, validates, and serializes in
# that variant's field order — byte-identical to the hand-built dict.
ParseResponse = Annotated[SpreadsheetParse | DocumentParse, Field(discriminator="kind")]
