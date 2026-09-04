"""Parse governor (U7, R26): the killable subprocess (timeout/OOM → 413) and the bounds
the dispatch runs around the chat office→Markdown extract — zip-bomb pre-filter, OPC
structural gate, and the refusal of kinds the dispatch does not serve.

The extraction itself is covered by `tests/services/extract/test_office.py`; what is pinned
here is that the governor contains it and that the bounds are wired in front of it."""

from __future__ import annotations

import io
import struct
import zipfile

import openpyxl
import pytest

from src.services.extract.zip_safety import FileParseError
from src.services.parse.governor import run_parse
from src.services.parse.parsers import parse_dispatch


def _xlsx(rows: list[list[object]]) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None
    for row in rows:
        ws.append(row)
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def _lying_zip(entries: dict[str, bytes], declared_uncompressed: int) -> bytes:
    """A real archive whose central directory DECLARES `declared_uncompressed` bytes.

    The pre-filter sums the declared sizes and never inflates to check, precisely because
    they are attacker-controllable — so an overstated size is the threat, not a cheat.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    raw = buffer.getvalue()
    cdh = raw.index(b"\x50\x4b\x01\x02")  # PK\x01\x02 — first central-directory header
    return raw[: cdh + 24] + struct.pack("<I", declared_uncompressed) + raw[cdh + 28 :]


# --- dispatch bounds (in-process) ---------------------------------------------


@pytest.mark.parametrize("kind", ["xlsx", "xls", "csv", "word", "pdf"])
def test_retired_and_unknown_kinds_are_415(kind: str) -> None:
    # The structured-row kinds went with the per-app parse endpoint (#37). The dispatch must
    # REFUSE them, not fall through to something that half-works: re-adding a branch for any
    # of them turns this red rather than quietly reviving a surface with no consumer.
    with pytest.raises(FileParseError) as exc:
        parse_dispatch(b"data", kind, "x.bin", None)
    assert exc.value.status == 415
    assert exc.value.code == "UNSUPPORTED_TYPE"


def test_non_office_bytes_rejected_by_the_structure_gate() -> None:
    # Under the EOCD minimum, so the zip pre-filter no-ops and the OPC structural gate is
    # what refuses this. Status and code pin the dispatch's `OfficeExtractError` → clean-400
    # mapping (without it the governor would report a generic 500); the message pins that the
    # GATE refused it, not mammoth choking downstream — which is the same 400 and the same
    # code, so without this line the test passes with the gate deleted.
    with pytest.raises(FileParseError, match="missing ZIP signature") as exc:
        parse_dispatch(b"not a zip at all", "extract_word", "x.docx", None)
    assert exc.value.status == 400
    assert exc.value.code == "INVALID_OFFICE_FILE"


def test_zip_bomb_refused_before_the_extract() -> None:
    # The entry is the one the excel structural gate looks for, so if the pre-filter were
    # dropped this file would reach openpyxl and fail as a 400 — never as a 413.
    bomb = _lying_zip({"xl/workbook.xml": b"<workbook/>"}, 400 * 1024 * 1024)
    with pytest.raises(FileParseError) as exc:
        parse_dispatch(bomb, "extract_excel", "x.xlsx", None)
    assert exc.value.status == 413
    assert exc.value.code == "FILE_TOO_LARGE"


# --- governor (killable subprocess) --------------------------------------------


async def test_governor_parses_in_subprocess() -> None:
    # The live chat path: openpyxl runs in the spawned child and the Markdown payload
    # crosses the Queue intact.
    data = _xlsx([["Name"], ["Alice"]])
    result = await run_parse(data, "extract_excel", "x.xlsx", None)
    assert result["format"] == "excel"
    assert "Alice" in result["text"]
    assert result["truncated"] is False


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
