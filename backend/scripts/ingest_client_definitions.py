"""Fold a round of the client's answers into `data/connectors/dice/definitions.json`.

THIS IS A RECURRING EVENT, NOT A ONE-OFF. BIAL reviews the column workbook in rounds: we send the
working set with a question against each column, they return the file with their answers, and the
next round corrects the round before. So the ingest is a script rather than an afternoon of hand
edits, and it prints what changed rather than asking anyone to diff 131 rows by eye.

WHAT IT READS. The workbook the client returns, whose `Column Definitions` sheet carries:

    Column name | Type | Definition | Example values | Status | Remarks | <Reviewer> Remarks

The LAST column is the answer. The `Definition` column is what WE sent them -- for most rows a
stub this repo generated from the column name ("Aircraft body.") -- so taking it back in would
overwrite good text with our own guess. The reviewer's remark is the only client-authored meaning
in the sheet, and the `Status` column says how far it has been checked.

WHAT IT WILL NOT DO.

  * It will not turn "No Idea" into a definition. Nine columns came back that way in round 1, and
    an answer of that shape means the airport does not know either -- which the agent must be told
    plainly, because our own inferred sentence reads exactly as confidently as a real one. Those
    rows keep our text, lose their claim to be the client's, and gain the question.
  * It will not touch a measured field. `populated`, `null_pct`, `distinct` and `examples` come
    from profiling the real files; the client's opinion does not change what the bytes held.
  * It will not write anything in dry-run, which is the default. Read the report, then `--apply`.

AFTER APPLYING, REBUILD AND RE-TEST: `uv run python scripts/build_connector_catalogue.py` renders
the artefact the agent is handed, and its size moves -- that block is delivered once per
conversation, so a round of answers has a token cost worth reading before shipping.

    uv run python scripts/ingest_client_definitions.py <workbook.xlsx> --round v1
    uv run python scripts/ingest_client_definitions.py <workbook.xlsx> --round v1 --apply
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

DATA_DIR: Final = Path(__file__).resolve().parent.parent / "data" / "connectors" / "dice"
DEFINITIONS: Final = DATA_DIR / "definitions.json"
SHEET: Final = "Column Definitions"

# The reviewer's own column. Its header carries their name, so it is matched by shape -- the last
# header ending in "Remarks" that is not the plain "Remarks" column we sent.
REVIEWER_HEADER: Final = re.compile(r"\bremarks\b", re.IGNORECASE)

# An answer that is not one. BIAL writing "No Idea" is information -- it is not a definition, and
# promoting it to one would put a confident empty sentence in front of the model.
NOT_AN_ANSWER: Final = re.compile(
    r"^\s*(no idea|not sure|unknown|tbd|n/?a|\?+)\s*\.?\s*$", re.IGNORECASE
)

# WHAT WE MEASURED AND THEY DID NOT. The client answers what a column MEANS; the profile knows how
# it is STORED, and those are different facts. BIAL's "Total number of bags arrived" is a better
# meaning than ours and still silently drops "stored as a decimal string (e.g. '42.00000')" — the
# kind of detail that produces a green build with wrong numbers. Round 1 would have dropped six of
# them, and a shipped test (`test_an_opaque_code_renders_its_whole_gloss_unclipped`) caught it. So
# a sentence of ours carrying a storage fact is KEPT beside their meaning, not replaced by it.
MEASURED_FACT: Final = re.compile(
    r"YYYYMMDDHHMMSS|stored as|two different formats|empty string|padded|decimal string"
    r"|rather than a (?:number|date)|null on|never null|trailing space"
    # UNITS TOO, for the same reason. Round 1 replaced TAT's "Turnaround time in minutes" with
    # "Turn Around Time": correct, and it drops the unit the arithmetic depends on.
    r"|in minutes|in seconds|SECONDS|SIGNED|per hour",
    re.IGNORECASE,
)

# Answers that describe SHAPE rather than subject: a value that depends on the row, a field that is
# an internal sequence rather than the code it looks like, or an enumeration folded into prose.
STRUCTURAL_MEANING: Final = re.compile(
    r"if row belongs|depending on|looks like|id seq|rotation key|brings arrival", re.IGNORECASE
)

SOURCE_ANSWERED: Final = "Client — answered {round}"
SOURCE_MERGED: Final = "Client — answered {round}, plus our measured note"
SOURCE_UNDEFINED: Final = "INFERRED — BIAL could not define ({round})"
QUESTION_UNDEFINED: Final = (
    "BIAL was asked to define this column and answered that they do not know. The text above is "
    "ours, not theirs. Do not rely on it."
)


@dataclass(frozen=True)
class Change:
    """One column's move, in the words the report prints."""

    name: str
    kind: str
    before: str
    after: str
    status: str


