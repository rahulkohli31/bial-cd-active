"""Hand-rolled PDF fixtures for the upload door.

WRITTEN BY HAND, NOT BY A LIBRARY, AND THAT IS THE POINT. What the door does to a PDF is now two
byte scans over the file's tail, so every property under test — where the trailer sits, whether it
declares encryption, whether the file ends where it says it does — has to be a fact of the FIXTURE
rather than of whatever wrote it. These emit raw PDF syntax: a header, a catalog, a page-tree node,
N page objects, and either a classic cross-reference table or a PDF 1.5 cross-reference stream.

THE ENCRYPTED ONES ARE HAND-WRITTEN TOO, WHICH THEY COULD NOT BE BEFORE. Three fixtures here used
to be built by `pypdf`, because the thing under test was a page count taken through `pypdf` and a
standard-security encryption dictionary is a key derivation rather than syntax. Nothing derives a
key any more: the check reads the trailer's `/Encrypt` ENTRY, which is syntax, and syntax is
hand-writable. The library went with the page cap.

They are generated rather than committed because a binary in the tree is a blob nobody can review,
and the interesting ones are a few hundred bytes whose whole meaning is their structure.
"""

from __future__ import annotations

import struct
import zlib

_HEADER = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
"""Magic + the binary comment. The upload route's existing check reads the first 18 bytes,
so every fixture here passes it — which is the precondition for the door's own scans to matter."""


def _assemble(objects: list[bytes], *, trailer_extra: bytes = b"") -> bytes:
    """`objects[i]` is the body of object `i + 1`; object 1 is the catalog. Emits the
    classic `xref` table + trailer that a conforming reader needs.

    `trailer_extra` is appended inside the trailer dictionary — which is how an encrypted
    document declares itself, and the only difference between a locked fixture and a plain one.
    """
    out = bytearray(_HEADER)
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number
        out += body
        out += b"\nendobj\n"
    xref_at = len(out)
    size = len(objects) + 1
    out += b"xref\n0 %d\n" % size
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R%s >>\nstartxref\n%d\n%%%%EOF\n" % (
        size,
        trailer_extra,
        xref_at,
    )
    return bytes(out)


def pdf_with_pages(pages: int) -> bytes:
    """An ordinary, valid PDF with exactly `pages` page objects (~100 bytes a page)."""
    kids = b" ".join(b"%d 0 R" % (index + 3) for index in range(pages))
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, pages),
    ]
    objects += [b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>"] * pages
    return _assemble(objects)


def scanned_pdf(pages: int = 2) -> bytes:
    """A page-image PDF: every page is one embedded image XObject and there is NO text at all.

    ★ A FIRST-CLASS SUPPORTED CASE, and the fixture that keeps the door's checks structural.
    A scanned invoice has a header, an EOF marker and a page tree like any other document, and the
    model reads it as vision. Anything at this door that reached for the file's TEXT would refuse
    it — so this is the file that would go red if either scan were ever "improved" into a text
    probe.
    """
    image = zlib.compress(bytes(64 * 64))
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [%s] /Count %d >>"
        % (b" ".join(b"%d 0 R" % (index + 3) for index in range(pages)), pages),
    ]
    objects += [
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /XObject << /Im0 %d 0 R >> >> >>" % (pages + 3)
    ] * pages
    objects.append(
        b"<< /Type /XObject /Subtype /Image /Width 64 /Height 64 /ColorSpace /DeviceGray "
        b"/BitsPerComponent 8 /Filter /FlateDecode /Length %d >>\nstream\n"
        % len(image)
        + image
        + b"\nendstream"
    )
    return _assemble(objects)


def unreadable_pdf() -> bytes:
    """Magic-valid bytes with no object structure and no terminator — the file that passes the
    18-byte prefix check and is plainly not a document. The shape a failed transfer takes."""
    return _HEADER + b"this file claims to be a PDF and is not\n"


def truncated_pdf(pages: int = 3) -> bytes:
    """A REAL PDF, CUT SHORT. Not rubbish — a valid document whose last quarter never arrived, so
    the cross-reference table and the `%%EOF` terminator are simply missing.

    This is what a dropped upload actually looks like, and it is the case the door's structural
    scan exists for: refused here, it costs the citizen one clear sentence; admitted, it is stored,
    counted against their conversation, and fails in front of the model turns later."""
    whole = pdf_with_pages(pages)
    return whole[: int(len(whole) * 0.75)]


