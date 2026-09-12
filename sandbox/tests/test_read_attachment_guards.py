"""An already-named reader failure keeps its own name.

WHY THIS IS A SEPARATE MODULE FROM `test_read_attachment.py`. That one `importorskip`s all four
reader libraries at module scope, so on any machine missing one of them it reports "13 skipped" —
which reads like green and proves nothing. What is under test here is not parsing at all: it is
which exception classes the three readers let past, and that question needs no library. So the
libraries are STUBBED into `sys.modules` and these cases run everywhere, including on a developer
machine and in CI, where the suite next door cannot.

THE DEFECT. `read_delimited`, `read_docx` and `read_pptx` each wrapped their library call in one
broad `except Exception`, which catches `MemoryError` (the reader's own 512 MB ceiling) and
`ReadFailure` (its own 30-second deadline) along with everything else. Both came back to the
citizen as "this file could not be read — re-save it in its own application": a fact about the
platform, worded as a fact about their file, with advice they could follow all afternoon without
getting anywhere. `describe()` has always had the right arms; nothing ever reached them.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import zipfile
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

READER = Path(__file__).resolve().parents[1] / "scripts" / "read_attachment.py"


def _function(path: Path, name: str) -> ast.FunctionDef:
    """One reader's own body, so a source assertion cannot be satisfied from somewhere else."""
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} is gone from {path.name} — this guard needs rewriting")


def _calls(body: ast.AST, name: str) -> bool:
    """Is `name` called anywhere inside this body?"""
    return any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name
        for node in ast.walk(body)
    )


def _emits(body: ast.AST, key: str) -> bool:
    """Does this body build a dict carrying `key`?"""
    return any(isinstance(node, ast.Constant) and node.value == key for node in ast.walk(body))


def _load_reader() -> ModuleType:
    """The shipped script, imported as a module so `describe` can be called directly.

    `test_read_attachment.py` drives it as a subprocess, deliberately — that is how an agent runs
    it, and it keeps the argument handling and the exit code under test. Here the subject is an
    exception path that has to be INJECTED, so the module is imported instead.
    """
    spec = importlib.util.spec_from_file_location("bial_read_attachment", READER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


reader_module = _load_reader()
ReadFailure = reader_module.ReadFailure

# Which library each reader reaches for, and the one entry point it calls on it. Stubbing at this
# seam means the guard is tested where it sits rather than through a real parse.
_ENTRY_POINT = {
    ".csv": ("polars", "scan_csv"),
    ".docx": ("docx", "Document"),
    ".pptx": ("pptx", "Presentation"),
}


def _raise_from_the_library(
    monkeypatch: pytest.MonkeyPatch, suffix: str, failure: BaseException
) -> None:
    """Make this suffix's reader library raise `failure` the moment it is called."""
    library, attribute = _ENTRY_POINT[suffix]

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise failure

    monkeypatch.setitem(reader_module.sys.modules, library, SimpleNamespace(**{attribute: boom}))


def _a_file(tmp_path: Path, suffix: str) -> Path:
    """A real file, because `describe` refuses a missing one before it reaches any reader."""
    path = tmp_path / f"roster{suffix}"
    path.write_bytes(b"anything")
    return path


@pytest.mark.parametrize("suffix", [".csv", ".docx", ".pptx"])
@pytest.mark.parametrize("code", ["too_large", "timeout"])
def test_a_bound_that_fired_is_reported_as_itself_not_as_a_damaged_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str, code: str
) -> None:
    """★ SIX CASES, ALL WRONG BEFORE. A read killed by the memory ceiling and a read killed by the
    deadline are facts about the platform's limits; "re-save it in its own application" is not
    something a citizen can do about either, and following it produces the identical failure.

    The two are injected the way they really arrive: `MemoryError` from the allocator under
    `RLIMIT_DATA`, and a `ReadFailure('timeout')` raised into the parse by the `SIGALRM` handler.

    Mutation check: drop the guard from any one reader, or move it BELOW the broad arm where it is
    valid, dead and silent, and that reader's two cases go red.
    """
    failure: BaseException = (
        MemoryError()
        if code == "too_large"
        else ReadFailure("timeout", "Reading this file took too long.", "Attach a smaller file.")
    )
    _raise_from_the_library(monkeypatch, suffix, failure)

    with pytest.raises(ReadFailure) as caught:
        reader_module.describe(_a_file(tmp_path, suffix))

    assert caught.value.code == code
    assert caught.value.code != "unreadable"
    assert "re-save" not in caught.value.next_step.lower()


