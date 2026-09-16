"""POST/GET/DELETE /v1/attachments — validation, magic bytes, quota, owner-scoped
keys, rate limit. Byte-stable with the Express `/api/attachments` contract.
"""

from __future__ import annotations

import base64
import datetime
import io
import struct
import time
import uuid
import zipfile

from openpyxl import Workbook
from sqlalchemy import select
from structlog.testing import capture_logs

from src.api.v1.attachments.router import (
    ATTACHMENT_LANES_SENTENCE,
    ATTACHMENT_MAX_BYTES,
    ATTACHMENT_MAX_MB,
    MAX_ATTACHMENTS_PER_CONVERSATION,
)
from src.config import settings
from src.db.models.attachment import Attachment
from src.services.attachments import reclaim_orphaned_attachments
from src.services.auth.session_jwt import mint_session_jwt
from src.services.media.lanes import EXCEL_MEDIA_TYPE, PASSWORD_PROTECTED_TEXT
from tests.factories import ConversationFactory, ProjectFactory, UserFactory
from tests.pdfs import (
    encrypted_pdf,
    encrypted_xref_stream_pdf,
    incrementally_updated_pdf,
    pdf_mentioning_encrypt_in_its_content,
    pdf_pointing_past_its_own_end,
    pdf_with_pages,
    scanned_pdf,
    truncated_pdf,
    unreadable_pdf,
    xref_bomb_pdf,
)

_TTL = settings.auth.access_ttl_seconds

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
# A REAL one-page PDF, not just the magic prefix: the door checks a PDF for structural wholeness,
# so magic-valid rubbish is refused rather than stored. `unreadable_pdf()` is that case, tested by
# name below.
_PDF = pdf_with_pages(1)


def _lying_zip(entries: dict[str, bytes], declared_uncompressed: int) -> bytes:
    """A real archive whose central directory DECLARES a huge uncompressed size.

    The bomb pre-filter sums the declared sizes and never inflates to check, precisely because
    they are attacker-controllable - so an overstated size is the threat, not a cheat.
    """
    raw = _zip_with(entries)
    cdh = raw.index(bytes([0x50, 0x4B, 0x01, 0x02]))  # first central-directory header
    return raw[: cdh + 24] + struct.pack("<I", declared_uncompressed) + raw[cdh + 28 :]


