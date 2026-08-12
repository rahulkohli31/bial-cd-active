"""Parse governor + parser logic (U7, R26): killable subprocess (timeout/OOM → 413),
the four bounds, and structured-row parsing (merged-banner skip, merged-cell anchor,
range-clamp, date/number coercion)."""

from __future__ import annotations

import datetime
import io

import openpyxl
import pytest

from src.services.extract.zip_safety import FileParseError
from src.services.parse import parsers
from src.services.parse.governor import run_parse
from src.services.parse.parsers import parse_dispatch


def _xlsx(rows: list[list[object]], merges: list[str] | None = None) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None
    for row in rows:
        ws.append(row)
    for merge in merges or []:
        ws.merge_cells(merge)
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


# --- parser logic (in-process) -------------------------------------------------


def test_spreadsheet_parses_to_rows() -> None:
    data = _xlsx([["Name", "Age"], ["Alice", 30], ["Bob", 25]])
    result = parse_dispatch(data, "xlsx", "x.xlsx", None)
    assert result["kind"] == "spreadsheet"
    assert result["columns"] == ["Name", "Age"]
    assert result["rows"] == [{"Name": "Alice", "Age": 30}, {"Name": "Bob", "Age": 25}]
    assert result["totalRows"] == 2


def test_full_width_banner_row_skipped() -> None:
    # A leading full-width merged title must not become the column names.
    data = _xlsx([["Sales Q1"], ["Name", "Age"], ["Alice", 30]], merges=["A1:B1"])
    result = parse_dispatch(data, "xlsx", "x.xlsx", None)
    assert result["columns"] == ["Name", "Age"]
    assert result["rows"] == [{"Name": "Alice", "Age": 30}]


def test_merged_cell_resolves_to_anchor_value() -> None:
    # A horizontal merge fills every covered column with the anchor value.
    data = _xlsx([["Name", "A", "B"], ["Alice", "shared", ""]], merges=["B2:C2"])
    result = parse_dispatch(data, "xlsx", "x.xlsx", None)
    assert result["rows"][0]["A"] == "shared"
    assert result["rows"][0]["B"] == "shared"


def test_dates_are_iso_and_numbers_numeric() -> None:
    data = _xlsx([["When", "Count"], [datetime.datetime(2026, 7, 6, 12, 0), 42]])
    row = parse_dispatch(data, "xlsx", "x.xlsx", None)["rows"][0]
    assert row["When"].startswith("2026-07-06T12:00")
    assert row["Count"] == 42 and isinstance(row["Count"], int)


def test_row_clamp_before_iteration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(parsers, "MAX_PARSE_ROWS", 2)
    data = _xlsx([["N"], [1], [2], [3], [4], [5]])
    result = parse_dispatch(data, "xlsx", "x.xlsx", None)
    assert result["rowCount"] == 2
    assert result["totalRows"] == 5
    assert result["truncated"] is True


def test_csv_type_inference() -> None:
    data = b"name,age\nAlice,30\nBob,25\n"
    result = parse_dispatch(data, "csv", "x.csv", None)
    assert result["rows"] == [{"name": "Alice", "age": 30}, {"name": "Bob", "age": 25}]


def test_duplicate_columns_deduped() -> None:
    data = _xlsx([["N", "N"], ["a", "b"]])
    result = parse_dispatch(data, "xlsx", "x.xlsx", None)
    assert result["columns"] == ["N", "N (1)"]


def test_unsupported_kind_is_415() -> None:
    with pytest.raises(FileParseError) as exc:
        parse_dispatch(b"data", "pdf", "x.pdf", None)
    assert exc.value.status == 415
    assert exc.value.code == "UNSUPPORTED_TYPE"


def test_non_office_bytes_as_xlsx_rejected() -> None:
    with pytest.raises(FileParseError) as exc:
        parse_dispatch(b"not a zip at all", "xlsx", "x.xlsx", None)
    assert exc.value.status == 400


# --- governor (killable subprocess) --------------------------------------------


async def test_governor_parses_in_subprocess() -> None:
    data = _xlsx([["Name"], ["Alice"]])
    result = await run_parse(data, "xlsx", "x.xlsx", None)
    assert result["rows"] == [{"Name": "Alice"}]


async def test_governor_timeout_is_413() -> None:
    with pytest.raises(FileParseError) as exc:
        await run_parse(b"", "__test_sleep", "", None, timeout=1.0)
    assert exc.value.status == 413
    assert exc.value.code == "PARSE_TIMEOUT"


async def test_governor_contained_crash_is_413() -> None:
    # A hard OS-level kill (simulated OOM) is contained → 413, never a server crash.
    with pytest.raises(FileParseError) as exc:
        await run_parse(b"", "__test_crash", "", None, timeout=3.0)
    assert exc.value.status == 413
    assert exc.value.code == "FILE_TOO_LARGE"
