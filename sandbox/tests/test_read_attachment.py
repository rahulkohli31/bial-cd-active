"""The shipped attachment reader — the three measured defects, and the one return shape.

Runs in the sandbox image, where the four reader libraries live. These are not smoke tests: each
case below is one of the ways the REPLACED extractor was wrong, asserted so the replacement cannot
be wrong the same way.

The fixtures are built here rather than checked in, so what each test proves is visible in the
test itself — a checked-in .xlsx whose formulas have no cached values looks identical to one whose
formulas do, and the difference is the entire point of
`test_a_formula_column_with_no_stored_result`.
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
from pathlib import Path

import pytest

READER = Path(__file__).resolve().parents[1] / "scripts" / "read_attachment.py"

# The canonical 1x1 transparent PNG. Base64 here so the fixture is a real image the
# libraries accept; the assertions below check this text never reaches the MANIFEST.
_ONE_PIXEL_PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAA"
    "DUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

openpyxl = pytest.importorskip("openpyxl", reason="reader libraries live in the sandbox image")
_chart = pytest.importorskip("openpyxl.chart", reason="reader libraries live in the sandbox image")
BarChart, Reference = _chart.BarChart, _chart.Reference
docx = pytest.importorskip("docx", reason="reader libraries live in the sandbox image")
pptx = pytest.importorskip("pptx", reason="reader libraries live in the sandbox image")
pytest.importorskip("polars", reason="reader libraries live in the sandbox image")


def run(path: Path) -> dict:
    """Drive the reader the way an agent does — as a script, reading its stdout.

    Through the command line rather than by importing `describe`, deliberately: the agent is told
    one invocation line to copy, and running it any other way would leave the argument handling,
    the bounds and the exit code untested.
    """
    proc = subprocess.run(
        [sys.executable, str(READER), str(path)], capture_output=True, text=True, timeout=120
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


# --- the three measured defects ---------------------------------------------------------


def test_a_formula_column_with_no_stored_result_says_so(tmp_path: Path) -> None:
    """★ A workbook written by a script has never been opened by Excel, so its formulas carry no
    cached value. Read with `data_only=True` alone those columns come back EMPTY, and the citizen
    is shown a blank column for data that is merely uncalculated — a confident wrong answer of
    exactly the kind this work removes.

    Mutation receipt: drop the `data_only=False` load and `isFormula` is False for every column,
    failing the first assertion.
    """
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append(["passengers", "bags", "total"])
    sheet.append([120, 240, "=A2+B2"])
    path = tmp_path / "ops.xlsx"
    book.save(path)

    out = run(path)

    total = out["sheets"]["shown"][0]["columnDetail"]["shown"][2]
    assert total["isFormula"] is True
    assert total["hasStoredResult"] is False
    assert "no calculated result" in total["note"]


def test_a_merged_title_row_does_not_become_the_header(tmp_path: Path) -> None:
    """★ THE LAYOUT THAT MAKES THE READER CONFIDENTLY WRONG ABOUT EVERY COLUMN AT ONCE.

    A title merged across the top is ordinary in a corporate workbook. Taken as the header it
    names one column and blanks the rest, judges every type from a row of text, and reports a
    formula column as ordinary data — the one answer the second pass exists to prevent. An arm
    holding a shell can work around it by opening the file itself; the Plan arm cannot.

    Mutation receipt: take the header from row 1 again and the overtime column comes back
    `isFormula: false` with an empty name.
    """
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append(["Gate roster - September", None, None])
    sheet.merge_cells("A1:C1")
    sheet.append(["gate", "hours", "overtime"])
    sheet.append(["A1", 9, "=(B3-8)*450"])
    path = tmp_path / "banner.xlsx"
    book.save(path)

    out = run(path)

    described = out["sheets"]["shown"][0]
    assert described["headerRow"] == 2
    assert described["header"]["shown"] == ["gate", "hours", "overtime"]
    overtime = described["columnDetail"]["shown"][2]
    assert overtime["name"] == "overtime"
    assert overtime["isFormula"] is True


def test_a_chart_on_its_own_tab_does_not_hide_the_data_sheets(tmp_path: Path) -> None:
    """★ ONE TAB HOLDING A PICTURE MUST NOT COST THE CITIZEN THE WHOLE FILE.

    "Move Chart -> New sheet" is an ordinary Excel action, and what it produces is a
    chartsheet: a tab with no cells and therefore no `max_row`. Reading the workbook by name
    touched that attribute on the chart tab, raised, and the broad arm turned it into
    "this file could not be read" — for a workbook whose data sheets were all intact. The
    upload door cannot catch it either, because the file is a perfectly valid workbook.

    THE CHART IS REAL, and that is not decoration: a chartsheet with nothing on it trips a
    loader bug of its own, in both read modes, and would pin the wrong failure. What Excel
    produces always carries the chart that was moved onto it.

    Mutation receipt: iterate the workbook by sheet name again and this comes back not ok.
    """
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append(["gate", "flights"])
    sheet.append(["A1", 12])
    sheet.append(["A2", 18])
    chart = BarChart()
    chart.add_data(Reference(sheet, min_col=2, min_row=1, max_row=3), titles_from_data=True)
    book.create_chartsheet("Overview").add_chart(chart)
    path = tmp_path / "with-a-chart.xlsx"
    book.save(path)

    out = run(path)

    assert out["ok"] is True
    assert [s["name"] for s in out["sheets"]["shown"]] == ["Sheet"]
    assert out["sheets"]["shown"][0]["header"]["shown"] == ["gate", "flights"]


def test_an_embedded_image_is_named_not_inlined(tmp_path: Path) -> None:
    """★ The defect that cost the most: one 66 KB report was 18,586 tokens, 98.8% of it an
    embedded photo the model cannot see, with the 891 characters of real prose truncated to make
    room. Media is an inventory — a name and a size — never bytes."""
    # A REAL 1x1 PNG: python-docx parses the header for the image's dimensions, so a stub with
    # only the magic prefix is refused before the document is written.
    image = tmp_path / "photo.png"
    image.write_bytes(base64.b64decode(_ONE_PIXEL_PNG))
    document = docx.Document()
    document.add_paragraph("The incident occurred at gate 14.")
    document.add_picture(str(image))
    path = tmp_path / "incident.docx"
    document.save(path)

    out = run(path)

    assert out["media"]["total"] >= 1
    entry = out["media"]["shown"][0]
    assert entry["bytes"] > 0
    # The bytes are counted, never carried: no base64, no raw image data anywhere in the manifest.
    blob = json.dumps(out)
    assert "iVBOR" not in blob and "\\u0089PNG" not in blob
    assert len(blob) < 4000


def test_a_table_header_survives_as_a_header(tmp_path: Path) -> None:
    """★ The extractor flattened tables, losing the row that says what the columns MEAN. A header
    folded into the body is a table an agent has to guess at."""
    document = docx.Document()
    table = document.add_table(rows=3, cols=2)
    table.rows[0].cells[0].text = "terminal"
    table.rows[0].cells[1].text = "movements"
    table.rows[1].cells[0].text = "T1"
    table.rows[1].cells[1].text = "42"
    path = tmp_path / "rota.docx"
    document.save(path)

    out = run(path)

    first = out["tables"]["shown"][0]
    assert first["header"] == {"total": 2, "shown": ["terminal", "movements"]}
    assert first["rows"] == 2  # the header is NOT counted as a body row
    assert ["T1", "42"] in first["sampleRows"]


# --- the whole is always stated ---------------------------------------------------------


def test_a_large_csv_reports_its_true_row_count_not_the_sample(tmp_path: Path) -> None:
    """★ THE ORIGINAL FAILURE, IN ONE ASSERTION. The replaced extractor kept the first thousand
    rows and said nothing about the rest, so a 5,000-row file was answered from a fifth of it. The
    sample is bounded; the count is the truth."""
    path = tmp_path / "movements.csv"
    lines = ["badge,name,terminal"] + [f"{i},Person {i},T{i % 4}" for i in range(5000)]
    path.write_text("\n".join(lines), encoding="utf-8")

    out = run(path)

    assert out["rows"] == 5000
    assert len(out["sampleRows"]) <= 5
    assert out["columns"]["total"] == 3


def test_a_semicolon_csv_is_read_as_columns_rather_than_as_one_wide_one(tmp_path: Path) -> None:
    """★ THE LIST SEPARATOR EXCEL USES WHEREVER THE MACHINE SAYS SO, which is much of Europe.

    Read as comma-delimited such a file parses perfectly — into ONE column named
    `gate;owner;busy` — and an agent builds a schema out of that. Nothing in the answer looks
    wrong, which is the class this reader exists to remove.

    Mutation receipt: take the separator from the extension again and the column total is 1.
    """
    path = tmp_path / "gates.csv"
    path.write_text("gate;owner;busy\nA1;ops;true\nA2;ground;false\n", encoding="utf-8")

    out = run(path)

    assert out["separator"] == ";"
    assert [c["name"] for c in out["columns"]["shown"]] == ["gate", "owner", "busy"]
    assert out["rows"] == 2


def test_a_comma_in_a_heading_does_not_take_a_tab_file_away_from_tabs(tmp_path: Path) -> None:
    """The sniff must never take a file away from the delimiter its extension implies on equal
    evidence. A `.tsv` whose heading contains a comma has one of each on its header line, and
    the extension is the tie-breaker — read as commas it yields `gate\tnote` as one column.

    Mutation receipt: let a tie switch the separator and the columns below merge and split.
    """
    path = tmp_path / "notes.tsv"
    path.write_text("gate\tnote,owner\nA1\tbusy,ops\n", encoding="utf-8")

    out = run(path)

    assert out["separator"] == chr(9)
    assert [c["name"] for c in out["columns"]["shown"]] == ["gate", "note,owner"]


def test_an_empty_workbook_is_reported_as_empty(tmp_path: Path) -> None:
    """★ `rows: 1, columns: 1` FOR A SHEET HOLDING NOTHING.

    A new workbook declares `A1:A1`, and believing that record told a citizen their empty file
    had content — against this module's own promise that a manifest never reads like an empty
    file when it is not one, and equally the other way round.

    Mutation receipt: trust the declared dimension again and this reports one row, one column.
    """
    book = openpyxl.Workbook()
    path = tmp_path / "empty.xlsx"
    book.save(path)

    out = run(path)

    sheet = out["sheets"]["shown"][0]
    assert sheet["rows"] == 0
    assert sheet["columns"] == 0
    assert sheet["header"]["total"] == 0


def test_a_long_document_states_its_whole_and_shows_a_bounded_part(tmp_path: Path) -> None:
    """The count is the honest half of a bounded list — and it is counted while reading rather
    than by building every paragraph and then throwing most of them away."""
    document = docx.Document()
    for index in range(60):
        document.add_paragraph(f"paragraph {index}")
    path = tmp_path / "long.docx"
    document.save(path)

    out = run(path)

    assert out["paragraphs"]["total"] == 60
    assert len(out["paragraphs"]["shown"]) == 50


def test_a_manifest_that_would_flood_the_window_is_trimmed_and_says_so(tmp_path: Path) -> None:
    """★ THE PER-LIST CAPS MULTIPLY, so a bound on the whole is a separate thing from the bounds
    on its parts. Fifty sheets of fifty long headings sits inside every individual cap and is
    close to a megabyte as one answer — handed to the model verbatim, so one oversized reply
    costs the turn it was meant to serve.

    Trimmed rather than refused, and it says which, because a quietly shortened answer is the
    confidently-wrong shape all of this exists to remove.

    Mutation receipt: return the manifest unbounded and this is megabytes with no flag.
    """
    book = openpyxl.Workbook()
    headings = ["x" * 400 for _ in range(50)]
    for index in range(50):
        sheet = book.create_sheet(f"S{index}")
        sheet.append(headings)
        sheet.append(list(range(50)))
    book.remove(book["Sheet"])
    path = tmp_path / "wide.xlsx"
    book.save(path)

    out = run(path)

    assert out["manifestTrimmed"] is True
    assert len(json.dumps(out)) <= 200_000
    # The counts survive the trim: what was there is still stated, only the detail is shorter.
    assert out["sheets"]["total"] == 50


def test_a_utf16_file_is_read_as_itself_rather_than_as_garbage(tmp_path: Path) -> None:
    """★ THE CONFIDENTLY-WRONG CLASS, IN THE READER THAT EXISTS TO REMOVE IT.

    PowerShell redirection, Notepad's "Save As → Unicode" and older SSMS all write UTF-16, and
    read as UTF-8 such a file came back `ok: true` with column names built from interleaved NUL
    bytes. The agent then described columns that were never in the file — worse than a refusal,
    because nothing about the answer looks wrong.

    Mutation receipt: scan the original path again and the first column is not `gate`.
    """
    path = tmp_path / "movements.csv"
    path.write_text("gate,staff\nA1,Priya\nA2,Arun\n", encoding="utf-16")

    out = run(path)

    assert out["ok"] is True
    assert [c["name"] for c in out["columns"]["shown"]] == ["gate", "staff"]
    assert out["rows"] == 2
    assert out["encoding"] == "utf-16"


def test_a_csv_saved_by_excel_on_windows_is_read_rather_than_refused(tmp_path: Path) -> None:
    """★ cp1252 IS THE DEFAULT THIS ESTATE PRODUCES, and it used to be a dead end: the parse
    failed, and the advice that came back — re-save it — writes exactly the same bytes again.

    It is read as cp1252, which accepts every byte, and the manifest says so, because a name that
    came back through a fallback is a fact about the answer.

    Mutation receipt: drop the cp1252 fallback and this file is reported unreadable.
    """
    path = tmp_path / "staff.csv"
    path.write_bytes("gate,name\nA1,Zoë\nA2,René\n".encode("cp1252"))

    out = run(path)

    assert out["ok"] is True
    assert out["encoding"] == "cp1252"
    assert out["sampleRows"][0]["name"] == "Zoë"


def test_a_delimited_file_reports_types_nulls_and_distincts(tmp_path: Path) -> None:
    """The shape an app gets built to: what each column IS, how empty it is, how many values it
    takes. None of it is guessable from the first rows, which is why the old extractor guessed."""
    path = tmp_path / "gates.csv"
    path.write_text("gate,busy\nA1,true\nA2,\nA1,false\n", encoding="utf-8")

    out = run(path)

    by_name = {c["name"]: c for c in out["columns"]["shown"]}
    assert by_name["gate"]["distinct"] == 2
    assert by_name["busy"]["nulls"] == 1


def test_a_tsv_is_read_on_tabs_not_commas(tmp_path: Path) -> None:
    """A .tsv whose fields contain commas is one column if the separator is wrong — and the file
    would look plausible either way, which is why this is asserted rather than assumed."""
    path = tmp_path / "x.tsv"
    path.write_text("name\tnote\nAsha\tgate 4, terminal 1\n", encoding="utf-8")

    out = run(path)

    assert [c["name"] for c in out["columns"]["shown"]] == ["name", "note"]
    assert out["rows"] == 1


# --- one return shape, including failure ------------------------------------------------


def test_a_corrupt_file_is_a_named_failure_not_a_crash(tmp_path: Path) -> None:
    """★ A file that passed every check at the door can still be rubbish inside. The citizen is
    told it is damaged and what to do — not shown an empty result, which reads exactly like an
    empty file, and not a stack trace from inside the container."""
    path = tmp_path / "broken.xlsx"
    path.write_bytes(b"PK\x03\x04 this is not really a workbook")

    out = run(path)

    assert out["ok"] is False
    assert out["error"]["code"] in {"unreadable", "encrypted"}
    assert out["error"]["next"]
    assert "Traceback" not in json.dumps(out)


def test_a_missing_file_is_a_named_failure(tmp_path: Path) -> None:
    """The platform restores the file before the agent's first read, so this branch is a genuine
    error rather than an expected path — but it still has to be a sentence, not an exception."""
    out = run(tmp_path / "never-existed.xlsx")

    assert out["ok"] is False
    assert out["error"]["code"] == "missing"


def test_an_unsupported_type_names_what_is_supported(tmp_path: Path) -> None:
    path = tmp_path / "notes.rtf"
    path.write_text("hello", encoding="utf-8")

    out = run(path)

    assert out["ok"] is False
    assert out["error"]["code"] == "unsupported"
    assert ".xlsx" in out["error"]["next"]


def test_every_outcome_exits_zero_and_prints_one_json_object(tmp_path: Path) -> None:
    """★ THE CONTRACT THAT MAKES THE REST USABLE. A non-zero exit reads as "the command broke" and
    invites a retry or a hand-written parser — the single outcome this design exists to prevent.
    Every path prints one object and exits 0, so a failure is an ANSWER."""
    good = tmp_path / "a.csv"
    good.write_text("a,b\n1,2\n", encoding="utf-8")
    bad = tmp_path / "b.xlsx"
    bad.write_bytes(b"not a workbook at all")

    for path in (good, bad, tmp_path / "gone.csv"):
        proc = subprocess.run(
            [sys.executable, str(READER), str(path)], capture_output=True, text=True, timeout=120
        )
        assert proc.returncode == 0, path
        parsed = json.loads(proc.stdout)  # exactly one object, always parseable
        assert "ok" in parsed


def test_no_arguments_is_answered_rather_than_traced() -> None:
    proc = subprocess.run(
        [sys.executable, str(READER)], capture_output=True, text=True, timeout=120
    )

    assert proc.returncode == 0
    assert json.loads(proc.stdout)["error"]["code"] == "usage"


# --- the rest of the manifest ------------------------------------------------------------


def test_a_workbook_reports_sheets_dimensions_and_merges(tmp_path: Path) -> None:
    book = openpyxl.Workbook()
    first = book.active
    first.title = "Movements"
    first.append(["a", "b"])
    first.append([1, 2])
    first.merge_cells("A1:B1")
    book.create_sheet("Notes")
    path = tmp_path / "two.xlsx"
    book.save(path)

    out = run(path)

    assert out["sheets"]["total"] == 2
    names = [s["name"] for s in out["sheets"]["shown"]]
    assert names == ["Movements", "Notes"]
    movements = out["sheets"]["shown"][0]
    assert movements["rows"] == 2 and movements["columns"] == 2
    assert "A1:B1" in movements["mergedRanges"]["shown"]


def test_a_deck_reports_slide_order_and_speaker_notes(tmp_path: Path) -> None:
    """A deck's text, order and notes are read; its visual design is not, which is a stated scope
    boundary — a citizen who needs the model to SEE a slide exports the deck to PDF."""
    deck = pptx.Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[5])
    slide.shapes.title.text = "Ground handling review"
    slide.notes_slide.notes_text_frame.text = "Mention the T2 backlog."
    path = tmp_path / "review.pptx"
    deck.save(path)

    out = run(path)

    first = out["slides"]["shown"][0]
    assert first["number"] == 1
    assert "Ground handling review" in first["text"]["shown"]
    assert "T2 backlog" in first["notes"]