def pdf_pointing_past_its_own_end() -> bytes:
    """A file that LOOKS complete — trailer, `startxref`, `%%EOF` all present — whose `startxref`
    names a byte offset beyond the end of the file.

    The second, subtler shape of a cut transfer: enough of the tail survived (or was re-appended)
    that the terminator is there, while the body it points into is not. Positive evidence, which
    is the only kind the structural scan acts on."""
    whole = pdf_with_pages(2)
    head, _, _ = whole.rpartition(b"startxref\n")
    return head + b"startxref\n%d\n%%%%EOF\n" % (len(whole) * 4)


def incrementally_updated_pdf(updates: int = 3) -> bytes:
    """A signed-or-annotated document: a base PDF with `updates` incremental sections appended,
    each with its own xref table, trailer and `%%EOF`.

    ★ THE FAIL-OPEN DIRECTION, and the reason the structural scan refuses only on evidence. A long
    update chain is exactly what signing and annotating produce, and it pushes the ORIGINAL trailer
    far from the end of the file — so a scan that demanded to recognise the whole structure would
    refuse a perfectly good document at the door. Only the last section's terminator is looked for,
    and this file has it."""
    out = bytearray(pdf_with_pages(2))
    for _ in range(updates):
        xref_at = len(out)
        out += b"xref\n0 1\n0000000000 65535 f \n"
        out += b"trailer\n<< /Size 5 /Root 1 0 R /Prev %d >>\nstartxref\n%d\n%%%%EOF\n" % (
            xref_at // 2,
            xref_at,
        )
    return bytes(out)


def encrypted_pdf(pages: int = 3) -> bytes:
    """A SHORT, VALID, PASSWORD-PROTECTED PDF in the classic trailer form.

    ★ THE ONE THING THE DOOR READS IS THE TRAILER'S `/Encrypt` ENTRY, which the spec requires to
    be an indirect reference — so the fixture only has to DECLARE encryption, and declaring is
    syntax. The standard-security dictionary below is real in shape (filter, revision, key length,
    permission mask) and its key derivation is never exercised by anything, which is the whole
    reason this fixture can be hand-written now and could not be before.

    Deliberately SHORT: the point is that the document's length is fine and the citizen still
    cannot get past a refusal, so the refusal has to talk about the password and nothing else."""
    kids = b" ".join(b"%d 0 R" % (index + 3) for index in range(pages))
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, pages),
    ]
    objects += [b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>"] * pages
    objects.append(
        b"<< /Filter /Standard /V 2 /R 3 /Length 128 /P -3904 "
        b"/O <" + b"ab" * 32 + b"> /U <" + b"cd" * 32 + b"> >>"
    )
    return _assemble(objects, trailer_extra=b" /Encrypt %d 0 R" % len(objects))


def encrypted_xref_stream_pdf(pages: int = 3) -> bytes:
    """AN ENCRYPTED PDF WITH NO `trailer` KEYWORD ANYWHERE IN IT — PDF 1.5+, cross-reference
    stream, which is what Word and Acrobat emit.

    ★ THIS IS THE FALSE-NEGATIVE FIXTURE, and the reason the door's lock scan is keyed on the
    `/Encrypt` entry rather than on the word `trailer`. A trailer-keyword scan finds nothing at all
    in a file of this shape — so it would wave through the encrypted document a citizen is most
    likely to actually have, while correctly refusing the hand-made one above. The two must both
    be refused or the check is theatre.

    The catalog, page tree and pages live inside a compressed object stream, so none of them
    appear as literal bytes; the only plaintext dictionary in the file is the XRef stream's own,
    and that is where `/Encrypt` sits."""
    bodies = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [%s] /Count %d >>"
        % (b" ".join(b"%d 0 R" % (index + 3) for index in range(pages)), pages),
    ]
    bodies += [b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>"] * pages
    count = len(bodies)
    objstm_number = count + 1
    encrypt_number = count + 2
    xref_number = count + 3

    pairs = bytearray()
    payload = bytearray()
    for number, body in enumerate(bodies, start=1):
        pairs += b"%d %d " % (number, len(payload))
        payload += body + b" "
    compressed = zlib.compress(bytes(pairs) + bytes(payload), 9)

    out = bytearray(b"%PDF-1.5\n%\xe2\xe3\xcf\xd3\n")
    objstm_at = len(out)
    out += (
        b"%d 0 obj\n<< /Type /ObjStm /N %d /First %d /Filter /FlateDecode /Length %d >>\n"
        b"stream\n" % (objstm_number, count, len(pairs), len(compressed))
    )
    out += compressed
    out += b"\nendstream\nendobj\n"
    encrypt_at = len(out)
    out += (
        b"%d 0 obj\n<< /Filter /Standard /V 2 /R 3 /Length 128 /P -3904 "
        b"/O <" + b"ab" * 32 + b"> /U <" + b"cd" * 32 + b"> >>\nendobj\n"
    ) % encrypt_number

    def entry(kind: int, first: int, second: int) -> bytes:
        return bytes([kind]) + struct.pack(">I", first) + struct.pack(">H", second)

    entries = bytearray(entry(0, 0, 65535))
    for index in range(count):
        entries += entry(2, objstm_number, index)  # type 2: inside the object stream
    entries += entry(1, objstm_at, 0)
    entries += entry(1, encrypt_at, 0)
    xref_at = len(out)
    entries += entry(1, xref_at, 0)
    xref_payload = zlib.compress(bytes(entries), 9)
    out += (
        b"%d 0 obj\n<< /Type /XRef /Size %d /W [1 4 2] /Root 1 0 R /Encrypt %d 0 R"
        b" /Filter /FlateDecode /Length %d >>\nstream\n"
        % (xref_number, xref_number + 1, encrypt_number, len(xref_payload))
    )
    out += xref_payload
    out += b"\nendstream\nendobj\nstartxref\n%d\n%%%%EOF\n" % xref_at
    return bytes(out)


def pdf_mentioning_encrypt_in_its_content() -> bytes:
    """A perfectly ordinary, UNENCRYPTED document whose page content is prose about encryption —
    so the literal bytes `/Encrypt` appear in the file, uncompressed, near the end.

    ★ THE FALSE-POSITIVE FIXTURE. A bare substring scan calls this locked and tells its owner to
    remove a password that does not exist — advice that leads nowhere, about a file that is fine.
    The door's scan matches the trailer's `/Encrypt <num> <gen> R` INDIRECT REFERENCE, which is the
    form the spec requires and the form running prose does not take."""
    text = (
        b"BT /F1 12 Tf 72 720 Td (A PDF declares encryption with the /Encrypt trailer key.) Tj ET"
    )
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n" % len(text) + text + b"\nendstream",
    ]
    return _assemble(objects)


