#!/usr/bin/env python3
"""Report what an attached file CONTAINS, without sending the file to the model.

One return shape, always: a single JSON object and exit 0 — a manifest on success, a named
failure on any other outcome. Never a stack trace, never a bare exception, and never an empty
manifest, which reads exactly like an empty file. Every truncated list carries the true count
beside it, because silence about what was omitted is the failure being replaced. It opens one
path on local disk and reaches no network. Editing it is expected — a Build agent may change
and re-run it — so the canonical copy lives outside the editable tree and can be restored.

WHY THIS EXISTS

The platform used to flatten a workbook to Markdown on the server, keep the first thousand rows,
and say nothing about the rest: a question about a 5,000-row file was answered from a fifth of
it, confidently and wrongly. Letting an agent write a parser per turn reproduces that — our own
from-scratch reader was wrong on its first run, and five parsers given one crafted file
disagreed on its row count by five orders of magnitude. This is known-correct code the agent
starts FROM rather than a capability it has to invent under time pressure.
"""

from __future__ import annotations

import codecs
import json
import os
import re
import signal
import sys
import tempfile

# BOUND THE PARALLELISM BEFORE polars IS IMPORTED, which is what makes the memory ceiling below
# mean anything. `RLIMIT_AS` counts VIRTUAL address space, and polars reserves a stack per worker
# thread up front: measured in the sandbox image, a 512 MB ceiling cannot even spawn its default
# pool — the process aborts with a thread-pool error before reading a byte, which reads as a
# broken container rather than a refused file. Two threads is ample for describing one file
# (200,000 rows well inside the limit) and keeps the reservation small enough that the ceiling
# bounds real usage instead of the allocator's bookkeeping.
#
# Set here rather than in `_apply_bounds` because polars reads it at IMPORT, and the import
# happens inside the reader function.
os.environ.setdefault("POLARS_MAX_THREADS", "2")
from pathlib import Path
from typing import Any

# The bounds. A read that cannot finish inside these is a NAMED FAILURE like any other, never a
# hang and never a container the platform has to notice is wedged. The server-side parser this
# replaces ran inside a killable, memory-capped subprocess; moving the parsing into the sandbox
# must not lose that, and neither `governor.py`'s rlimit nor its deadline reaches in here.
TIME_LIMIT_SECONDS = 30
# HELD AT 512 MB ACROSS THE 4 MB -> 10 MB CAP RISE, and that is a measurement rather than an
# oversight. The number used to be justified by "room for the object model a parser builds", which
# made it a function of the door's cap; it is not one any more, because no reader here builds a
# whole-file object model. Measured in this image on the worst case the door now admits — a dense
# 9.99 MB workbook, 23,000 x 40 = 920,000 populated cells, in a 1 vCPU / 2 GiB container:
#
#     materialising (`read_only=False`, two loads)  829 MB peak, 9.4s  -> REFUSED at this ceiling
#     streaming     (`read_only=True`,  two passes)  46 MB peak, 5.5s  -> reads, ~10x headroom
#
# So the ceiling stays and the reader was fixed instead. Raising it would have been the wrong
# lever twice over: the container holds `next dev` as well, and 512 MB of headroom there is worth
# more than a spreadsheet nobody could read anyway. If a future format genuinely needs more,
# measure it the same way before moving this — a bound raised on argument rather than on evidence
# trades a clean "attach a smaller file" for the OOM killer taking the citizen's live preview.
#
# BOUNDED WITH `RLIMIT_DATA`, NOT `RLIMIT_AS`, and the difference is the whole reason this is
# commented. `RLIMIT_AS` caps VIRTUAL address space, which for a Rust allocator with a worker
# pool is mostly reservation rather than memory in use: measured in this image, a 512 MB
# `RLIMIT_AS` aborts polars before it reads a byte, and the process dies with an allocator panic
# that reads as a broken container rather than a refused file. `RLIMIT_DATA` bounds the data the
# process actually asks for, so polars runs and an oversized read raises `MemoryError` on the
# paths that allocate through Python.
#
# IT IS NOT A GUARANTEE ON EVERY PATH, and saying otherwise hid a real failure. polars
# allocates through Rust, whose allocator ABORTS when it cannot satisfy a request rather than
# raising anything Python can catch: the process dies on a signal with an empty stdout, which
# breaks the one-object-and-exit-0 contract this reader otherwise keeps. Nothing in here can
# catch it, so the caller names it — see `attachment_tools.read`.
MEMORY_LIMIT_BYTES = 512 * 1024 * 1024

# How much of any unbounded list is shown. The true total always rides beside it.
MAX_ITEMS = 50
MAX_SAMPLE_ROWS = 5
# Cell text is summarised, never dumped: this reports the SHAPE of a file so an app can be built
# to it, and a manifest that inlined every cell would be the flattening that was removed.
MAX_TEXT_CHARS = 300


