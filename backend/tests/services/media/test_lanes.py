"""The two lanes — and the widening that must not reach the model.

An attachment is routed by what can be DONE with it: the model reads images and PDFs itself, and
code in the workspace reads everything else. These tests pin the boundary in both directions,
because the dangerous half is invisible — a deck reaching the model as raw ZIP bytes looks like a
working upload right up until the answer is wrong.
"""

from __future__ import annotations

from src.services.media import ALLOWED_MEDIA, CODE_LANE_MEDIA, is_code_lane
from src.services.media.lanes import (
    CSV_MEDIA_TYPE,
    EXCEL_MEDIA_TYPE,
    PPTX_MEDIA_TYPE,
    TSV_MEDIA_TYPE,
    WORD_MEDIA_TYPE,
    code_lane_refusal,
    looks_password_protected,
)

_ZIP = bytes([0x50, 0x4B, 0x03, 0x04])
_OLE2 = bytes([0xD0, 0xCF, 0x11, 0xE0, 0xA1, 0xB1, 0x1A, 0xE1])


def _ooxml(part: bytes) -> bytes:
    return _ZIP + bytes(2) + part + bytes(32)


def test_the_two_lanes_do_not_overlap() -> None:
    """★ THE INVARIANT THE WHOLE FEATURE RESTS ON. A media type is read by the model or by code,
    never both — a type in both sets would take whichever path happened to check first."""
    assert not (CODE_LANE_MEDIA & set(ALLOWED_MEDIA))


def test_the_model_allowlist_was_not_widened() -> None:
    """★ HAZARD 8, ASSERTED RATHER THAN REMEMBERED.

    `ALLOWED_MEDIA` is the magic-byte gate, and it is applied on both paths that end at the model:
    the upload route and the store's rehydrator. Adding OOXML to it would make both answer True
    for a deck, leaving a hand-written refusal as the only thing between a spreadsheet and the
    model's context.

    IT WAS THREE PATHS WHEN THIS WAS WRITTEN — the build session's own attachment resolver refused
    a deck by name, and that whole surface is retired. The count moving is the argument rather
    than a correction to it: a lane is a property of the media type, so a consumer appearing or
    disappearing changes nothing about what this asserts.

    So the code lane is its own set, and the model-facing consumers keep refusing it with no
    line changing in either of them. Widen `ALLOWED_MEDIA` and this test says so.
    """
    for media_type in (EXCEL_MEDIA_TYPE, WORD_MEDIA_TYPE, PPTX_MEDIA_TYPE, CSV_MEDIA_TYPE):
        assert media_type not in ALLOWED_MEDIA
        assert is_code_lane(media_type)


def test_an_office_file_must_carry_its_own_opc_part() -> None:
    """All OOXML shares the ZIP signature, so the part name is the only discriminator there is —
    without it a renamed `.zip` is admitted as a deck, and a `.docx` sent as a `.xlsx` is stored
    as a workbook the reader then cannot open."""
    assert code_lane_refusal(EXCEL_MEDIA_TYPE, "book.xlsx", _ooxml(b"xl/workbook.xml")) is None
    assert code_lane_refusal(WORD_MEDIA_TYPE, "doc.docx", _ooxml(b"word/document.xml")) is None
    assert code_lane_refusal(PPTX_MEDIA_TYPE, "d.pptx", _ooxml(b"ppt/presentation.xml")) is None

    # A workbook sent as a document, and a plain archive renamed: both refused.
    assert code_lane_refusal(WORD_MEDIA_TYPE, "x.docx", _ooxml(b"xl/workbook.xml")) is not None
    assert code_lane_refusal(EXCEL_MEDIA_TYPE, "z.xlsx", _ZIP + b"just a zip") is not None


def test_a_password_protected_office_file_is_refused_at_the_door() -> None:
    """★ R6. An encrypted Office file is not a damaged ZIP — it is an OLE2 compound document
    wrapping the encrypted package, and it announces itself in its first eight bytes. So a locked
    workbook gets the same sentence a locked PDF gets, instead of being accepted, stored, charged,
    and failing inside the sandbox several turns later where nothing can explain it.

    Mutation receipt: remove the `looks_password_protected` branch and the refusal becomes the
    generic "could not be read" one, which a citizen holding a file they know is fine cannot act
    on.
    """
    locked = _OLE2 + bytes(64)

    assert looks_password_protected(locked)
    refusal = code_lane_refusal(EXCEL_MEDIA_TYPE, "salaries.xlsx", locked)
    assert refusal is not None
    assert "password" in refusal.lower()


def test_the_password_check_runs_before_the_structure_check() -> None:
    """An encrypted file is ALSO a structurally invalid ZIP, so the order decides which sentence
    the citizen meets. "Remove the password" is something a person can do; "this file is damaged",
    about a file they know is fine, reads as the platform being broken."""
    refusal = code_lane_refusal(WORD_MEDIA_TYPE, "locked.docx", _OLE2 + bytes(64))

    assert refusal is not None
    assert "read" not in refusal.lower().split("password")[0]
    assert "password" in refusal.lower()


def test_delimited_files_are_admitted_on_their_extension() -> None:
    """★ CSV AND TSV HAVE NO MAGIC BYTES, and that is not a weaker check — it is the only check
    that exists for them. Any text is a valid CSV; inventing a signature would refuse real files.
    The extension is what distinguishes a `.tsv` from a `.csv`, and reading one on the wrong
    separator yields a single column that still looks plausible."""
    assert code_lane_refusal(CSV_MEDIA_TYPE, "movements.csv", b"a,b\n1,2\n") is None
    assert code_lane_refusal(TSV_MEDIA_TYPE, "movements.tsv", b"a\tb\n1\t2\n") is None
    assert code_lane_refusal(TSV_MEDIA_TYPE, "movements.tab", b"a\tb\n") is None

    # Declared as one, named as the other.
    assert code_lane_refusal(CSV_MEDIA_TYPE, "movements.xlsx", b"a,b\n") is not None