@pytest.mark.parametrize("suffix", [".csv", ".docx", ".pptx"])
def test_a_genuinely_damaged_file_still_gets_its_format_specific_remedy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str
) -> None:
    """The guard narrows what the broad arm sees; it must not empty it. A library that fails
    because the bytes are rubbish is still `unreadable`, still with the advice that fits the
    format — which is also the receipt that the two cases above are a real narrowing rather than
    a reader that stopped catching anything."""
    _raise_from_the_library(monkeypatch, suffix, ValueError("not a valid file"))

    with pytest.raises(ReadFailure) as caught:
        reader_module.describe(_a_file(tmp_path, suffix))

    assert caught.value.code == "unreadable"
    assert "attach it again" in caught.value.next_step.lower()


def test_the_guards_parse_under_the_version_the_image_actually_ships() -> None:
    """★ THE PARENTHESES ARE LOAD-BEARING. The image ships Python 3.13, where a bindingless
    `except A, B:` is a hard SyntaxError; the backend that runs this suite is 3.14, where PEP 758
    makes it legal. So a stripped-paren clause would pass every check here and break the reader in
    the only environment it runs in.

    `ruff format` is never run over this file for the same reason (its formatter targets the
    backend's version and strips them), and this is the assertion that would catch it if it were.
    """
    source = READER.read_text(encoding="utf-8")

    assert "except (ReadFailure, MemoryError):" in source
    assert source.count("except (ReadFailure, MemoryError):") == 3
    assert "except ReadFailure, MemoryError:" not in source


# --- the reader streams, and that is load-bearing ------------------------------------


def test_the_workbook_is_streamed_never_materialised() -> None:
    """★ THE PROPERTY THE 10 MB CAP DEPENDS ON, pinned against the source.

    `read_only=True` walks the sheet XML a row at a time; `read_only=False` builds a cell object
    per cell. Measured in the shipped image on a dense 9.99 MB workbook (920,000 cells) at
    1 vCPU / 2 GiB: materialising peaked at 829 MB and was REFUSED by `MEMORY_LIMIT_BYTES`;
    streaming peaks at 46 MB and reads in 5.5s. Both loads must be streamed — the second one
    (`data_only=True`, for cached formula results) is the same size as the first.

    Read off the AST rather than the source text, because the text also contains this rule
    written out in prose — the first version of this test matched its own docstring. Measured
    rather than asserted is not an option here: the libraries are absent outside the image, and a
    memory assertion is exactly the kind that goes quietly green when it is skipped.
    `test_read_attachment.py` covers the OUTPUT; this covers the how.

    Mutation receipt: flip either `read_only=True` back to `False` and this goes red.
    """
    loads = [
        call
        for call in ast.walk(ast.parse(READER.read_text(encoding="utf-8")))
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "load_workbook"
    ]

    assert len(loads) == 2, f"expected two workbook loads, found {len(loads)}"
    for call in loads:
        streamed = [k.value for k in call.keywords if k.arg == "read_only"]
        assert streamed and all(
            isinstance(v, ast.Constant) and v.value is True for v in streamed
        ), (
            "both workbook loads must stream; a materialising load cannot read a 10 MB workbook "
            "inside MEMORY_LIMIT_BYTES"
        )