# N818 asks for an `Error` suffix. The name is deliberate and the distinction is the design:
# this script EXITS 0 and prints a named failure, because a non-zero exit reads to the agent as
# "the command broke" and invites a retry or a hand-written parser (see `main`). Calling it
# `ReadError` would describe the thing the design exists to avoid being.
class ReadFailure(Exception):  # noqa: N818
    """A failure the citizen can be told about, with something they can do next."""

    def __init__(self, code: str, message: str, next_step: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.next_step = next_step


def _clip(text: Any, limit: int = MAX_TEXT_CHARS) -> str:
    """One cell or paragraph, bounded, with the true length stated when it is cut."""
    s = "" if text is None else str(text)
    if len(s) <= limit:
        return s
    return f"{s[:limit]}… [{len(s)} characters total]"


def _listing(items: list[Any], limit: int = MAX_ITEMS) -> dict[str, Any]:
    """A bounded list that always reports the whole it came from."""
    return {"total": len(items), "shown": items[:limit]}


def _sample(rows: list[Any]) -> list[Any]:
    """A few rows of a table, bounded in BOTH directions.

    A row count alone is half a bound: a table five rows deep and sixteen thousand columns wide
    still puts eighty thousand cells into a manifest that is returned to the model verbatim. The
    true width is stated beside every sample — `columns` for a delimited file, the table's own
    `columns` for a document — so cutting the row here loses nothing a reader cannot see.

    A row is a list of cells or a mapping of column name to cell; both shapes are cut the same
    way and keep the shape they arrived in.
    """
    return [
        row[:MAX_ITEMS] if isinstance(row, list) else dict(list(row.items())[:MAX_ITEMS])
        for row in rows[:MAX_SAMPLE_ROWS]
    ]


# --- spreadsheets -----------------------------------------------------------------------------


def _sheet_part(sheet: Any) -> str:
    """Where this worksheet's XML lives inside the workbook zip.

    `ReadOnlyWorksheet` exposes it only as the private `_worksheet_path` — openpyxl's public
    `Worksheet.path` does not exist on the streaming class. Read through `getattr` so a rename in
    a future openpyxl costs the merge list and nothing else: `_merged_ranges` treats an unknown
    part as "no merges", which is the same answer it gives for a sheet that has none.
    """
    return str(getattr(sheet, "_worksheet_path", "") or "")


# `<mergeCell ref="A1:B2"/>` in the sheet part. Matched on bytes, single-or-double quoted, with
# arbitrary whitespace, which is the whole grammar this element has.
_MERGE_CELL_REF = re.compile(rb"""<mergeCell\s[^>]*?\bref\s*=\s*["\']([^"\']{1,64})["\']""")
# Read the part in slices and keep a small tail, so a tag straddling a boundary is still seen.
_MERGE_SCAN_CHUNK = 1 << 20
_MERGE_SCAN_OVERLAP = 256
# A sheet with more merges than this is not going to be summarised usefully anyway, and the cap
# keeps a hostile file from turning a bounded scan into an unbounded list.
_MERGE_SCAN_LIMIT = 10_000
# AND A SECOND CAP, ON MATCHES RATHER THAN RANGES, because the first one stopped bounding the READ
# the moment the results became a set. One `<mergeCell>` element repeated deflates to almost
# nothing: 0.67 MB on disk inflates to 274 MB of identical tags, which grow the distinct count not
# at all and so never reach the cap above. This one is what still ends the walk.
_MERGE_SCAN_MATCHES = 100_000


_MERGE_REF = re.compile(r"^([A-Z]+)(\d+):([A-Z]+)(\d+)$")


def _tops_a_banner(merged: list[str]) -> bool:
    """Whether the sheet opens with a merged title spanning columns.

    A merge further down the sheet, or one a single column wide, is ordinary formatting and
    says nothing about where the headings are. A merge that starts on row 1 and covers more
    than one column is a title bar — and the row beneath it is the real header.
    """
    for ref in merged:
        match = _MERGE_REF.match(ref)
        if match and match.group(2) == "1" and match.group(1) != match.group(3):
            return True
    return False


def _merged_ranges(path: Path, sheet_path: str) -> list[str]:
    """The sheet's merged ranges, read from its XML as BYTES rather than as a tree.

    `read_only=True` does not populate `worksheet.merged_cells`, and building the full object
    model just to reach it is what used to cost most of a gigabyte.

    NOT `ET.iterparse` EITHER, and that mistake is worth recording: clearing each element does not
    unlink it from its parent, so the root accumulates every `<row>` and `<c>` in the sheet. On a
    dense 10 MB workbook that walked the reader straight back over `MEMORY_LIMIT_BYTES` — 527 MB
    measured — while the openpyxl half of the same read sat at 38 MB. A chunked scan holds one
    slice at a time regardless of how large the part is.

    SAFE AGAINST CELL CONTENT that looks like markup: a citizen who types a literal `<mergeCell`
    tag into a cell has it stored XML-escaped (`&lt;mergeCell`), so the bytes this matches cannot
    come from user data.

    Never raises: a merge list is a nicety beside the shape, and a workbook whose part this cannot
    walk is still one whose dimensions and header are worth reporting.
    """
    import zipfile

    entry = sheet_path.lstrip("/")
    if not entry:
        return []
    # A DICT, SO THE REPORTED CAP COUNTS DISTINCT RANGES AND BOTH EXITS DE-DUPLICATE. The overlap
    # can re-match a tag that straddled a chunk boundary, so a list would let duplicates consume
    # the budget and would return them raw on the capped path. `seen` is the second cap, and it
    # counts raw matches: it is what still ends the walk when nothing new is being found.
    ranges: dict[str, None] = {}
    seen = 0
    try:
        with zipfile.ZipFile(path) as bundle, bundle.open(entry) as part:
            tail = b""
            while True:
                chunk = part.read(_MERGE_SCAN_CHUNK)
                if not chunk:
                    break
                window = tail + chunk
                for match in _MERGE_CELL_REF.finditer(window):
                    ranges[match.group(1).decode("ascii", "replace")] = None
                    seen += 1
                    if len(ranges) >= _MERGE_SCAN_LIMIT or seen >= _MERGE_SCAN_MATCHES:
                        return list(ranges)
                tail = window[-_MERGE_SCAN_OVERLAP:]
    except (KeyError, OSError, zipfile.BadZipFile):
        return []
    return list(ranges)


def read_xlsx(path: Path) -> dict[str, Any]:
    """Sheets, real dimensions, column types, formulas, merges and media.

    STREAMED, NOT MATERIALISED, and the difference is the whole reason a 10 MB workbook is
    readable at all. `read_only=True` walks the sheet XML a row at a time instead of building a
    cell object per cell: measured in the image at 1 vCPU / 2 GiB on a dense 9.95 MB workbook
    (17,200 x 40 = 688,000 cells of high-entropy text, no dimension record, so every pass walks
    the sheet), the full-model load peaked at 621 MB and took 8.9s, and this one peaks at 44 MB
    and takes 5.2s. The old shape did not merely cost more — it exceeded `MEMORY_LIMIT_BYTES` and
    the citizen was told to attach a smaller file.

    LOADED TWICE ON PURPOSE, and this is the requirement that costs the second pass. openpyxl
    reads either the FORMULAS (`data_only=False`) or the values Excel last CACHED for them
    (`data_only=True`) — never both from one load. A workbook written by a script has never been
    opened by Excel, so its formula cells have no cached value at all: with only the value load
    those columns come back blank, and the citizen is shown an empty column for data that is
    simply uncalculated. Reading both is how the manifest can say "this column is a formula and
    carries no stored result" instead. Two streamed passes are cheap; two materialised ones
    were the 829 MB.

    THE TWO THINGS `read_only` TAKES AWAY, and how each is given back:
      * `max_row`/`max_column` come from the sheet's own `<dimension>` record, which a writer can
        get wrong; the record is read, then discarded so it cannot truncate the rows, and the
        sheet is walked only when it is missing or the rows prove it too narrow.
      * `merged_cells` is never populated — `_merged_ranges` reads them from the sheet part.
    """
    import openpyxl
    from openpyxl.utils.exceptions import InvalidFileException

    try:
        formulas = openpyxl.load_workbook(path, data_only=False, read_only=True)
        values = openpyxl.load_workbook(path, data_only=True, read_only=True)
    except InvalidFileException as exc:
        raise ReadFailure(
            "unreadable",
            f"This file could not be opened as a spreadsheet ({exc}).",
            "Open it in Excel, re-save it as .xlsx, and attach it again.",
        ) from None

    sheets = []
    # ONLY THE TABS THAT HOLD CELLS. A chart moved onto its own sheet (Excel's ordinary
    # "Move Chart -> New sheet", which the upload door cannot see) is a chartsheet, and a
    # chartsheet carries no `max_row` — so the first attribute read below raised, the broad
    # arm caught it, and the whole workbook came back unreadable because one tab held a
    # picture. `worksheets` is openpyxl's own name for the grid-bearing sheets; a chart tab
    # has no cells to describe.
    for fsheet in formulas.worksheets:
        name = fsheet.title
        vsheet = values[name]
        # THE DECLARED `<dimension>` IS READ BEFORE IT IS DISCARDED, and discarding it is what
        # stops the damage. A read-only row is truncated to `max_column`, so a 50-row, 3-column
        # sheet declaring `A1:A1` did not merely report one row and one column — its other columns
        # were ABSENT from every row the reader saw, with `ok: true` and nothing to say so.
        # `reset_dimensions()` costs nothing and both passes take it, so the header, the type probe
        # and the cached values below all arrive at their real width whatever the sheet claims.
        declared_rows, declared_cols = fsheet.max_row or 0, fsheet.max_column or 0
        vsheet.reset_dimensions()
        fsheet.reset_dimensions()

        # ★ A MERGED TITLE ACROSS THE TOP IS A BANNER, NOT A HEADER. Read as one it makes the
        # reader confidently wrong about every column at once: the real headings sit on the row
        # below, so the names come back empty, every type is judged from a row of text, and a
        # formula column reports `isFormula: false` — defeating the second pass that exists to
        # catch exactly that. It is an ordinary corporate layout, and on a streamed read the
        # merge record is the only evidence of it left.
        merged = _merged_ranges(path, _sheet_part(fsheet))
        header_row = 2 if _tops_a_banner(merged) else 1

        # ONE PASS FOR EVERY ROW READ. A read-only sheet has no random `cell(row=, column=)`
        # access — that is the API's way of saying it never holds the grid — so the header and
        # the probe row are taken from the same forward walk rather than looked up per column.
        frows = fsheet.iter_rows(min_row=1, max_row=header_row + 1, values_only=True)
        seen_rows = [list(row or ()) for row in frows]
        header_values = seen_rows[header_row - 1] if len(seen_rows) >= header_row else []
        probe_values = seen_rows[header_row] if len(seen_rows) > header_row else []
        vrows = vsheet.iter_rows(min_row=1, max_row=header_row + 1, values_only=True)
        seen_cached = [list(row or ()) for row in vrows]
        cached_values = seen_cached[header_row] if len(seen_cached) > header_row else []
        width = max(len(header_values), len(probe_values))

        rows, cols = declared_rows, max(declared_cols, width)
        # `width == 0` JOINS THE WALK, and that is the empty-workbook case. A new workbook
        # declares `A1:A1`, so believing the record reports one row and one column for a
        # sheet holding nothing — `ok: true` over an empty file, which the module docstring
        # promises never to produce. It also covers a sheet whose first rows are blank and
        # whose data starts further down, where 0 would be just as wrong.
        if not declared_rows or declared_cols < width or width == 0:
            # WALKED ONLY WHEN THE RECORD IS ABSENT OR CAUGHT OUT. openpyxl's `write_only` writer
            # emits no dimension at all, and a record narrower than the row underneath it has
            # already been proved wrong — in both cases a walk is the only way to the real shape.
            #
            # NOT ON EVERY WORKBOOK, which is what forcing the walk unconditionally amounted to.
            # Measured in the image at 1 vCPU / 2 GiB: four sheets of 46,000 rows, 9.55 MB and well
            # inside the upload cap, went from 0.28s to 7.47s, and the same shape a little larger
            # spends the whole 30-second deadline and comes back telling the citizen to attach a
            # smaller file. Excel and openpyxl both always write a truthful record, so that is the
            # common case paying for the rare one.
            try:
                fsheet.calculate_dimension(force=True)
            except (ValueError, TypeError, UnboundLocalError):
                # A SHEET WITH NO NON-EMPTY ROW LEAVES THE WALK WITH AN UNBOUND LOCAL, and an
                # unused second tab is ordinary in a real workbook — refusing the whole file over
                # one would cost the citizen every other sheet in it. There is nothing to measure
                # and the 0 x 0 below is the truth. The same fallback covers a malformed record.
                pass
            rows, cols = fsheet.max_row or 0, max(fsheet.max_column or 0, width)

        # ONE HEADER ENTRY PER COLUMN THE FIRST ROWS ACTUALLY CARRY, and each half of that matters.
        #
        # A blank cell keeps its slot rather than being filtered out: a merge blanks every cell
        # but the first (`A1:B1` leaves B1 empty), and dropping those shifts every later column's
        # name one place left — a header that reads plausibly and is wrong, which is worse than a
        # gap. `_clip(None)` is "", the placeholder the materialising reader produced.
        #
        # And a short row is padded out to the wider of the two. A materialised grid handed back
        # one cell per column whether or not the row carried them; a streamed row stops at its
        # last cell, so without this a header row shorter than the row below it loses entries.
        #
        # `width`, NOT `cols`. Some writers declare the whole grid — `A1:XFD1048576` — for a sheet
        # holding six cells, and describing a column per declared column would put 16,384 empty
        # entries into a manifest that goes to the model. `columns` still reports what the sheet
        # claims; this list describes what is there to describe.
        header = [_clip(v) for v in header_values] + [""] * max(width - len(header_values), 0)

        columns = []
        for index in range(1, width + 1):
            # The first data row is what the column's type is judged from — the header row is
            # text in almost every real file and would make every column look like a string.
            #
            # Judged on the row that was READ, not on the count. A sheet declaring one row while
            # holding fifty would otherwise report every column's type as null.
            probe = probe_values[index - 1] if index <= len(probe_values) else None
            cached = cached_values[index - 1] if index <= len(cached_values) else None
            is_formula = isinstance(probe, str) and probe.startswith("=")
            column: dict[str, Any] = {
                "name": header[index - 1],
                "isFormula": is_formula,
            }
            if is_formula:
                # A FORMULA WITH NO CACHED VALUE is not an empty column; saying so
                # is the whole point of the second pass.
                column["hasStoredResult"] = cached is not None
                if not column["hasStoredResult"]:
                    column["note"] = (
                        "This column is a formula and the file carries no calculated result "
                        "for it — the workbook has not been opened by Excel since it was written."
                    )
            else:
                column["type"] = type(probe).__name__ if probe is not None else None
            columns.append(column)

        sheets.append(
            {
                "name": _clip(name),
                "rows": rows,
                "columns": cols,
                # WHICH ROW THE NAMES CAME FROM, because it is not always the first one and
                # an agent reading a column list has no other way to tell.
                "headerRow": header_row,
                # BOUNDED LIKE EVERY OTHER LIST HERE. A wide sheet — genuinely wide, or one
                # declaring 16,384 columns — otherwise emits one clipped cell per column with no
                # ceiling, and this manifest is returned to the model verbatim: a 7.7 MB workbook
                # that passes every door check produced a 10 MB reply built almost entirely of
                # header. It was the only list in the manifest that did not state its own whole.
                "header": _listing(header),
                "columnDetail": _listing(columns),
                "mergedRanges": _listing(merged),
            }
        )

    formulas.close()
    values.close()
    return {"sheets": _listing(sheets), "media": _zip_media(path)}


_ENCODING_SAMPLE_BYTES = 64 * 1024
_TRANSCODE_CHUNK = 1024 * 1024


def _text_encoding(path: Path) -> str:
    """Which encoding a delimited file is actually written in.

    ★ GETTING THIS WRONG IS THE CONFIDENTLY-WRONG CLASS ITSELF. Read as UTF-8, a UTF-16 file
    comes back `ok: true` with column names built out of interleaved NUL bytes, so the agent
    describes columns that do not exist. A cp1252 file — what Excel writes by default on
    Windows — failed to parse at all, and the advice that came back ("re-save it") produces
    the same bytes a second time.

    A BOM is definitive. Without one: a sample that is a quarter NUL bytes is UTF-16 from a
    tool that omitted the mark, and which half carries them gives the endianness; otherwise
    UTF-8 is tried, where a character cut in half by the sample boundary is not a failure;
    and anything still undecodable is read as cp1252, the estate's own default.
    """
    with path.open("rb") as handle:
        sample = handle.read(_ENCODING_SAMPLE_BYTES)
    if sample.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    if sample.startswith(codecs.BOM_UTF16_LE) or sample.startswith(codecs.BOM_UTF16_BE):
        return "utf-16"
    if sample.count(0) > len(sample) // 4:
        return "utf-16-le" if sample[1::2].count(0) >= sample[0::2].count(0) else "utf-16-be"
    try:
        codecs.getincrementaldecoder("utf-8")().decode(sample)
    except UnicodeDecodeError:
        return "cp1252"
    return "utf-8"


_CANDIDATE_SEPARATORS = (",", chr(9), ";", "|")


def _outside_quotes(line: str) -> str:
    """The line with every quoted field removed, for counting delimiters in.

    ★ A HEADING MAY CONTAIN THE OTHER DELIMITER, and counting raw characters let it decide.
    `gate,"owner; deputy; backup",waiting` holds three semicolons and two commas, so a comma
    file was read as a semicolon one and came back as four columns wearing pieces of each
    other's names — with `ok: true`, which is the failure this whole script exists to remove.
    """
    kept: list[str] = []
    quoted = False
    index = 0
    while index < len(line):
        char = line[index]
        if char == '"':
            # A doubled quote INSIDE a quoted field is an escaped quote, not the end of it.
            if quoted and line[index + 1 : index + 2] == '"':
                index += 2
                continue
            quoted = not quoted
        elif not quoted:
            kept.append(char)
        index += 1
    return "".join(kept)


def _separator(path: Path, encoding: str, default: str) -> str:
    """The delimiter the file actually uses, not the one its extension implies.

    ★ A SEMICOLON CSV IS THE LOCALE DEFAULT ACROSS MUCH OF EUROPE, and Excel writes it
    whenever the machine's list separator says so. Read as comma-delimited it parses
    perfectly into ONE column called `gate;owner;busy`, and an agent builds a schema from
    that — a wrong answer with nothing in it that looks wrong.

    Judged on the header line OUTSIDE its quoted fields, where a delimiter repeats and one
    inside a heading does not.
    The extension's own separator wins a tie, so an ordinary file keeps the behaviour it had.
    """
    with path.open("rb") as handle:
        head = handle.read(_ENCODING_SAMPLE_BYTES)
    lines = head.decode(encoding, errors="replace").splitlines()
    line = lines[0] if lines else ""
    bare = _outside_quotes(line)
    counts = {candidate: bare.count(candidate) for candidate in _CANDIDATE_SEPARATORS}
    best = max(_CANDIDATE_SEPARATORS, key=lambda candidate: counts[candidate])
    return best if counts[best] > counts[default] else default


def _as_utf8(path: Path, encoding: str) -> Path:
    """The same file rewritten as UTF-8, for a scanner that reads nothing else.

    polars takes no encoding on a lazy scan, so decoding here is the only way to read what
    the estate produces. Streamed a megabyte at a time, so a file at the upload cap costs
    kilobytes of memory rather than its own size twice; the caller removes the copy.
    """
    handle, name = tempfile.mkstemp(prefix="attachment-", suffix=".utf8")
    os.close(handle)
    target = Path(name)
    # REPLACING, NOT STRICT. cp1252 leaves five byte values undefined and a truncated file
    # ends mid-character; either would raise here and turn a readable file back into a
    # refusal, which is the failure this whole path exists to remove. The manifest names the
    # encoding, so a citizen can see which reading produced their column names.
    decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
    with path.open("rb") as source, target.open("w", encoding="utf-8", newline="") as out:
        while True:
            chunk = source.read(_TRANSCODE_CHUNK)
            if not chunk:
                out.write(decoder.decode(b"", True))
                break
            out.write(decoder.decode(chunk))
    return target


def read_delimited(path: Path, separator: str) -> dict[str, Any]:
    """Columns, types, null and distinct counts, the true row count, and a few sample rows.

    polars rather than a hand-rolled split: typed columns, null and distinct counts and a lazy
    scan are exactly what let a file's shape be reported HONESTLY instead of guessed from its
    first rows — which is what the replaced extractor did.
    """
    import polars as pl

    encoding = _text_encoding(path)
    separator = _separator(path, encoding, separator)
    source = path if encoding == "utf-8" else _as_utf8(path, encoding)
    try:
        lazy = pl.scan_csv(source, separator=separator, infer_schema_length=10_000)
        frame = lazy.collect()
    # ★ A FAILURE THAT ALREADY HAS A NAME KEEPS IT, and this clause must stay ABOVE the broad one
    # — placed below it the guard is valid, dead and silent, and nothing in the selected ruff set
    # would say so. The parentheses are load-bearing too: the image ships Python 3.13, where a
    # bindingless `except A, B:` is a hard SyntaxError (the backend is 3.14, where it is legal).
    #
    # `MemoryError` is the reader's own ceiling firing and `ReadFailure` is its deadline; neither
    # is a fact about the FILE. Swallowed by the arm below, both were reported as "could not be
    # read — re-save it in its own application", which is advice a citizen can follow all
    # afternoon without getting anywhere. Re-raised, `describe` gives each its own name and its
    # own next step (`too_large` → attach a smaller file; `timeout` → fewer sheets or rows).
    except (ReadFailure, MemoryError):
        raise
    # polars raises a family of parse errors; all mean the same thing here.
    except Exception as exc:
        raise ReadFailure(
            "unreadable",
            f"This file could not be read as delimited text ({type(exc).__name__}).",
            "Check it opens in a spreadsheet program, re-save it, and attach it again.",
        ) from None
    finally:
        if source is not path:
            source.unlink(missing_ok=True)

    # BOUNDED BEFORE IT IS BUILT, rather than after. `_listing` keeps 50 entries and throws
    # the rest away — but the comprehension had already asked polars for a null count and a
    # distinct count per column, two full scans each, for columns nobody would ever see.
    # Measured at 10,000 columns: 34 seconds, which spends the whole deadline on a 0.11 MB
    # file and answers `timeout` with advice to attach a smaller one. The schema is hoisted
    # for the same reason — `frame.schema` rebuilds the whole mapping on every access.
    schema = frame.schema
    columns = [
        {
            "name": _clip(name),
            "type": str(schema[name]),
            "nulls": int(frame[name].null_count()),
            "distinct": int(frame[name].n_unique()),
        }
        for name in frame.columns[:MAX_ITEMS]
    ]
    sample = [
        {_clip(k): _clip(v) for k, v in row.items()}
        for row in _sample(frame.head(MAX_SAMPLE_ROWS).to_dicts())
    ]
    # `rows` is the TRUE height, not the sample's length — the number the old extractor never said.
    # THE ENCODING IS STATED, because a file read through a fallback is a fact about the
    # answer: a citizen whose file was read as cp1252 can tell from this whether the accents
    # in their column names came back as they meant them.
    return {
        "rows": frame.height,
        "encoding": encoding,
        # WHICH DELIMITER PRODUCED THESE COLUMNS. A file read with the wrong one still
        # parses, into one column wearing every heading at once, so the answer has to say
        # which separator it used for the reader of it to tell.
        "separator": separator,
        # `total` is every column the file has; `shown` is the 50 described above.
        "columns": {"total": frame.width, "shown": columns},
        "sampleRows": sample,
    }


# --- documents and decks ----------------------------------------------------------------------


def read_docx(path: Path) -> dict[str, Any]:
    """Paragraphs, headings, tables with their headers intact, and media NAMED not inlined.

    TWO OF THE THREE MEASURED DEFECTS LIVE HERE. The replaced extractor inlined an embedded
    photo as text — 98.8% of one 66 KB report's 18,586 tokens were a picture the model could not
    see, and the 891 characters of actual prose were truncated to make room for it. And it
    flattened tables, losing the header row that says what the columns mean. So media is an
    inventory of names and sizes, and a table keeps its first row as `header`.
    """
    import docx  # python-docx

    try:
        document = docx.Document(str(path))
    except (ReadFailure, MemoryError):
        raise  # a ceiling or a deadline, not a damaged file — see `read_delimited`
    except Exception as exc:
        raise ReadFailure(
            "unreadable",
            f"This file could not be opened as a Word document ({type(exc).__name__}).",
            "Open it in Word, re-save it as .docx, and attach it again.",
        ) from None

    # BOUNDED AS IT GOES, and the totals counted separately. Building every paragraph of a
    # 400-page report to show fifty is the shape that cost the delimited reader its whole
    # deadline; here it costs memory the reader is bounded on, against a file the door
    # admits at 10 MB.
    paragraphs, headings = [], []
    paragraph_count = heading_count = 0
    for para in document.paragraphs:
        text = para.text.strip()
        if not text:
            continue
        if (para.style.name or "").startswith("Heading"):
            heading_count += 1
            if len(headings) < MAX_ITEMS:
                headings.append({"level": para.style.name, "text": _clip(text)})
        paragraph_count += 1
        if len(paragraphs) < MAX_ITEMS:
            paragraphs.append(_clip(text))

    tables = []
    table_count = 0
    for table in document.tables:
        table_count += 1
        if len(tables) >= MAX_ITEMS:
            continue
        rows = table.rows
        header = [_clip(c.text.strip()) for c in rows[0].cells] if rows else []
        body = [[_clip(c.text.strip()) for c in r.cells] for r in rows[1 : 1 + MAX_SAMPLE_ROWS]]
        tables.append(
            {
                # Kept as a HEADER, not folded into the body — and bounded, because a table can
                # be arbitrarily wide and this manifest goes to the model whole.
                "header": _listing(header),
                "rows": max(0, len(rows) - 1),
                "columns": len(header),
                "sampleRows": _sample(body),
            }
        )

    return {
        "paragraphs": {"total": paragraph_count, "shown": paragraphs},
        "headings": {"total": heading_count, "shown": headings},
        "tables": {"total": table_count, "shown": tables},
        "media": _zip_media(path),
    }


def read_pptx(path: Path) -> dict[str, Any]:
    """Slide text in order, speaker notes, tables and chart data.

    THE VISUAL DESIGN IS NOT REPORTED, and that is a stated scope boundary rather than a gap: a
    deck cannot be rendered without a converter the platform is not allowed to host. A citizen who
    needs the model to SEE a slide is told to export the deck to PDF, which lands it in the lane
    the model can read directly.
    """
    import pptx  # python-pptx

    try:
        deck = pptx.Presentation(str(path))
    except (ReadFailure, MemoryError):
        raise  # a ceiling or a deadline, not a damaged file — see `read_delimited`
    except Exception as exc:
        raise ReadFailure(
            "unreadable",
            f"This file could not be opened as a PowerPoint deck ({type(exc).__name__}).",
            "Open it in PowerPoint, re-save it as .pptx, and attach it again.",
        ) from None

    slides = []
    for number, slide in enumerate(deck.slides, start=1):
        text, tables, charts = [], [], []
        for shape in slide.shapes:
            if shape.has_text_frame and shape.text_frame.text.strip():
                text.append(_clip(shape.text_frame.text.strip()))
            if getattr(shape, "has_table", False):
                rows = shape.table.rows
                header = [_clip(c.text.strip()) for c in rows[0].cells] if len(rows) else []
                tables.append({"header": _listing(header), "rows": max(0, len(rows) - 1)})
            if getattr(shape, "has_chart", False):
                plots = shape.chart.plots
                categories = list(plots[0].categories) if len(plots) else []
                charts.append(
                    {
                        "type": str(shape.chart.chart_type),
                        "categories": _listing([_clip(c) for c in categories]),
                        "series": _listing(
                            [
                                {"name": _clip(s.name), "values": _listing(list(s.values))}
                                for s in shape.chart.series
                            ]
                        ),
                    }
                )

        notes = ""
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
            notes = _clip(slide.notes_slide.notes_text_frame.text.strip())

        slides.append(
            {
                "number": number,
                "text": _listing(text),
                "notes": notes,
                "tables": _listing(tables),
                "charts": _listing(charts),
            }
        )

    return {"slides": _listing(slides), "media": _zip_media(path)}


# --- shared -----------------------------------------------------------------------------------


def _zip_media(path: Path) -> dict[str, Any]:
    """What images and other media the file carries — NAMED AND SIZED, never inlined.

    This is the defect that cost the most: an embedded photo rendered as text is bytes the model
    cannot see, charged at full price, crowding out the words that mattered. An inventory tells
    the agent a picture exists without spending the context to not-look at it.

    Every Office format is a ZIP, so one implementation covers all three.
    """
    import zipfile

    try:
        with zipfile.ZipFile(path) as archive:
            media = [
                {"name": _clip(info.filename.split("/")[-1]), "bytes": info.file_size}
                for info in archive.infolist()
                if "/media/" in info.filename
            ]
    except (zipfile.BadZipFile, OSError):
        # Not fatal: the caller already parsed the file, so a media inventory that cannot be read
        # is missing detail rather than a failed read.
        return {"total": 0, "shown": [], "note": "The media inventory could not be read."}
    return _listing(media)


READERS = {
    ".xlsx": read_xlsx,
    ".docx": read_docx,
    ".pptx": read_pptx,
    ".csv": lambda p: read_delimited(p, ","),
    ".tsv": lambda p: read_delimited(p, "\t"),
}


def _apply_bounds() -> None:
    """Wall clock and address space, so a hostile or pathological file cannot wedge the container.

    Both raise into the same named-failure path as any other error — a killed read is reported,
    not silent. `resource` is Linux-only, which is what the sandbox runs; the guard is skipped
    where it is unavailable so the script stays runnable for development on other platforms.
    """

    def _out_of_time(_signum: int, _frame: Any) -> None:
        raise ReadFailure(
            "timeout",
            f"Reading this file took longer than {TIME_LIMIT_SECONDS} seconds.",
            "Attach a smaller file, or one with fewer sheets or rows.",
        )

    if hasattr(signal, "SIGALRM"):
        signal.signal(signal.SIGALRM, _out_of_time)
        signal.alarm(TIME_LIMIT_SECONDS)
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_DATA, (MEMORY_LIMIT_BYTES, MEMORY_LIMIT_BYTES))
    except (ImportError, ValueError, OSError):
        pass