def reviewer_column(headers: list[str]) -> int:
    """Which column holds the client's answers.

    THE HEADER CARRIES A PERSON'S NAME ("Kranthi Remarks"), and the next round may carry someone
    else's, so this matches the shape instead: the LAST header mentioning remarks. The plain
    "Remarks" column is the one we sent them and never the answer.
    """
    candidates = [
        i
        for i, h in enumerate(headers)
        if REVIEWER_HEADER.search(h) and h.strip().lower() != "remarks"
    ]
    if not candidates:
        raise SystemExit(
            f"no reviewer remarks column in {headers!r} — the client's answers are the last "
            "column ending in 'Remarks'; if the shape changed, this script must change with it"
        )
    return candidates[-1]


def read_workbook(path: Path) -> dict[str, dict[str, str]]:
    """`{column name: {definition, status}}` as the client returned it."""
    try:
        import openpyxl
    except ModuleNotFoundError:  # pragma: no cover - a developer running it bare
        raise SystemExit("openpyxl is needed: uv run --with openpyxl python scripts/…") from None

    book = openpyxl.load_workbook(path, data_only=True)
    if SHEET not in book.sheetnames:
        raise SystemExit(f"{path.name} has no {SHEET!r} sheet (found {book.sheetnames})")
    rows = list(book[SHEET].iter_rows(values_only=True))
    headers = [str(h).strip() if h else "" for h in rows[0]]
    answer_at = reviewer_column(headers)
    status_at = headers.index("Status") if "Status" in headers else -1

    def cell(row: tuple[Any, ...], index: int) -> str:
        if index < 0 or index >= len(row) or row[index] is None:
            return ""
        return " ".join(str(row[index]).split())

    out: dict[str, dict[str, str]] = {}
    for row in rows[1:]:
        if not row or not row[0]:
            continue
        out[str(row[0]).strip()] = {
            "answer": cell(row, answer_at),
            "status": cell(row, status_at),
        }
    return out


def keep_measured(ours: str) -> str:
    """The sentences of OUR definition that state a measured fact, or "" if it states none.

    Sentence-level rather than all-or-nothing: most of our text is a guess at meaning, which their
    answer rightly replaces, while one clause in it may be the only record of how the column is
    actually stored."""
    sentences = [s.strip() for s in re.split(r"(?<=\.)\s+", ours) if s.strip()]
    return " ".join(s for s in sentences if MEASURED_FACT.search(s))