def _zip_with(entries: dict[str, bytes]) -> bytes:
    """A real ZIP. The code lane runs `assert_zip_not_bomb`, which reads the archive's own central
    directory, so a hand-built PK prefix is refused before any structure check."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for entry_name, body in entries.items():
            archive.writestr(entry_name, body)
    return buffer.getvalue()


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _cookie(jwt: str) -> dict[str, str]:
    return {"Cookie": f"session={jwt}"}


async def _auth(db_session):
    """A signed-in user, and A CONVERSATION TO UPLOAD AGAINST.

    ★ THE SECOND HALF IS NOT CONVENIENCE. `conversationId` is required at the door now — an
    upload names the chat it belongs to, because that is what makes the per-conversation count
    answerable at the door and leaves no file without an owner. Every test that uploads therefore
    needs a real, owned, already-written conversation, so this returns one rather than letting
    forty-odd tests each mint their own.

    The tests that deliberately upload WITHOUT one (or against a stranger's) build their bodies by
    hand and are named for it.
    """
    user = await UserFactory.create(db_session)
    conv = await ConversationFactory.create(db_session, user.id)
    return _cookie(mint_session_jwt(user.id, user.token_version, _TTL)), user, conv


# --- upload happy path + download ---------------------------------------------


async def test_upload_image_then_download(client, db_session, fake_storage) -> None:
    headers, user, conv = await _auth(db_session)
    resp = await client.post(
        "/v1/attachments",
        headers=headers,
        json={
            "conversationId": str(conv.id),
            "attachmentId": "att_1",
            "name": "shot.png",
            "mediaType": "image/png",
            "base64": _b64(_PNG),
        },
    )
    assert resp.status_code == 201
    att = resp.json()["attachment"]
    assert att["attachmentId"] == "att_1"
    assert att["mediaType"] == "image/png"
    assert att["size"] == len(_PNG)
    assert att["name"] == "shot.png"
    assert att["kind"] == "image"
    assert att["key"].startswith(f"att/{user.id}/")

    dl = await client.get("/v1/attachments/att_1", headers=headers)
    assert dl.status_code == 200
    assert dl.content == _PNG
    assert dl.headers["content-type"] == "image/png"
    assert dl.headers["cache-control"] == "private, max-age=3600"


async def test_upload_pdf_is_document_kind(client, db_session) -> None:
    headers, _, conv = await _auth(db_session)
    resp = await client.post(
        "/v1/attachments",
        headers=headers,
        json={
            "conversationId": str(conv.id),
            "attachmentId": "att_pdf",
            "mediaType": "application/pdf",
            "base64": _b64(_PDF),
        },
    )
    assert resp.status_code == 201
    assert resp.json()["attachment"]["kind"] == "document"


# --- upload validation --------------------------------------------------------


async def test_wrong_magic_rejected(client, db_session) -> None:
    headers, _, conv = await _auth(db_session)
    resp = await client.post(
        "/v1/attachments",
        headers=headers,
        json={
            "conversationId": str(conv.id),
            "attachmentId": "att_x",
            "mediaType": "image/png",
            "base64": _b64(_PDF),
        },
    )
    assert resp.status_code == 400
    assert resp.json() == {
        "error": {"message": "Attachment bytes do not match the declared type image/png."}
    }


async def test_unsupported_type_rejected(client, db_session) -> None:
    headers, _, conv = await _auth(db_session)
    resp = await client.post(
        "/v1/attachments",
        headers=headers,
        json={
            "conversationId": str(conv.id),
            "attachmentId": "att_x",
            "mediaType": "image/tiff",
            "base64": _b64(_PNG),
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["message"].startswith("Unsupported attachment type: image/tiff.")


async def test_text_type_rejected(client, db_session) -> None:
    headers, _, conv = await _auth(db_session)
    resp = await client.post(
        "/v1/attachments",
        headers=headers,
        json={
            "conversationId": str(conv.id),
            "attachmentId": "att_x",
            "mediaType": "text/plain",
            "base64": _b64(b"hi"),
        },
    )
    # `text/plain` STAYS REFUSED, and that is a withdrawal rather than an oversight: it works on
    # the branch today and stops. The mechanism argument died with the inline lane — under the
    # routing rule a .txt is simply a file that code reads, exactly like a .csv — so the refusal
    # rests on the surviving reason alone: no client requirement names it, and every format costs
    # a reader arm, refusal copy, a test and a line in the help page.
    assert resp.status_code == 400
    assert "not supported" in resp.json()["error"]["message"]
    # And it carries the ONE sentence, so a citizen meets the same words here as in the composer.
    assert ATTACHMENT_LANES_SENTENCE in resp.json()["error"]["message"]


async def test_a_csv_is_no_longer_refused_as_inline_text(client, db_session, fake_storage) -> None:
    """★ THE `text/*` REFUSAL INVERTS FOR THE DELIMITED FORMATS.

    It used to refuse every text type because text rode inside the prompt rather than being
    uploaded. That lane is gone: every attachment is an uploaded file with a stored identity,
    which is what lets a chip be rebuilt on reload for every format by one fix.
    """
    headers, _, conv = await _auth(db_session)

    resp = await client.post(
        "/v1/attachments",
        headers=headers,
        json={
            "conversationId": str(conv.id),
            "attachmentId": "att_csv",
            "name": "movements.csv",
            "mediaType": "text/csv",
            "base64": _b64(b"badge,name\n1,Asha\n"),
        },
    )

    assert resp.status_code == 201, resp.text


async def test_a_workbook_is_admitted_and_a_renamed_archive_is_not(
    client, db_session, fake_storage
) -> None:
    """All OOXML shares the ZIP signature, so the OPC part is the only discriminator there is —
    without it a renamed `.zip` is stored as a workbook the reader then cannot open."""
    headers, _, conv = await _auth(db_session)
    # A REAL archive. `assert_zip_not_bomb` runs on this lane and reads the ZIP own
    # central directory, so a hand-built PK prefix is refused before any structure
    # check even matters - the guard working, not a fixture problem.
    workbook = _zip_with({"xl/workbook.xml": b"<workbook/>"})

    ok = await client.post(
        "/v1/attachments",
        headers=headers,
        json={
            "conversationId": str(conv.id),
            "attachmentId": "att_xlsx",
            "name": "book.xlsx",
            "mediaType": EXCEL_MEDIA_TYPE,
            "base64": _b64(workbook),
        },
    )
    assert ok.status_code == 201, ok.text

    refused = await client.post(
        "/v1/attachments",
        headers=headers,
        json={
            "conversationId": str(conv.id),
            "attachmentId": "att_zip",
            "name": "book.xlsx",
            "mediaType": EXCEL_MEDIA_TYPE,
            "base64": _b64(_zip_with({"readme.txt": b"not a workbook"})),
        },
    )
    assert refused.status_code == 415, refused.text


async def test_a_password_protected_workbook_is_refused_at_the_door(
    client, db_session, fake_storage
) -> None:
    """★ R6/AE8c. A locked workbook gets the same treatment a locked PDF gets — refused before
    anything is stored, with the password named — rather than being accepted, charged, and failing
    inside the sandbox several turns later where nothing can explain it."""
    headers, _, conv = await _auth(db_session)
    locked = bytes([0xD0, 0xCF, 0x11, 0xE0, 0xA1, 0xB1, 0x1A, 0xE1]) + bytes(64)

    resp = await client.post(
        "/v1/attachments",
        headers=headers,
        json={
            "conversationId": str(conv.id),
            "attachmentId": "att_locked",
            "name": "salaries.xlsx",
            "mediaType": EXCEL_MEDIA_TYPE,
            "base64": _b64(locked),
        },
    )

    assert resp.status_code == 415, resp.text
    assert "password" in resp.json()["error"]["message"].lower()
    assert fake_storage.objects == {}  # refused BEFORE the store


async def test_invalid_id_rejected(client, db_session) -> None:
    headers, _, conv = await _auth(db_session)
    resp = await client.post(
        "/v1/attachments",
        headers=headers,
        json={
            "conversationId": str(conv.id),
            "attachmentId": "bad id!",
            "mediaType": "image/png",
            "base64": _b64(_PNG),
        },
    )
    assert resp.status_code == 400
    assert resp.json() == {"error": {"message": "Invalid attachment id."}}


async def test_missing_media_type_rejected(client, db_session) -> None:
    headers, _, conv = await _auth(db_session)
    resp = await client.post(
        "/v1/attachments",
        headers=headers,
        json={"conversationId": str(conv.id), "attachmentId": "att_x", "base64": _b64(_PNG)},
    )
    assert resp.status_code == 400
    assert resp.json() == {"error": {"message": "mediaType is required."}}


async def test_exactly_at_the_size_cap_is_accepted_and_one_byte_over_is_not(
    client, db_session, fake_storage
) -> None:
    """★ THE BOUNDARY, ON BOTH SIDES, AND THE NUMBER READ FROM THE CONSTANT.

    This test used to spell "4 MB" into its own assertion, so the day the cap moved it went red
    for the right reason with completely the wrong message — and it is not findable by grepping
    for the symbol, which is how a hardcoded figure survives a rename. Sized and asserted from
    `ATTACHMENT_MAX_BYTES` now, so the boundary follows the cap wherever it goes.

    The accepted side matters as much as the refused one: an off-by-one that refuses a file
    exactly at the cap is the same defect wearing the other sign.
    """
    headers, _, conv = await _auth(db_session)
    at_cap = b"\x89PNG\r\n\x1a\n" + b"\x00" * (ATTACHMENT_MAX_BYTES - 8)
    ok = await client.post(
        "/v1/attachments",
        headers=headers,
        json={
            "conversationId": str(conv.id),
            "attachmentId": "att_at_cap",
            "mediaType": "image/png",
            "base64": _b64(at_cap),
        },
    )
    assert ok.status_code == 201

    resp = await client.post(
        "/v1/attachments",
        headers=headers,
        json={
            "conversationId": str(conv.id),
            "attachmentId": "att_big",
            "mediaType": "image/png",
            "base64": _b64(at_cap + b"\x00"),
        },
    )
    assert resp.status_code == 413
    assert resp.json() == {
        "error": {"message": f"Attachment is too large (max {ATTACHMENT_MAX_MB} MB)."}
    }


async def test_a_request_body_over_the_wire_ceiling_is_refused_before_it_is_buffered(
    client, db_session
) -> None:
    """★ A BRANCH NOTHING COVERED, on a constant this work moved.

    The wire ceiling is a different question from the size cap: it fires on the declared
    Content-Length before the body is read at all, so it is what stops a hostile request being
    buffered into memory. It therefore has to clear base64 of a legal file — 10 MiB encodes to
    13,981,016 bytes — and a ceiling set too low would refuse files the door means to accept,
    with a sentence about the REQUEST rather than about the file, and no test to say so.

    Sent with a Content-Length header and a tiny body: the branch reads the header, so the test
    does not have to move fifteen megabytes to reach it.
    """
    headers, _, conv = await _auth(db_session)
    resp = await client.post(
        "/v1/attachments",
        headers={**headers, "content-length": str(64 * 1024 * 1024)},
        content=b"{}",
    )
    assert resp.status_code == 413
    assert resp.json() == {"error": {"message": "Attachment request is too large."}}


async def test_the_wire_ceiling_clears_a_legal_file_encoded(
    client, db_session, fake_storage
) -> None:
    """The other side of the same constant, and the one that would fail silently: a cap-sized file
    base64-encodes to about 13.3 MB on the wire, so a ceiling below that refuses every maximum
    upload before it is even decoded."""
    headers, _, conv = await _auth(db_session)
    at_cap = b"\x89PNG\r\n\x1a\n" + b"\x00" * (ATTACHMENT_MAX_BYTES - 8)
    body = _b64(at_cap)
    assert len(body) > ATTACHMENT_MAX_BYTES  # base64 really is bigger than the file
    resp = await client.post(
        "/v1/attachments",
        headers=headers,
        json={
            "conversationId": str(conv.id),
            "attachmentId": "att_wire",
            "mediaType": "image/png",
            "base64": body,
        },
    )
    assert resp.status_code == 201


async def test_a_zip_bomb_is_refused_on_the_upload_lane(client, db_session, fake_storage) -> None:
    """★ THE BOUND IS WIRED, NOT MERELY PRESENT.

    `assert_zip_not_bomb` had three callers, all server-side extraction arms this work deletes,
    and its own suite calls the function DIRECTLY — so that suite proves the algorithm and would
    have stayed green through the guard going completely unwired. The two tests that did prove
    wiring rode the very office kinds being removed.

    This is its replacement on the lane that now carries archives. A 4 MB `.xlsx` can declare 300
    MB uncompressed, and the new path is strictly more exposed than the old one: the office lane
    extracted inside a killable, memory-capped subprocess and never stored a file it could not
    read, while this one stores the archive and hands it to a reader in the citizen's own sandbox,
    where neither that ceiling nor that deadline reaches.

    Mutation receipt: remove the `assert_zip_not_bomb` call from the upload lane and this is the
    only test that goes red.
    """
    headers, _, conv = await _auth(db_session)
    bomb = _lying_zip({"xl/workbook.xml": b"<workbook/>"}, 400 * 1024 * 1024)

    resp = await client.post(
        "/v1/attachments",
        headers=headers,
        json={
            "conversationId": str(conv.id),
            "attachmentId": "att_bomb",
            "name": "book.xlsx",
            "mediaType": EXCEL_MEDIA_TYPE,
            "base64": _b64(bomb),
        },
    )

    assert resp.status_code == 413, resp.text
    assert fake_storage.objects == {}  # refused BEFORE the store


async def test_the_conversation_count_cap_holds_for_a_chat_not_written_yet(
    client, db_session, fake_storage
) -> None:
    """★ THE CAP WAS BYPASSABLE ON THE ORDINARY PATH.

    It was gated on `conversation_id is not None`, and an upload whose chat has no row yet stores
    NULL — which, since the first message of every new chat does exactly that, is the common case
    rather than a contrived one. Anything uploading without a link was uncapped.

    It counts the UNLINKED POOL — files that are also still unsent — which is the same population
    the byte budget uses for that case; each file leaves the pool when its message is sent.

    Mutation receipt: restore the `conversation_id is not None` gate and the 21st upload is
    accepted.
    """
    headers, _, conv = await _auth(db_session)
    for index in range(MAX_ATTACHMENTS_PER_CONVERSATION):
        resp = await client.post(
            "/v1/attachments",
            headers=headers,
            json={
                "conversationId": str(conv.id),
                "attachmentId": f"att_unlinked_{index}",
                "name": "shot.png",
                "mediaType": "image/png",
                "base64": _b64(_PNG),
            },
        )
        assert resp.status_code == 201, resp.text

    over = await client.post(
        "/v1/attachments",
        headers=headers,
        json={
            "conversationId": str(conv.id),
            "attachmentId": "att_unlinked_over",
            "name": "shot.png",
            "mediaType": "image/png",
            "base64": _b64(_PNG),
        },
    )

    assert over.status_code == 413, over.text
    assert over.json()["error"]["code"] == "CONVERSATION_ATTACHMENTS_FULL"


async def test_twenty_files_sent_elsewhere_do_not_block_a_new_chats_first_upload(
    client, db_session, fake_storage
) -> None:
    """★ THE BUDGET USED TO BE THE WHOLE ACCOUNT.

    Twenty files in old chats, and the next chat's first upload was refused with "This conversation
    has reached its limit of 20 attachments" — on a chat holding zero, naming "start a new chat" as
    the remedy, which was the one move that could not help. The demo account had already been past
    it from rehearsal.

    THE FIXTURE MOVED WITH THE ORDERING, and the claim did not. It used to send NO conversationId,
    because the first upload of a new chat happened before its row existed; the chat is created
    first now, so the same question is asked with a real, empty conversation. What is being
    asserted either way is that fullness is a fact about one chat rather than about the citizen.

    Mutation receipt: put the account-wide scope back and this upload 413s.
    """
    headers, user, fresh = await _auth(db_session)
    elsewhere = await _a_conversation(db_session, user)
    await _fill_conversation(db_session, user, elsewhere.id, held=MAX_ATTACHMENTS_PER_CONVERSATION)

    resp = await _upload_into(client, headers, fresh.id, "att_new_chat_first")

    assert resp.status_code == 201, resp.text


async def test_a_csv_is_not_run_through_the_archive_bound(
    client, db_session, fake_storage
) -> None:
    """★ THE CODE LANE IS NOT ALL ARCHIVES, and gating the zip-bomb check on the whole lane
    refused every CSV and TSV at the door.

    The refusal read "Malformed archive (no ZIP end-of-central-directory)" — a true statement
    about a file that was never supposed to be an archive, and unactionable advice to a citizen
    holding an ordinary spreadsheet export. Found by attaching one in the real UI; the test above
    stayed green throughout, because it only ever fed the check an `.xlsx`.

    A delimited file is bytes of text with no central directory to bound. The size cap is its
    bound, and the OOXML half keeps the archive check (asserted directly above).

    Mutation receipt: gate on `is_code_lane` instead of `is_opc_archive` and this goes red with a
    413 naming a ZIP.
    """
    headers, _, conv = await _auth(db_session)

    resp = await client.post(
        "/v1/attachments",
        headers=headers,
        json={
            "conversationId": str(conv.id),
            "attachmentId": "att_plaincsv",
            "name": "movements.csv",
            "mediaType": "text/csv",
            "base64": _b64(b"badge,name,terminal\n1,Asha,T1\n2,Ravi,T2\n"),
        },
    )

    assert resp.status_code == 201, resp.text
    assert fake_storage.objects  # stored, not refused


# --- the conversation-scoped budgets ---------------------------


async def _a_conversation(db_session, user):
    project = await ProjectFactory.create(db_session, user.id)
    return await ConversationFactory.create(db_session, user.id, project_id=project.id)


async def _upload_into(client, headers, conversation_id, attachment_id, *, size=None):
    data = _PNG if size is None else _PNG + b"\x00" * (size - len(_PNG))
    return await client.post(
        "/v1/attachments",
        headers=headers,
        json={
            "attachmentId": attachment_id,
            "mediaType": "image/png",
            "base64": _b64(data),
            "conversationId": str(conversation_id),
        },
    )


async def _fill_conversation(db_session, user, conversation_id, *, held: int) -> None:
    """Put `held` attachments in a conversation, without going through the door."""
    for index in range(held):
        db_session.add(
            Attachment(
                user_id=user.id,
                attachment_id=f"att_seed_{conversation_id.hex[:6]}_{index}",
                media_type="image/png",
                name="",
                size=len(_PNG),
                storage_key=f"att/{user.id}/seed-{conversation_id.hex[:6]}-{index}",
                conversation_id=conversation_id,
            )
        )
    await db_session.flush()


async def test_a_full_conversation_does_not_exhaust_the_account(
    client, db_session, fake_storage
) -> None:
    """The whole point of scoping the budget to the conversation.

    It used to count every attachment a citizen had ever uploaded, across every conversation, so
    two or three working sessions exhausted a lifetime allowance and the only way to reclaim any
    was to delete whole conversations. Now a full chat is a full chat: the next one has room, and
    "start a new chat" is advice that actually works.

    RE-POINTED FROM BYTES TO A COUNT, not deleted. The per-conversation BYTE budget is gone and a
    flat file count replaced it, but what this test is about — that fullness is a fact about one
    chat and not about the account — is the same claim either way.

    Mutation receipt: drop the conversation predicate from the count query and the second upload
    413s on files the other conversation is holding.
    """
    headers, user, conv = await _auth(db_session)
    first = await _a_conversation(db_session, user)
    await _fill_conversation(db_session, user, first.id, held=MAX_ATTACHMENTS_PER_CONVERSATION)

    # The conversation holding them is full...
    refused = await _upload_into(client, headers, first.id, "att_more")
    assert refused.status_code == 413, refused.text
    assert refused.json()["error"]["code"] == "CONVERSATION_ATTACHMENTS_FULL"

    # ...and a new one has room. This is the assertion the old per-citizen budget could not pass.
    second = await _a_conversation(db_session, user)
    accepted = await _upload_into(client, headers, second.id, "att_fresh")
    assert accepted.status_code == 201, accepted.text


async def test_a_full_conversation_says_so_and_names_a_way_out(
    client, db_session, fake_storage
) -> None:
    """AE21. The old copy was "Attachment storage is full. Remove some attachments and try
    again." — advice a citizen cannot follow, because nothing lets them remove one attachment
    from an old conversation. The refusal names the thing that is full and the thing that
    works."""
    headers, user, conv = await _auth(db_session)
    conversation = await _a_conversation(db_session, user)
    await _fill_conversation(
        db_session, user, conversation.id, held=MAX_ATTACHMENTS_PER_CONVERSATION
    )

    resp = await _upload_into(client, headers, conversation.id, "att_more")

    message = resp.json()["error"]["message"]
    assert "new chat" in message.lower()
    assert "remove some attachments" not in message.lower()


async def test_bytes_far_over_the_deleted_budget_are_accepted_while_the_count_is_under(
    client, db_session, fake_storage
) -> None:
    """★ THE BYTE BUDGET IS REALLY GONE, and this is the only test that can say so.

    Fifty megabytes used to be the per-conversation ceiling, and every other test here would stay
    green with it restored — they all sit well under it. This one seeds a conversation holding far
    more than that and uploads into it successfully, so the deleted rule cannot return unnoticed.

    Mutation check: restore the `sum(Attachment.size)` budget and its refusal, and this goes red.
    """
    headers, user, conv = await _auth(db_session)
    conversation = await _a_conversation(db_session, user)
    db_session.add(
        Attachment(
            user_id=user.id,
            attachment_id="att_enormous",
            media_type="image/png",
            name="",
            size=400 * 1024 * 1024,
            storage_key=f"att/{user.id}/enormous",
            conversation_id=conversation.id,
        )
    )
    await db_session.flush()

    accepted = await _upload_into(client, headers, conversation.id, "att_next")

    assert accepted.status_code == 201, accepted.text


async def test_re_uploading_a_known_id_into_a_full_conversation_is_refused(
    client, db_session, fake_storage
) -> None:
    """★ THE GUARD THAT KEEPS THE COUNT REAL, and the bypass it closes is one line wide.

    A row is looked up by (owner, attachment id) with NO conversation filter, so a known id
    re-uploaded against a DIFFERENT conversation used to find `existing`, skip the count entirely
    and move the row into a chat already holding twenty. The count cap says twenty; that path made
    it twenty-one, twenty-two, and so on, with no refusal at any point.

    Mutation check: narrow the predicate back to `if existing is None:` and this goes red.
    """
    headers, user, conv = await _auth(db_session)
    roomy = await _a_conversation(db_session, user)
    full = await _a_conversation(db_session, user)
    await _fill_conversation(db_session, user, full.id, held=MAX_ATTACHMENTS_PER_CONVERSATION)

    # A real upload into a chat with room — this is the row the re-upload will try to move.
    first = await _upload_into(client, headers, roomy.id, "att_x")
    assert first.status_code == 201, first.text

    moved = await _upload_into(client, headers, full.id, "att_x")

    assert moved.status_code == 413, moved.text
    assert moved.json()["error"]["code"] == "CONVERSATION_ATTACHMENTS_FULL"


async def test_the_conversation_attachment_count_is_enforced_on_the_server(
    client, db_session, fake_storage
) -> None:
    """AE18 — a cap a reload cannot clear.

    The browser has had this number since the beginning and it was never enforced here: the
    portal tallies attachments by walking the messages it has loaded, so the count reset to zero
    on every refresh. Nothing on the server disagreed, because nothing on the server counted.

    Mutation receipt: remove the count check and the twenty-first upload is accepted.
    """
    headers, user, conv = await _auth(db_session)
    conversation = await _a_conversation(db_session, user)
    for index in range(MAX_ATTACHMENTS_PER_CONVERSATION):
        db_session.add(
            Attachment(
                user_id=user.id,
                attachment_id=f"att_{index}",
                media_type="image/png",
                name="",
                size=len(_PNG),
                storage_key=f"att/{user.id}/{index}",
                conversation_id=conversation.id,
            )
        )
    await db_session.flush()

    resp = await _upload_into(client, headers, conversation.id, "att_one_too_many")

    assert resp.status_code == 413, resp.text
    assert resp.json()["error"]["code"] == "CONVERSATION_ATTACHMENTS_FULL"


async def test_re_uploading_a_file_the_conversation_already_holds_is_not_a_new_one(
    client, db_session, fake_storage
) -> None:
    """The count must not refuse an idempotent retry. A re-upload of the same id replaces its
    row rather than adding one, so counting it would break the retry the upload path is
    explicitly built to allow — a network hiccup mid-send would then wedge a full conversation
    permanently."""
    headers, user, conv = await _auth(db_session)
    conversation = await _a_conversation(db_session, user)
    for index in range(MAX_ATTACHMENTS_PER_CONVERSATION - 1):
        db_session.add(
            Attachment(
                user_id=user.id,
                attachment_id=f"att_{index}",
                media_type="image/png",
                name="",
                size=len(_PNG),
                storage_key=f"att/{user.id}/{index}",
                conversation_id=conversation.id,
            )
        )
    await db_session.flush()

    first = await _upload_into(client, headers, conversation.id, "att_last")
    assert first.status_code == 201, first.text

    again = await _upload_into(client, headers, conversation.id, "att_last")

    assert again.status_code == 201, again.text


# --- download / delete ownership ----------------------------------------------


async def test_download_missing_404(client, db_session) -> None:
    headers, _, conv = await _auth(db_session)
    resp = await client.get("/v1/attachments/att_nope", headers=headers)
    assert resp.status_code == 404
    assert resp.json() == {"error": {"message": "Attachment not found."}}


async def test_download_cross_user_denied(client, db_session) -> None:
    headers_a, _, conv_a = await _auth(db_session)
    resp = await client.post(
        "/v1/attachments",
        headers=headers_a,
        json={
            "conversationId": str(conv_a.id),
            "attachmentId": "att_shared",
            "mediaType": "image/png",
            "base64": _b64(_PNG),
        },
    )
    assert resp.status_code == 201
    # User B has no attachment with that id — owner-scoped lookup returns 404.
    headers_b, _, _ = await _auth(db_session)
    resp_b = await client.get("/v1/attachments/att_shared", headers=headers_b)
    assert resp_b.status_code == 404


async def test_delete_removes_object_and_row(client, db_session, fake_storage) -> None:
    headers, user, conv = await _auth(db_session)
    await client.post(
        "/v1/attachments",
        headers=headers,
        json={
            "conversationId": str(conv.id),
            "attachmentId": "att_del",
            "mediaType": "image/png",
            "base64": _b64(_PNG),
        },
    )
    assert len(fake_storage.objects) == 1

    resp = await client.delete("/v1/attachments/att_del", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert fake_storage.objects == {}
    row = await db_session.scalar(
        select(Attachment).where(
            Attachment.user_id == user.id, Attachment.attachment_id == "att_del"
        )
    )
    assert row is None

    # A SECOND DELETE OF THE SAME ID IS STILL 200. The composer retries on a dropped response,
    # and a 404 on the retry would tell the citizen the delete failed when it had succeeded.
    again = await client.delete("/v1/attachments/att_del", headers=headers)
    assert again.status_code == 200
    assert again.json() == {"ok": True}


async def test_a_blob_the_sweep_could_not_remove_is_recorded_against_its_id(
    client, db_session, fake_storage, monkeypatch
) -> None:
    """★ U8 — THE ROW GOES FIRST, AND THE LEAK IS WRITTEN DOWN.

    Deleting the object before committing the row meant a commit failure left a row pointing at
    a blob that was already gone: the chip stays in the composer and the file opens to nothing.
    The order is reversed, which trades that loud dead row for a silent orphaned object — and
    the trade is only defensible because nothing else can find that object afterwards.
    `reclaim_orphaned_attachments` is row-driven and no listing pass over the object store
    exists anywhere, so a survivor that is not logged here is a leak nobody will ever see.

    Mutation receipt: drop the `if survived:` log and this goes red on the empty log list, while
    the delete still answers 200 — which is exactly how invisible the leak would be.
    """
    headers, user, conv = await _auth(db_session)
    await client.post(
        "/v1/attachments",
        headers=headers,
        json={
            "conversationId": str(conv.id),
            "attachmentId": "att_stuck",
            "mediaType": "image/png",
            "base64": _b64(_PNG),
        },
    )

    async def _refuse(key: str) -> None:
        raise RuntimeError("the object store is unreachable")

    monkeypatch.setattr(fake_storage, "delete", _refuse)

    with capture_logs() as logs:
        resp = await client.delete("/v1/attachments/att_stuck", headers=headers)

    # THE CITIZEN'S SIDE IS UNAFFECTED: the row is gone and the delete succeeded. A sweep that
    # surfaced its failure would 500 an already-committed delete.
    assert resp.status_code == 200
    row = await db_session.scalar(
        select(Attachment).where(
            Attachment.user_id == user.id, Attachment.attachment_id == "att_stuck"
        )
    )
    assert row is None
    # The object really did survive — asserting only the log would pass against a sweep that
    # never ran.
    assert len(fake_storage.objects) == 1

    survived = [e for e in logs if e["event"] == "attachment_blob_sweep_survived"]
    assert len(survived) == 1
    assert survived[0]["attachment_id"] == "att_stuck"
    assert survived[0]["key_count"] == 1


async def test_delete_missing_is_idempotent(client, db_session) -> None:
    headers, _, conv = await _auth(db_session)
    resp = await client.delete("/v1/attachments/att_ghost", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


async def test_delete_cross_user_is_noop_and_preserves_owner_data(
    client, db_session, fake_storage
) -> None:
    # Destructive-leak guard: B DELETEing A's attachmentId is a 200 no-op AND must NOT
    # touch A's row or blob — the `_load_owned(db, user.id, …)` scope predicate must hold.
    a_headers, user_a, conv = await _auth(db_session)
    await client.post(
        "/v1/attachments",
        headers=a_headers,
        json={
            "conversationId": str(conv.id),
            "attachmentId": "att_shared",
            "mediaType": "image/png",
            "base64": _b64(_PNG),
        },
    )
    assert len(fake_storage.objects) == 1

    b_headers, _, conv = await _auth(db_session)
    resp = await client.delete("/v1/attachments/att_shared", headers=b_headers)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}  # idempotent no-op — B owns nothing

    assert len(fake_storage.objects) == 1
    row = await db_session.scalar(
        select(Attachment).where(
            Attachment.user_id == user_a.id, Attachment.attachment_id == "att_shared"
        )
    )
    assert row is not None


async def test_malformed_base64_rejected(client, db_session) -> None:
    # Exercises the tuple-except branch in _validate_attachment_bytes.
    headers, _, conv = await _auth(db_session)
    resp = await client.post(
        "/v1/attachments",
        headers=headers,
        json={
            "conversationId": str(conv.id),
            "attachmentId": "att_b64",
            "mediaType": "image/png",
            "base64": "a",
        },
    )
    assert resp.status_code == 400
    assert "do not match the declared type" in resp.json()["error"]["message"]


def test_attachments_openapi_documents_codes() -> None:
    from src.main import create_app

    paths = create_app().openapi()["paths"]
    upload = set(paths["/v1/attachments"]["post"]["responses"])
    # "415" IS THE POINT OF THIS LINE. The subset operator makes every code here opt-in, so a
    # status the route raises without declaring passes unnoticed — which is exactly what the
    # locked-PDF 415 did until a review caught it. Anything raised gets named here.
    #
    # "501" IS GONE. It was the deck converter's "PowerPoint attachments aren't enabled",
    # and the converter is deleted — a .pptx is stored as itself and read in the sandbox now.
    #
    # AND THIS LINE HAD TO CHANGE, which the retirement inventory predicted it would not: it read
    # the subset operator as making the assertion blind to a NARROWING. It is the opposite way
    # round — the literal set is on the LEFT, so every code named here must be present, and
    # dropping 501 from the route turned this red. The blindness is in the other direction, to a
    # code the route raises without declaring, which is what the comment above is about.
    assert {"400", "401", "404", "413", "415", "429", "500"} <= upload
    dl = set(paths["/v1/attachments/{attachment_id}"]["get"]["responses"])
    assert {"400", "404", "401", "500"} <= dl
    delete = set(paths["/v1/attachments/{attachment_id}"]["delete"]["responses"])
    assert {"400", "429", "401", "500"} <= delete


# --- conversation link --------------------------------------------------------


async def test_upload_links_owned_conversation(client, db_session, fake_storage) -> None:
    headers, user, conv = await _auth(db_session)
    conv = await ConversationFactory.create(db_session, user.id)
    resp = await client.post(
        "/v1/attachments",
        headers=headers,
        json={
            "attachmentId": "att_linked",
            "mediaType": "image/png",
            "base64": _b64(_PNG),
            "conversationId": str(conv.id),
        },
    )
    assert resp.status_code == 201
    row = await db_session.scalar(
        select(Attachment).where(
            Attachment.user_id == user.id, Attachment.attachment_id == "att_linked"
        )
    )
    assert row is not None
    assert row.conversation_id == conv.id


async def test_an_upload_with_no_conversation_id_is_refused_with_a_sentence(
    client, db_session, fake_storage
) -> None:
    """★ THE BREAKING CHANGE, AND ITS WORDS. This test asserted the opposite: an upload with
    no `conversationId` stored a NULL link and kept working, because the composer uploaded before
    the chat existed. The order is inverted now — the chat is created, then its files go up
    against it — so the absence is a client that has not been updated.

    400, NOT 422, and the sentence matters as much as the status: this route hand-parses its body,
    so every body error here renders the data-plane `{"error":{"message","code"}}` envelope, which
    is the only shape `uploadAttachment` reads. A FastAPI 422 would render a different one and the
    browser would show its generic fallback instead of this.

    Mutation check: make the field optional again and this goes red on the status.
    """
    headers, user, _ = await _auth(db_session)

    resp = await client.post(
        "/v1/attachments",
        headers=headers,
        json={
            "attachmentId": "att_unlinked",
            "mediaType": "image/png",
            "base64": _b64(_PNG),
        },
    )

    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "CONVERSATION_ID_REQUIRED"
    assert body["error"]["message"] == (
        "conversationId is required — create the conversation first, then upload its files "
        "against it."
    )
    # Nothing is stored on the refusal path — no object, no row.
    assert fake_storage.objects == {}
    row = await db_session.scalar(
        select(Attachment).where(
            Attachment.user_id == user.id, Attachment.attachment_id == "att_unlinked"
        )
    )
    assert row is None


async def test_upload_cross_user_conversation_404(client, db_session, fake_storage) -> None:
    # A well-formed conversationId the caller does NOT own is the same non-leaking 404 as a
    # missing one, and nothing is written or stored.
    a_headers, user_a, conv = await _auth(db_session)
    conv_a = await ConversationFactory.create(db_session, user_a.id)

    b_headers, user_b, conv_b = await _auth(db_session)
    resp = await client.post(
        "/v1/attachments",
        headers=b_headers,
        json={
            "attachmentId": "att_steal",
            "mediaType": "image/png",
            "base64": _b64(_PNG),
            "conversationId": str(conv_a.id),
        },
    )
    assert resp.status_code == 404
    assert resp.json() == {"error": {"message": "Conversation not found."}}
    assert fake_storage.objects == {}  # nothing stored on the reject path
    row = await db_session.scalar(
        select(Attachment).where(Attachment.attachment_id == "att_steal")
    )
    assert row is None


async def test_an_upload_for_a_chat_that_does_not_exist_is_a_404(
    client, db_session, fake_storage
) -> None:
    """★ THE THIRD ASSERTION THIS TEST HAS CARRIED, and each one was right about its own ordering.

    It first said an unknown conversation id is refused. Then it said the opposite — that a
    well-formed, unwritten id is ACCEPTED and stored NULL — because the composer minted the id in
    the browser and the row was created by the first send, which happened strictly after the
    upload. Refusing it then made attaching a file to a new chat impossible.

    The row is created before the first upload now, so an unwritten id is once again a client that
    is out of step, and it answers exactly what a stranger's id answers (ADR-0004): absence and
    somebody else's chat are one non-leaking 404.
    """
    headers, _, _ = await _auth(db_session)

    resp = await client.post(
        "/v1/attachments",
        headers=headers,
        json={
            "attachmentId": "att_newchat",
            "mediaType": "image/png",
            "base64": _b64(_PNG),
            "conversationId": str(uuid.uuid4()),  # well-formed, never written
        },
    )

    assert resp.status_code == 404, resp.text
    assert resp.json() == {"error": {"message": "Conversation not found."}}
    assert fake_storage.objects == {}
    row = await db_session.scalar(
        select(Attachment).where(Attachment.attachment_id == "att_newchat")
    )
    assert row is None


async def test_upload_malformed_conversation_id_400(client, db_session, fake_storage) -> None:
    headers, _, conv = await _auth(db_session)
    resp = await client.post(
        "/v1/attachments",
        headers=headers,
        json={
            "attachmentId": "att_badconv",
            "mediaType": "image/png",
            "base64": _b64(_PNG),
            "conversationId": "not a uuid!",  # fails the id token shape
        },
    )
    assert resp.status_code == 400
    assert resp.json() == {"error": {"message": "Invalid conversation id."}}
    assert fake_storage.objects == {}


async def test_upload_rejected_parse_stores_no_object_with_conversation_id(
    client, db_session, fake_storage
) -> None:
    """★ RE-POINTED, NOT DELETED.

    The invariant here is about CONVERSATION LINKING, not about Office: a refused upload must
    leave no orphaned object even when the body carries a valid conversationId. It happened to
    ride the office branch as its vehicle, and that branch is gone — so it is re-pointed onto the
    code lane's own refusal rather than removed with the machinery it borrowed.

    Worth stating because the inventory predicted this test would simply disappear with the
    office arm. It does not: it goes RED, because a corrupt workbook now reaches the new lane and
    is refused there instead. Red is the good outcome — the silent one would have been it passing
    for a different reason and quietly stopping proving anything.
    """
    headers, user, conv = await _auth(db_session)
    conv = await ConversationFactory.create(db_session, user.id)
    resp = await client.post(
        "/v1/attachments",
        headers=headers,
        json={
            "attachmentId": "att_corrupt",
            "name": "book.xlsx",
            "mediaType": EXCEL_MEDIA_TYPE,
            "base64": _b64(_zip_with({"readme.txt": b"not a workbook"})),
            "conversationId": str(conv.id),
        },
    )
    assert resp.status_code == 415
    assert fake_storage.objects == {}


async def test_reclaim_frees_room_then_upload_succeeds(client, db_session, fake_storage) -> None:
    """THE RECLAIMER'S ONLY END-TO-END WIRING TEST, re-pointed from bytes to the count.

    A citizen whose conversation is full of never-sent uploads can upload again after a sweep —
    the whole reason the reclaimer exists, and the only test that drives it through the door rather
    than calling it directly. It used to fill the deleted byte budget with one huge row; it fills
    the file count with twenty old ones now.

    ★ AND THE ORPHANS ARE LINKED, which is the shape an upload that names its conversation
    actually produces. An upload names its
    conversation at the door, so a first send refused four times leaves twenty files linked to a
    chat and carried by no message. The reclaimer never read the link — it asks whether any SENT
    message references the row, and age — so it frees exactly these.
    """
    headers, user, conv = await _auth(db_session)
    old = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=30)
    keys = []
    for index in range(MAX_ATTACHMENTS_PER_CONVERSATION):
        key = f"att/{user.id}/old-{index}"
        keys.append(key)
        db_session.add(
            Attachment(
                user_id=user.id,
                attachment_id=f"att_old_{index}",
                media_type="image/png",
                name="",
                size=len(_PNG),
                storage_key=key,
                conversation_id=conv.id,
                created_at=old,
            )
        )
    await db_session.flush()
    for key in keys:
        fake_storage.objects[key] = b"x"

    over = await client.post(
        "/v1/attachments",
        headers=headers,
        json={
            "conversationId": str(conv.id),
            "attachmentId": "att_new",
            "mediaType": "image/png",
            "base64": _b64(_PNG),
        },
    )
    assert over.status_code == 413
    assert over.json()["error"]["code"] == "CONVERSATION_ATTACHMENTS_FULL"

    result = await reclaim_orphaned_attachments(db_session, fake_storage, user_id=user.id)
    assert result.reclaimed == MAX_ATTACHMENTS_PER_CONVERSATION
    assert result.freed_bytes == len(_PNG) * MAX_ATTACHMENTS_PER_CONVERSATION
    for key in keys:
        assert key not in fake_storage.objects

    ok = await client.post(
        "/v1/attachments",
        headers=headers,
        json={
            "conversationId": str(conv.id),
            "attachmentId": "att_new",
            "mediaType": "image/png",
            "base64": _b64(_PNG),
        },
    )
    assert ok.status_code == 201


# --- rate limit + auth --------------------------------------------------------


async def test_rate_limit_enforced(client, db_session) -> None:
    from src.api.v1.attachments.router import ATTACHMENT_RATE_LIMIT

    headers, _, conv = await _auth(db_session)
    payload = {
        "conversationId": str(conv.id),
        "attachmentId": "att_rl",
        "mediaType": "image/png",
        "base64": _b64(_PNG),
    }
    for _ in range(ATTACHMENT_RATE_LIMIT):
        resp = await client.post("/v1/attachments", headers=headers, json=payload)
        assert resp.status_code == 201  # idempotent re-upload of the same id
    blocked = await client.post("/v1/attachments", headers=headers, json=payload)
    assert blocked.status_code == 429
    assert blocked.json() == {
        "error": {"message": "Too many attachment requests. Please slow down."}
    }


# --- office / deck branches ---------------------------------------------------


async def test_upload_non_string_name_400(client, db_session, fake_storage) -> None:
    headers, _, conv = await _auth(db_session)
    for bad in (123, ["shot.png"], {"n": "x"}):
        resp = await client.post(
            "/v1/attachments",
            headers=headers,
            json={
                "conversationId": str(conv.id),
                "attachmentId": "att_badname",
                "name": bad,
                "mediaType": "image/png",
                "base64": _b64(_PNG),
            },
        )
        assert resp.status_code == 400, bad
        assert resp.json() == {"error": {"message": "name must be a string."}}, bad
    assert fake_storage.objects == {}  # nothing stored


async def test_upload_over_long_name_400(client, db_session, fake_storage) -> None:
    # `Attachment.name` is String(512); 513 is one past the boundary — the check must catch
    # it here, 400ing where the client can fix it, rather than 500ing at the DB flush.
    headers, _, conv = await _auth(db_session)
    resp = await client.post(
        "/v1/attachments",
        headers=headers,
        json={
            "conversationId": str(conv.id),
            "attachmentId": "att_longname",
            "name": "n" * 513,
            "mediaType": "image/png",
            "base64": _b64(_PNG),
        },
    )
    assert resp.status_code == 400
    assert resp.json() == {"error": {"message": "name must be at most 512 characters."}}
    assert fake_storage.objects == {}  # nothing stored


async def test_upload_absent_name_defaults_to_empty(client, db_session) -> None:
    # Absent (and its `null` spelling) keeps the column's defined "" default — name is optional.
    headers, user, conv = await _auth(db_session)
    resp = await client.post(
        "/v1/attachments",
        headers=headers,
        json={
            "conversationId": str(conv.id),
            "attachmentId": "att_noname",
            "mediaType": "image/png",
            "base64": _b64(_PNG),
        },
    )
    assert resp.status_code == 201
    assert resp.json()["attachment"]["name"] == ""
    row = await db_session.scalar(
        select(Attachment).where(
            Attachment.user_id == user.id, Attachment.attachment_id == "att_noname"
        )
    )
    assert row is not None and row.name == ""


async def test_office_upload_non_string_name_400(client, db_session) -> None:
    # The name is parsed ONCE at the boundary, so the office branch is covered by the same check.
    headers, _, conv = await _auth(db_session)
    workbook = Workbook()
    buffer = io.BytesIO()
    workbook.save(buffer)
    resp = await client.post(
        "/v1/attachments",
        headers=headers,
        json={
            "conversationId": str(conv.id),
            "attachmentId": "att_office_badname",
            "mediaType": EXCEL_MEDIA_TYPE,
            "base64": _b64(buffer.getvalue()),
            "name": 42,
        },
    )
    assert resp.status_code == 400
    assert resp.json() == {"error": {"message": "name must be a string."}}


async def test_requires_auth(client) -> None:
    assert (await client.get("/v1/attachments/att_1")).status_code == 401
    assert (await client.post("/v1/attachments", json={})).status_code == 401


# --- what the door asks a PDF ------------------------------------------------
#
# ★ TWO QUESTIONS, AND NEITHER IS "HOW LONG IS IT". A page cap used to live here, because a
# document was charged a flat figure sized to it — a 61-page file measured 153,342 tokens, 77% of
# the hard context limit, while the guardrail recorded 1,600. The guardrail stopped guessing
# (`test_context_window.py`: it reads what the provider reported for a turn it served), and the
# flat charge went with it, which left a cap bounding a cost that no longer existed. It is gone,
# and its parser, its process governor and its dependency with it. A long document's token cost is
# the client's to bear.
#
# WHAT REPLACED IT IS NOT NOTHING. The page count was also the only READABILITY check any PDF got,
# so a file that could not be opened at all was refused as a side effect of being counted. Two
# byte scans over the file's tail keep that half: is it locked, and is it all there. Both are
# dependency-free, neither reads text, and the structural one refuses only on positive evidence.


async def _upload_pdf(client, headers, conv, attachment_id: str, data: bytes, name="doc.pdf"):
    return await client.post(
        "/v1/attachments",
        headers=headers,
        json={
            "conversationId": str(conv.id),
            "attachmentId": attachment_id,
            "mediaType": "application/pdf",
            "base64": _b64(data),
            "name": name,
        },
    )


async def test_a_long_pdf_is_accepted_now_that_nothing_counts_its_pages(
    client, db_session, fake_storage
) -> None:
    """★ THE DOOR IMPOSES NO PAGE CAP, AS ONE ASSERTION. Forty pages was over the old cap and is an
    ordinary business document; it uploads.

    THE POSITIVE CASE COMES FIRST because every other test in this section asserts a refusal, and
    a door that refused every PDF would satisfy all of them.
    """
    headers, _, conv = await _auth(db_session)

    resp = await _upload_pdf(client, headers, conv, "att_long", pdf_with_pages(40))

    assert resp.status_code == 201, resp.text
    assert len(fake_storage.objects) == 1


async def test_a_scanned_image_only_pdf_is_accepted(client, db_session, fake_storage) -> None:
    """★ THE CHECK IS STRUCTURAL, NOT TEXTUAL, and this is the file that proves it.

    A scanned invoice carries no text at all — every page is one image — and it is a first-class
    supported case: the model reads a PDF as vision. Anything at this door that reached for
    the file's text would refuse the very documents citizens photograph and upload most.

    Mutation check: add any text probe to the PDF arm and this goes red.
    """
    headers, _, conv = await _auth(db_session)

    resp = await _upload_pdf(client, headers, conv, "att_scan", scanned_pdf())

    assert resp.status_code == 201, resp.text


async def test_a_password_protected_pdf_is_refused_at_the_door(
    client, db_session, fake_storage
) -> None:
    """★ AE8c — a locked document is the one PDF failure a citizen can act on, so it keeps its own
    refusal rather than being stored, counted, and failing in front of the model.

    Rebuilt without the library that used to detect it: the door reads the trailer's `/Encrypt`
    entry, which is syntax.
    """
    headers, _, conv = await _auth(db_session)

    resp = await _upload_pdf(client, headers, conv, "att_locked", encrypted_pdf(pages=3))

    assert resp.status_code == 415, resp.text
    body = resp.json()
    assert body["error"]["code"] == "PDF_ENCRYPTED"
    assert body["error"]["message"] == PASSWORD_PROTECTED_TEXT
    assert fake_storage.objects == {}


async def test_an_xref_stream_encrypted_pdf_is_refused_too(
    client, db_session, fake_storage
) -> None:
    """★ THE FALSE-NEGATIVE DIRECTION, and the one a naive check misses entirely.

    Word and Acrobat emit PDF 1.5+ cross-reference STREAMS and write no `trailer` keyword at all,
    so a scan keyed on that word finds nothing in the encrypted document a citizen is most likely
    to actually have — while correctly refusing a hand-made classic one. The two fixtures must
    both be refused or the check is theatre.

    Mutation check: key the lock scan on the `trailer` keyword and only this test goes red.
    """
    headers, _, conv = await _auth(db_session)

    resp = await _upload_pdf(client, headers, conv, "att_locked_xref", encrypted_xref_stream_pdf())

    assert resp.status_code == 415, resp.text
    assert resp.json()["error"]["code"] == "PDF_ENCRYPTED"
    assert fake_storage.objects == {}


async def test_a_document_that_merely_mentions_encryption_is_not_called_locked(
    client, db_session, fake_storage
) -> None:
    """★ THE FALSE-POSITIVE DIRECTION. A perfectly ordinary document whose page text is prose
    about encryption carries the literal bytes `/Encrypt` near the end of the file. Called locked,
    its owner is told to remove a password that does not exist — advice that leads nowhere, about
    a file that is fine.

    The scan matches the trailer's `/Encrypt <num> <gen> R` indirect reference, which the spec
    requires and running prose does not take.

    Mutation check: match the bare word and this goes red.
    """
    headers, _, conv = await _auth(db_session)

    resp = await _upload_pdf(
        client, headers, conv, "att_prose", pdf_mentioning_encrypt_in_its_content()
    )

    assert resp.status_code == 201, resp.text


async def test_a_truncated_pdf_is_refused_and_not_stored(client, db_session, fake_storage) -> None:
    """★ THE READABILITY HALF THE PAGE CAP USED TO PROVIDE. A dropped upload is a real document
    whose last quarter never arrived — no cross-reference table, no terminator. Admitted, it is
    stored, counted against the conversation and fails in front of the model turns later, where
    nothing can explain it.

    Mutation check: delete the structural scan and this is stored with a 201.
    """
    headers, _, conv = await _auth(db_session)

    resp = await _upload_pdf(client, headers, conv, "att_cut", truncated_pdf())

    assert resp.status_code == 415, resp.text
    assert fake_storage.objects == {}


async def test_a_pdf_naming_an_offset_past_its_own_end_is_refused(
    client, db_session, fake_storage
) -> None:
    """The subtler shape of the same failure: terminator present, `startxref` pointing into bytes
    that are not there. Positive evidence, which is the only kind the scan acts on."""
    headers, _, conv = await _auth(db_session)

    resp = await _upload_pdf(
        client, headers, conv, "att_past_end", pdf_pointing_past_its_own_end()
    )

    assert resp.status_code == 415, resp.text
    assert fake_storage.objects == {}


async def test_magic_valid_rubbish_is_still_refused(client, db_session, fake_storage) -> None:
    """A file that passes the 18-byte prefix check and is plainly not a document. It used to be
    refused as "too long to work with", which was never true of it; the sentence it gets now says
    the readable thing instead."""
    headers, _, conv = await _auth(db_session)

    resp = await _upload_pdf(client, headers, conv, "att_rubbish", unreadable_pdf())

    assert resp.status_code == 415, resp.text
    assert "could not be read as a PDF" in resp.json()["error"]["message"]
    assert fake_storage.objects == {}


async def test_a_signed_pdf_with_incremental_updates_is_accepted(
    client, db_session, fake_storage
) -> None:
    """★ THE FAIL-OPEN DIRECTION, and the reason the structural scan refuses only on evidence.

    Signing and annotating append incremental sections, each with its own xref table and trailer,
    which pushes the ORIGINAL structure far from the end of the file. A scan that demanded to
    recognise the whole document would refuse exactly the files an approvals process produces —
    and refusing a valid file at the door is worse than letting a broken one fail later, which is
    what happened before this check existed anyway.

    Mutation check: make the scan require the FIRST trailer to be findable and this goes red.
    """
    headers, _, conv = await _auth(db_session)

    resp = await _upload_pdf(client, headers, conv, "att_signed", incrementally_updated_pdf())

    assert resp.status_code == 201, resp.text


async def test_a_cross_reference_bomb_costs_the_door_nothing(
    client, db_session, fake_storage
) -> None:
    """★ THE RECEIPT THAT REMOVING THE GOVERNOR DID NOT REOPEN WHAT IT WAS FOR.

    This 32 KB file declares eight million cross-reference entries. A reader must walk every one
    before it can resolve the catalog — six to twelve seconds, inside every size cap, unbounded in
    the only axis they watch — and that is precisely why each PDF used to be handed to a killable,
    memory-capped subprocess.

    Nothing opens a PDF here any more. The door scans the last 64 KB for two byte patterns, so the
    bomb is neither expensive nor interesting: it is a well-formed, unencrypted document and it is
    accepted, in microseconds.

    The wall clock is asserted deliberately loosely. It is not a benchmark — it is the difference
    between "scanned some bytes" and "walked eight million entries", which is three orders of
    magnitude, so a generous bound still fails loudly if a parse ever comes back.
    """
    headers, _, conv = await _auth(db_session)
    bomb = xref_bomb_pdf()

    started = time.monotonic()
    resp = await _upload_pdf(client, headers, conv, "att_bomb", bomb)
    elapsed = time.monotonic() - started

    assert resp.status_code == 201, resp.text
    assert elapsed < 2.0, f"the door spent {elapsed:.1f}s on a file nothing should parse"


async def test_a_locked_pdf_and_a_locked_workbook_say_the_identical_sentence(
    client, db_session, fake_storage
) -> None:
    """★ ONE SENTENCE FOR ONE SITUATION, asserted as byte equality.

    The two used to differ in both nouns: a locked PDF was told to "remove the password and UPLOAD
    it again" about "that DOCUMENT", a locked workbook to "attach it again" about "that FILE".
    Same predicament, same remedy, two voices — and a citizen who hits both learns that the
    platform does not know it is saying the same thing twice.

    Mutation check: fork either sentence and this goes red on the equality, not on a substring.
    """
    headers, _, conv = await _auth(db_session)
    ole2 = bytes([0xD0, 0xCF, 0x11, 0xE0, 0xA1, 0xB1, 0x1A, 0xE1]) + b"\x00" * 64

    pdf = await _upload_pdf(client, headers, conv, "att_lp", encrypted_pdf())
    workbook = await client.post(
        "/v1/attachments",
        headers=headers,
        json={
            "conversationId": str(conv.id),
            "attachmentId": "att_lx",
            "name": "book.xlsx",
            "mediaType": EXCEL_MEDIA_TYPE,
            "base64": _b64(ole2),
        },
    )

    assert pdf.status_code == 415 and workbook.status_code == 415
    assert pdf.json()["error"]["message"] == workbook.json()["error"]["message"]
    assert pdf.json()["error"]["message"] == PASSWORD_PROTECTED_TEXT


async def test_a_refused_pdf_leaks_no_internals_to_the_citizen(
    client, db_session, fake_storage
) -> None:
    """The standing rule for every refusal here: name the file's problem, never the platform's
    machinery. A citizen holding a damaged scan should not meet the words trailer, xref or
    startxref."""
    headers, _, conv = await _auth(db_session)

    resp = await _upload_pdf(client, headers, conv, "att_leak", truncated_pdf())

    message = resp.json()["error"]["message"]
    for leak in ("trailer", "startxref", "xref", "byte", "parse", "encrypt", "/Encrypt"):
        assert leak not in message.lower(), f"{leak!r} leaked into {message!r}"


async def test_an_image_is_not_put_through_the_pdf_scans(
    client, db_session, fake_storage, monkeypatch
) -> None:
    """The arm is split on media type, and this is the receipt. A PNG has no trailer to look in,
    and a scan that ran over one would be asking a question with no meaning — the same reasoning
    that kept images out of the page counter before it."""
    import src.api.v1.attachments.router as att_router

    # The real one is taken from the module that DEFINES it, not from the router that imported
    # it: the router re-binds the name, and reading it back through there is an implicit re-export
    # the strict gate refuses. The patch still targets the router's own binding, which is what the
    # route body reads.
    from src.services.media.lanes import pdf_refusal as real_pdf_refusal

    headers, _, conv = await _auth(db_session)
    calls: list[str] = []

    def _spy(name: str, data: bytes) -> str | None:
        calls.append(name)
        return real_pdf_refusal(name, data)

    monkeypatch.setattr(att_router, "pdf_refusal", _spy)

    image = await client.post(
        "/v1/attachments",
        headers=headers,
        json={
            "conversationId": str(conv.id),
            "attachmentId": "att_png",
            "mediaType": "image/png",
            "base64": _b64(_PNG),
        },
    )
    document = await _upload_pdf(client, headers, conv, "att_pdf", pdf_with_pages(2))

    assert image.status_code == 201 and document.status_code == 201
    assert calls == ["doc.pdf"], "only the PDF should reach the PDF scans"


async def test_the_pdf_refusals_leave_a_trace_an_operator_can_act_on(
    client, db_session, fake_storage
) -> None:
    """The compensating control for a citizen-facing sentence that says nothing about the cause.

    A locked file needs no log — its refusal already names the cause, and the citizen can act on
    it. An INCOMPLETE one does: "could not be read as a PDF" is all the citizen is told, so the
    operator half has to exist somewhere, and it is one event carrying the size and nothing that
    could identify the file.
    """
    headers, _, conv = await _auth(db_session)

    with capture_logs() as logs:
        resp = await _upload_pdf(client, headers, conv, "att_logged", truncated_pdf())

    assert resp.status_code == 415, resp.text
    events = [entry for entry in logs if entry["event"] == "pdf_refused_as_incomplete"]
    assert len(events) == 1, f"expected exactly one trace, got {events}"
    assert events[0]["size"] > 0
    assert "doc.pdf" not in repr(events[0])