def describe(path: Path) -> dict[str, Any]:
    """The manifest for one file, or a raised `ReadFailure` naming what went wrong."""
    if not path.exists():
        # A GENUINE ERROR, not an expected path: the platform restores the file from the stored
        # original before the agent's first read, so the reader only ever meets a present file.
        raise ReadFailure(
            "missing",
            f"There is no file at {path}.",
            "Ask for the file to be restored, or attach it again.",
        )
    reader = READERS.get(path.suffix.lower())
    if reader is None:
        raise ReadFailure(
            "unsupported",
            f"{path.suffix or 'This file type'} is not one this reader handles.",
            "Attach a spreadsheet (.xlsx), document (.docx), deck (.pptx), .csv or .tsv.",
        )
    try:
        body = reader(path)
    except ReadFailure:
        raise
    except MemoryError:
        raise ReadFailure(
            "too_large",
            "This file needs more memory to read than the workspace allows.",
            "Attach a smaller file, or split it.",
        ) from None
    except Exception as exc:
        # ENCRYPTION LANDS HERE, among other things. Every library refuses a password-protected
        # file in its own way, so the shape is named rather than the exception type guessed at.
        hint = (
            "encrypted"
            if "encrypt" in str(exc).lower() or "password" in str(exc).lower()
            else "unreadable"
        )
        raise ReadFailure(
            hint,
            f"This file could not be read ({type(exc).__name__}).",
            "Remove the password and attach it again."
            if hint == "encrypted"
            else "Re-save the file in its own application and attach it again.",
        ) from None
    return {"ok": True, "file": path.name, "kind": path.suffix.lower().lstrip("."), **body}


