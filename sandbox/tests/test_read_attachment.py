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