def xref_bomb_pdf(entries: int = 8_000_000) -> bytes:
    """A tiny PDF that costs a PARSER seconds per kilobyte: a Flate-compressed cross-reference
    STREAM declaring `entries` entries, whose payload is a few kilobytes of zeros.

    The document itself is one blank page. What was hostile is the index: a reader must walk every
    declared entry before it can resolve the catalog, at a cost LINEAR IN `entries` and FLAT IN
    MEMORY. At the default it is ~32 KB of upload for ~6 seconds of parsing — inside every size
    cap, unbounded in the only axis they watch.

    ★ IT IS KEPT AS THE RECEIPT THAT NOTHING PARSES ANY MORE. This file is why the door used to
    spawn a killable, memory-capped child for every PDF; with the page cap gone, nothing here
    opens a PDF at all, and the fixture's job is now to prove that removing the governor did not
    reopen what the governor was for."""
    out = bytearray(_HEADER)

    def add(number: int, body: bytes) -> None:
        out.extend(b"%d 0 obj\n" % number)
        out.extend(body)
        out.extend(b"\nendobj\n")

    add(1, b"<< /Type /Catalog /Pages 2 0 R >>")
    add(2, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    add(3, b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>")
    payload = zlib.compress(bytes(entries * 4), 9)
    xref_at = len(out)
    out.extend(
        b"4 0 obj\n<< /Type /XRef /Size %d /Index [0 %d] /W [1 2 1] /Root 1 0 R"
        b" /Filter /FlateDecode /Length %d >>\nstream\n" % (entries, entries, len(payload))
    )
    out.extend(payload)
    out.extend(b"\nendstream\nendobj\nstartxref\n%d\n%%%%EOF\n" % xref_at)
    return bytes(out)