MAX_MANIFEST_CHARS = 200_000
_TRIMMED_ITEMS = 5


def _without(node: Any, key: str) -> Any:
    """The manifest with one key dropped wherever it appears."""
    if isinstance(node, list):
        return [_without(item, key) for item in node]
    if isinstance(node, dict):
        return {k: _without(v, key) for k, v in node.items() if k != key}
    return node


def _shorter_listings(node: Any, limit: int) -> Any:
    """Every bounded list cut further, with its `total` left alone — the whole is the part
    of a listing an agent reasons about, and it costs nothing to keep."""
    if isinstance(node, list):
        return [_shorter_listings(item, limit) for item in node]
    if isinstance(node, dict):
        shrunk = {k: _shorter_listings(v, limit) for k, v in node.items()}
        shown = shrunk.get("shown")
        if isinstance(shown, list):
            shrunk["shown"] = shown[:limit]
        return shrunk
    return node


def _within_budget(result: dict[str, Any]) -> dict[str, Any]:
    """The manifest, trimmed if it would flood the window it is returned into.

    ★ THE PER-LIST CAPS MULTIPLY, which is why a bound on the whole is a separate thing from
    the bounds on its parts. Fifty slides each holding fifty tables of fifty cells sits inside
    every individual cap and is tens of megabytes as one answer — and this is handed to the
    model verbatim, so a single oversized reply costs the turn it was meant to serve.

    Sample rows go first and described columns second, because a count is what an agent
    reasons about and a sample is the illustration. `manifestTrimmed` says it happened, since
    a quietly shortened answer is the confidently-wrong shape this reader exists to remove.
    """
    if len(json.dumps(result, default=str)) <= MAX_MANIFEST_CHARS:
        return result
    trimmed = _without(result, "sampleRows")
    if len(json.dumps(trimmed, default=str)) > MAX_MANIFEST_CHARS:
        trimmed = _shorter_listings(trimmed, _TRIMMED_ITEMS)
    trimmed["manifestTrimmed"] = True
    return trimmed


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "code": "usage",
                        "message": "Give the reader exactly one file path.",
                        "next": f"Run: python3 {argv[0]} <path>",
                    },
                }
            )
        )
        return 0
    _apply_bounds()
    path = Path(argv[1])
    try:
        result = describe(path)
    except ReadFailure as failure:
        # EXIT 0 WITH A NAMED FAILURE, deliberately. A non-zero exit is read as "the command
        # broke" and invites a retry or a hand-written parser; a JSON failure is an answer the
        # agent can pass to the citizen as a sentence.
        result = {
            "ok": False,
            "file": path.name,
            "error": {"code": failure.code, "message": failure.message, "next": failure.next_step},
        }
    finally:
        if hasattr(signal, "SIGALRM"):
            signal.alarm(0)
    print(json.dumps(_within_budget(result), default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