def fold(column: dict[str, Any], answered: dict[str, str], tag: str) -> Change | None:
    """Apply one client answer to one column, or report why it was left alone."""
    name = str(column["name"])
    answer, status = answered["answer"], answered["status"]
    before = str(column.get("definition", ""))

    if not answer:
        return Change(name, "no answer in this round", before, before, status)

    if NOT_AN_ANSWER.match(answer):
        # Their "No Idea" is the finding. Keep our sentence, strip its borrowed authority.
        column["source"] = SOURCE_UNDEFINED.format(round=tag)
        column["client_status"] = status or column.get("client_status", "")
        column["question"] = QUESTION_UNDEFINED
        return Change(name, "client cannot define it", before, before, status)

    if answer == before:
        column["client_status"] = status or column.get("client_status", "")
        return Change(name, "unchanged", before, answer, status)

    kept = keep_measured(before)
    merged = f"{answer.rstrip('.')}. {kept}" if kept else answer
    # A MEANING THE NAME CANNOT CARRY EARNS A GLOSS. The renderer glosses bare codes only, on the
    # ground that `ACTUAL_OFF_BLOCK_TIME_AOBT` says what it is. Round 1 broke that assumption:
    # AIBT_AOBT_TIME is not "both times", it is the arrival's in-block OR the departure's
    # off-block depending on the row -- the same merged-column trap the artefact spends a
    # paragraph warning about for SIBT_SOBT_TIME. A name cannot say that, so the column is marked
    # for a gloss and the renderer honours the mark.
    if STRUCTURAL_MEANING.search(merged):
        column["gloss"] = True
    column["definition"] = merged
    column["source"] = (SOURCE_MERGED if kept else SOURCE_ANSWERED).format(round=tag)
    column["client_status"] = status or column.get("client_status", "")
    column["question"] = ""
    # A ROUND MUST NOT QUIETLY LOSE MEANING. Their answer is authoritative about what a column IS,
    # and it is sometimes just the acronym spelled out ("TAT" -> "Turn Around Time") where ours
    # described what it measures. That is not a storage fact, so the merge above does not keep it,
    # and it is not a mistake either -- it is a judgement someone has to make per column. Flag it
    # rather than pick: a silent shrink is how a definition set rots one round at a time.
    if not kept and len(before) > 60 and len(answer) < len(before) / 2:
        kind = "REVIEW - their answer is much shorter than ours"
    else:
        kind = "redefined, our measured note kept" if kept else "redefined by the client"
    return Change(name, kind, before, merged, status)


def report(changes: list[Change], missing: list[str], unknown: list[str], applied: bool) -> None:
    by_kind: dict[str, list[Change]] = {}
    for change in changes:
        by_kind.setdefault(change.kind, []).append(change)

    print(f"\n{'APPLIED' if applied else 'DRY RUN — nothing written'}\n")
    for kind, rows in sorted(by_kind.items(), key=lambda kv: -len(kv[1])):
        print(f"{len(rows):4}  {kind}")
    if missing:
        print(
            f"{len(missing):4}  in our set but absent from the workbook: {', '.join(missing[:8])}"
        )
    if unknown:
        print(f"{len(unknown):4}  in the workbook but not in our set: {', '.join(unknown[:8])}")

    redefined = by_kind.get("redefined by the client", [])
    if redefined:
        print("\nREDEFINED — what the agent will now read:\n")
        for change in redefined:
            print(f"  {change.name}  [{change.status}]")
            print(f"      was: {change.before[:100] or '(nothing)'}")
            print(f"      now: {change.after[:100]}")

    cannot = by_kind.get("client cannot define it", [])
    if cannot:
        print("\nBIAL COULD NOT DEFINE THESE — our text stays, marked as ours:\n")
        for change in cannot:
            print(f"  {change.name:24} {change.before[:70]}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workbook", type=Path, help="the file the client returned")
    parser.add_argument("--round", default="v1", help="round label recorded in `source`")
    parser.add_argument("--apply", action="store_true", help="write definitions.json")
    args = parser.parse_args()

    if not args.workbook.exists():
        raise SystemExit(f"no such workbook: {args.workbook}")

    answers = read_workbook(args.workbook)
    document = json.loads(DEFINITIONS.read_text(encoding="utf-8"))
    columns: list[dict[str, Any]] = document["columns"]
    ours = {str(c["name"]) for c in columns}

    changes = [
        change
        for column in columns
        if (
            change := fold(
                column, answers.get(str(column["name"]), {"answer": "", "status": ""}), args.round
            )
        )
    ]
    missing = sorted(ours - set(answers))
    unknown = sorted(set(answers) - ours)

    if args.apply:
        DEFINITIONS.write_text(
            json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

    report(changes, missing, unknown, applied=args.apply)
    if args.apply:
        print("\nnow rebuild the artefact:  uv run python scripts/build_connector_catalogue.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