def test_the_merge_scan_does_not_build_a_tree() -> None:
    """★ THE MISTAKE THIS ALREADY MADE ONCE, pinned so it cannot come back.

    Merged ranges are not populated on a read-only worksheet, so they are read from the sheet part
    directly. The first attempt used `ET.iterparse` with `element.clear()` — which does NOT unlink
    the element from its parent, so the root accumulated every `<row>` and `<c>` in the sheet. On
    the dense workbook that put the reader at 527 MB and straight back over the ceiling, while the
    openpyxl half of the same read sat at 38 MB.

    Read off the AST for the same reason as the test above: the explanation names the very call
    it forbids.

    Mutation receipt: reintroduce `iterparse` in `_merged_ranges` and this goes red.
    """
    tree = ast.parse(READER.read_text(encoding="utf-8"))
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    } | {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    assert "iterparse" not in called
    assert "_MERGE_SCAN_CHUNK" in {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }


def test_a_merge_ref_split_across_a_read_boundary_is_still_found() -> None:
    """The chunked scan keeps a tail so a tag straddling two reads is not lost — and the overlap
    must not double-count the tags it re-sees."""
    reader = _load_reader()
    ranges = reader._MERGE_CELL_REF.findall(
        b'<mergeCells count="2"><mergeCell ref="A1:B1"/><mergeCell ref=\'C3:D9\'/></mergeCells>'
    )

    assert [r.decode() for r in ranges] == ["A1:B1", "C3:D9"]


def test_a_cell_containing_merge_markup_cannot_forge_a_range() -> None:
    """A citizen who types a literal `<mergeCell` tag into a cell has it stored XML-escaped, so
    the bytes the scan matches cannot come from user data. Pinned because the scan reads bytes
    rather than parsing, and this is the assumption that makes that safe."""
    reader = _load_reader()
    escaped = b'<c r="A1" t="inlineStr"><is><t>&lt;mergeCell ref="Z1:Z9"/&gt;</t></is></c>'

    assert reader._MERGE_CELL_REF.findall(escaped) == []


def test_the_reader_measures_the_sheet_rather_than_believing_its_dimension_record() -> None:
    """★ A WORKBOOK CAN LIE ABOUT ITS OWN SHAPE, and the streaming read believed it.

    `calculate_dimension(force=True)` recomputes only when the dimension record is UNSET, so a
    sheet that DECLARES `A1:A1` over fifty rows of real data was summarised as one row and one
    column — with `ok: true`, no error, and nothing to suggest anything had been missed. It reads
    exactly like a nearly-empty file. Declaring a huge range fails the other way, reporting a
    six-cell sheet as a million rows.

    `reset_dimensions()` discards the declared record so the scan actually happens.

    NOT REACHABLE FROM AN OPENPYXL-WRITTEN FIXTURE, which is why the differential harness that
    checked the streaming rewrite could not catch it: openpyxl always writes a truthful record.
    The bytes have to be rewritten afterwards, so the source is asserted here and the behaviour is
    pinned in the sandbox image where the libraries live.

    THE RECEIVER IS PART OF THE ASSERTION. Two sheets are open per workbook — the formula pass
    and the value pass — and only the formula sheet's shape is reported. Resetting the other one
    leaves the defect entirely intact while a name-only check stays green.

    Mutation receipt: drop the `reset_dimensions()` call, move it onto `vsheet`, or swap it below
    `calculate_dimension`, and a lying workbook reports the shape it claims instead of the shape
    it has.
    """
    calls = [
        (node.func.value.id, node.func.attr)
        for node in ast.walk(_function(READER, "read_xlsx"))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
    ]

    assert ("fsheet", "reset_dimensions") in calls, (
        "the formula sheet's declared dimension record must be discarded before its shape is "
        "read, or a workbook that misdeclares its own extent is summarised as the shape it claims"
    )
    assert calls.index(("fsheet", "reset_dimensions")) < calls.index(
        ("fsheet", "calculate_dimension")
    ), "reset must come first: calculate_dimension only recomputes an UNSET record"


def test_no_table_reaches_the_manifest_unbounded_in_either_direction() -> None:
    """★ THE MANIFEST GOES TO THE MODEL VERBATIM, so an unbounded list in it is unbounded context.

    `header` was emitted raw. A workbook that is genuinely wide — or one declaring 16,384 columns
    — turned a 7.7 MB upload that passed every door check into a ~10 MB reply built almost
    entirely of header cells, which the read tool returns without truncating.

    A ROW COUNT IS HALF A BOUND. `sampleRows` was cut to five rows and left as wide as the table,
    so the same 16,384 columns came back five times over instead of once. Both the document and
    the delimited reader had that shape.

    Mutation receipt: emit `header` bare, or slice `sampleRows` by row count alone, and this goes
    red.
    """
    source = READER.read_text(encoding="utf-8")

    assert '"header": header' not in source, "a manifest list must carry its true total"
    assert source.count('"header": _listing(header)') == 3
    for reader_name in ("read_docx", "read_delimited"):
        body = _function(READER, reader_name)
        assert _emits(body, "sampleRows"), f"{reader_name} emits no sample rows — retire this arm"
        assert _calls(body, "_sample"), (
            f"{reader_name}'s sample rows are cut by row count alone, leaving each one as wide as "
            "the table it came from"
        )


def test_a_sample_is_cut_in_both_directions_whichever_shape_a_row_arrives_in() -> None:
    """`_sample` is the one place the width bound lives, and it takes two row shapes.

    A document table's row is a list of cells; a delimited file's row is a mapping of column name
    to cell. Both are cut the same way and both keep the shape they arrived in, because the
    manifest's readers branch on it.
    """
    wide_list_rows = [list(range(200)) for _ in range(9)]
    cut = reader_module._sample(wide_list_rows)

    assert len(cut) == reader_module.MAX_SAMPLE_ROWS
    assert all(len(row) == reader_module.MAX_ITEMS for row in cut)
    assert all(isinstance(row, list) for row in cut)

    wide_dict_rows = [{f"c{n}": n for n in range(200)} for _ in range(9)]
    cut = reader_module._sample(wide_dict_rows)

    assert len(cut) == reader_module.MAX_SAMPLE_ROWS
    assert all(len(row) == reader_module.MAX_ITEMS for row in cut)
    assert all(isinstance(row, dict) for row in cut)
    # The columns kept are the FIRST ones, so a sample lines up with the header beside it.
    assert list(cut[0]) == [f"c{n}" for n in range(reader_module.MAX_ITEMS)]


def test_the_merge_scan_spends_its_budget_on_distinct_ranges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ THE CHUNK OVERLAP RE-MATCHES, so a list would count the same range twice.

    The scan reads the sheet part in fixed chunks and carries an overlap forward, because a
    `<mergeCell>` tag can straddle a boundary. That means a tag inside the overlap is matched by
    both windows. Accumulated into a list, the duplicate spent one of the 10,000 slots AND came
    back raw on the capped exit — so a heavily merged sheet reported fewer real ranges than it
    had, some of them twice.

    Driven here with the chunk, overlap and cap all shrunk, because the real numbers need a
    megabyte of XML to reach either path. Needs no reader library, so unlike the parse-level
    suite this runs everywhere.

    Mutation receipt: accumulate into a list again and the distinct-count assertion goes red.
    """
    part = "xl/worksheets/sheet1.xml"
    tags = "".join(f'<mergeCell ref="A{n}:B{n}"/>' for n in range(1, 9))
    book = tmp_path / "merged.xlsx"
    with zipfile.ZipFile(book, "w") as archive:
        archive.writestr(part, f"<worksheet><mergeCells>{tags}</mergeCells></worksheet>")

    monkeypatch.setattr(reader_module, "_MERGE_SCAN_CHUNK", 64)
    monkeypatch.setattr(reader_module, "_MERGE_SCAN_OVERLAP", 48)
    monkeypatch.setattr(reader_module, "_MERGE_SCAN_LIMIT", 3)

    found = reader_module._merged_ranges(book, f"/{part}")

    assert len(found) == len(set(found)), f"the overlap's re-matches were counted: {found}"
    assert found == ["A1:B1", "A2:B2", "A3:B3"], (
        "the cap must be spent on the first DISTINCT ranges, in the order they appear"
    )


def test_the_merge_scan_stops_when_it_stops_finding_anything_new(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ A CAP ON DISTINCT RANGES IS NOT A CAP ON THE READ.

    Counting distinct ranges is the honest reading of the limit, and it removed the only thing
    bounding the walk: one `<mergeCell>` element repeated grows the result not at all, so the
    early exit became unreachable. Identical bytes deflate to nothing — 0.67 MB on disk inflates
    to 275 MB of identical tags — and the scan walked all of it, 3.18s against 0.01s before, on a
    budget shared with the sheet walk and a `SIGALRM` that fires once.

    Proved by what the scan never reaches rather than by a stopwatch: the one distinct range sits
    at the very end, past a run of repeats longer than the match budget.

    Mutation receipt: drop the `seen` counter and `Z9:Z9` comes back.
    """
    part = "xl/worksheets/sheet1.xml"
    tags = '<mergeCell ref="A1:B2"/>' * 200 + '<mergeCell ref="Z9:Z9"/>'
    book = tmp_path / "repeated.xlsx"
    with zipfile.ZipFile(book, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(part, f"<worksheet><mergeCells>{tags}</mergeCells></worksheet>")

    monkeypatch.setattr(reader_module, "_MERGE_SCAN_MATCHES", 20)

    found = reader_module._merged_ranges(book, f"/{part}")

    assert found == ["A1:B2"]
    assert "Z9:Z9" not in found, "the scan read the whole part looking for something new"


# --- the shape a workbook DECLARES, against the shape it has ---------------------------


def _workbook(tmp_path: Path, rows: list[list[Any]], *, declares: str | None = None) -> Path:
    """A real .xlsx, optionally with its `<dimension>` record rewritten to something untrue.

    openpyxl always writes a truthful record, so the lying case can only be reached by editing the
    sheet part afterwards — which is why the differential harness behind the streaming rewrite
    could not produce it, and why the fixture is built here rather than stubbed.
    """
    openpyxl = pytest.importorskip("openpyxl")

    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Data"
    for row in rows:
        sheet.append(row)
    honest = tmp_path / "honest.xlsx"
    book.save(honest)
    if declares is None:
        return honest

    with zipfile.ZipFile(honest) as bundle:
        names = bundle.namelist()
        blobs = {name: bundle.read(name) for name in names}
    part = next(n for n in names if re.match(r"xl/worksheets/sheet\d+\.xml$", n))
    blobs[part], hits = re.subn(
        rb"<dimension[^>]*>", f'<dimension ref="{declares}"/>'.encode(), blobs[part]
    )
    assert hits == 1, "no dimension record to rewrite — the fixture builder needs updating"
    lying = tmp_path / "lying.xlsx"
    with zipfile.ZipFile(lying, "w", zipfile.ZIP_DEFLATED) as out:
        for name in names:
            out.writestr(name, blobs[name])
    return lying


def test_a_sheet_that_declares_itself_narrower_than_it_is_gets_read_at_its_real_width(
    tmp_path: Path,
) -> None:
    """★ THE DEFECT, AS BEHAVIOUR RATHER THAN AS SOURCE TEXT.

    A read-only row is truncated to `max_column`, so a sheet declaring `A1:A1` over fifty rows of
    three columns did not merely report one row and one column — b and c were absent from every
    row the reader saw, with `ok: true` and nothing to say anything had been missed. It reads
    exactly like a nearly-empty file, and the agent answers from it.

    Mutation receipt: drop `fsheet.reset_dimensions()` and the width goes to 1; drop the
    `declared_cols < width` arm and the row count goes to 1.
    """
    rows = [["a", "b", "c"], *[[f"r{n}", n, n * 1.5] for n in range(49)]]
    book = _workbook(tmp_path, rows, declares="A1:A1")

    sheet = reader_module.read_xlsx(book)["sheets"]["shown"][0]

    assert (sheet["rows"], sheet["columns"]) == (50, 3)
    assert sheet["header"] == {"total": 3, "shown": ["a", "b", "c"]}


def test_a_sheet_that_declares_the_whole_grid_gets_no_column_per_declared_column(
    tmp_path: Path,
) -> None:
    """Some writers declare `A1:XFD1048576` for a sheet holding six cells. The counts are the
    sheet's own claim and are reported as such, but describing a column per declared column would
    put 16,384 empty entries into a manifest that is returned to the model verbatim."""
    book = _workbook(tmp_path, [["a", "b", "c"], [1, 2, 3]], declares="A1:XFD1048576")

    sheet = reader_module.read_xlsx(book)["sheets"]["shown"][0]

    assert sheet["header"]["total"] == 3
    assert sheet["columnDetail"]["total"] == 3


def test_an_unused_second_tab_does_not_cost_the_citizen_the_whole_workbook(
    tmp_path: Path,
) -> None:
    """★ A BLANK SHEET IS ORDINARY — a template's unused Sheet2, left in place.

    With no dimension record to read, the sheet is walked for its extent, and openpyxl's walk
    binds the cell it measures from inside the loop: with no non-empty row there is no cell, and
    it raises `UnboundLocalError`, which is a `NameError` and so passes straight through a guard
    written for `ValueError`/`TypeError`. The whole file came back "could not be read — re-save it
    in its own application", advice that cannot work, because re-saving keeps the blank tab.

    WRITTEN `write_only`, which is what makes the case reachable: that writer emits no dimension
    record, so the walk is the only way to a row count. A sheet that declares `A1` is believed and
    never walked.

    Mutation receipt: narrow the except clause back to `(ValueError, TypeError)` and this goes red
    on `ok`.
    """
    openpyxl = pytest.importorskip("openpyxl")

    book = openpyxl.Workbook(write_only=True)
    data = book.create_sheet("Data")
    data.append(["a", "b"])
    data.append([1, 2])
    book.create_sheet("Blank")
    path = tmp_path / "two.xlsx"
    book.save(path)

    out = reader_module.describe(path)

    assert out["ok"] is True
    assert [s["name"] for s in out["sheets"]["shown"]] == ["Data", "Blank"]


def test_a_truthful_dimension_record_is_taken_at_its_word_rather_than_re_walked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ THE COMMON CASE MUST NOT PAY FOR THE RARE ONE.

    Excel and openpyxl both always write a truthful `<dimension>`, and walking the sheet anyway
    costs a full parse per sheet. Measured in the image at 1 vCPU / 2 GiB, four sheets of 46,000
    rows — 9.55 MB, inside the upload cap — went from 0.28s to 7.47s, and the same shape a little
    larger spends the whole 30-second deadline and tells the citizen to attach a smaller file.

    Counted rather than timed: a duration assertion is flaky on a loaded machine, and what matters
    is whether the walk happens at all.

    Mutation receipt: force the walk unconditionally and this goes red.
    """
    openpyxl = pytest.importorskip("openpyxl")
    read_only = pytest.importorskip("openpyxl.worksheet._read_only")

    walks: list[str] = []
    original = read_only.ReadOnlyWorksheet.calculate_dimension

    def counted(self: Any, force: bool = False) -> Any:
        walks.append(self.title)
        return original(self, force=force)

    monkeypatch.setattr(read_only.ReadOnlyWorksheet, "calculate_dimension", counted)

    book = openpyxl.Workbook()
    data = book.active
    data.title = "Data"
    data.append(["a", "b", "c"])
    for n in range(200):
        data.append([n, n + 1, n + 2])
    path = tmp_path / "honest.xlsx"
    book.save(path)

    sheet = reader_module.read_xlsx(path)["sheets"]["shown"][0]

    assert (sheet["rows"], sheet["columns"]) == (201, 3)
    assert walks == [], f"a truthful record was re-walked on {walks}"


def test_a_formula_past_a_short_dimension_still_reports_the_result_the_file_carries(
    tmp_path: Path,
) -> None:
    """★ THE MANIFEST'S FLAGSHIP HONESTY CLAIM, on the path the width fix newly reaches.

    The workbook is loaded twice because openpyxl reads either the formulas or the values Excel
    cached for them, never both. Resetting only the formula pass leaves the value pass truncated
    to the declared width, so a formula column recovered by the fix is described as carrying no
    calculated result — about a file that plainly carries one. A silent under-report replaced by a
    confidently stated falsehood is the worse of the two.

    Mutation receipt: drop `vsheet.reset_dimensions()` and `hasStoredResult` goes false.
    """
    pytest.importorskip("openpyxl")

    book = _workbook(tmp_path, [["a", "b", "total"], [3, 4, "=A2+B2"]])
    with zipfile.ZipFile(book) as bundle:
        names = bundle.namelist()
        blobs = {name: bundle.read(name) for name in names}
    part = next(n for n in names if re.match(r"xl/worksheets/sheet\d+\.xml$", n))
    blobs[part] = blobs[part].replace(b"<f>A2+B2</f>", b"<f>A2+B2</f><v>7</v>")
    blobs[part], hits = re.subn(rb"<dimension[^>]*>", b'<dimension ref="A1:A2"/>', blobs[part])
    assert hits == 1
    cached = tmp_path / "cached.xlsx"
    with zipfile.ZipFile(cached, "w", zipfile.ZIP_DEFLATED) as out:
        for name in names:
            out.writestr(name, blobs[name])

    detail = reader_module.read_xlsx(cached)["sheets"]["shown"][0]["columnDetail"]["shown"]

    total = next(column for column in detail if column["name"] == "total")
    assert total["isFormula"] is True
    assert total["hasStoredResult"] is True, "the file carries <v>7</v> for this cell"
    assert "note" not in total
