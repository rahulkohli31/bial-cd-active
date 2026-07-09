"""Per-app parse request/response schemas.

The parse route emits one of two shapes keyed on `kind` — `spreadsheet` (xlsx/xls
AND csv) or `document` (word). They are modeled as a `kind`-discriminated union that
mirrors the exact Express wire dict, every field declared in emit order. The route
returns a pre-built `JSONResponse`, so this `response_model` is DOCUMENTED-ONLY:
FastAPI advertises the per-`kind` shape in OpenAPI but does NOT re-serialize the body.
Enforcement was deliberately dropped because it would reserialize xlsx `timedelta`
cells (float seconds -> ISO-8601 duration) and break byte-identity — locked by
`test_spreadsheet_timedelta_cell_stays_float`. The characterization tests are the guard.
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


# Discriminated on `kind`: documents the per-variant shape in OpenAPI. The route
# returns a JSONResponse, so FastAPI does NOT validate or re-serialize against this.
ParseResponse = Annotated[SpreadsheetParse | DocumentParse, Field(discriminator="kind")]
