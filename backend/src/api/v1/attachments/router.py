"""Attachment HTTP endpoints — upload / download / delete, for all ten formats.

THE DOOR ASKS THREE QUESTIONS AND NOTHING DOWNSTREAM ASKS ANY OF THEM AGAIN. Is this file one of
the ten? Is it under `ATTACHMENT_MAX_BYTES`? Is it whole and unlocked? A file that passes is
stored as itself, owner-scoped, and read where it can actually be read.

ONE SIZE FOR EVERY FORMAT, AND ONE PLACE THAT ASKS. There used to be four independently-declared
per-file byte numbers — this route's, a duplicate inside a decoder that had no callers, the
browser's, and the supervisor's write ceiling. Four numbers for one rule is a rule that will
disagree with itself, and it was one release away from doing so. The others are gone; the browser
keeps a copy because a citizen should learn a file is too large before uploading it, and a test
holds the two equal.

WHAT IS NOT ASKED, DELIBERATELY. Length. A PDF's page count used to be measured in a killable
subprocess and capped, because a document was charged a flat figure sized to that cap. Nothing
prices a document up front any more — the window check reads what the provider reports for a
completed turn — so the cap was bounding a cost that no longer exists, at the price of a
dependency, a process governor and a refusal a citizen could not act on. The token cost of a long
document is the client's to bear.

Object keys are scoped by `user_id` and re-guarded with `assert_owned`; the envelopes are the
ported `{error:{message,code?}}` / `{ok:true}`.
"""

from __future__ import annotations

import base64
import binascii
import re
import uuid
from typing import Annotated, Any, Final

import sqlalchemy as sa
import structlog
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from src.api.deps import CurrentUser, DbSession
from src.api.v1.attachments.schemas import UploadResponse

# The one media type admitted and charged as a document. Imported rather than re-spelled:
# `_shared.resolve_binaries` is what decides a stored ref IS a document, so a second copy here
# could drift from the definition the send route actually enforces.
from src.api.v1.conversations._shared import PDF_MEDIA_TYPE
from src.core.errors import AppApiError
from src.db.models.attachment import MAX_ATTACHMENT_NAME, Attachment
from src.db.models.conversation import Conversation
from src.schemas import AUTH_401, ErrorEnvelope, OkResponse, error_responses
from src.services.extract.zip_safety import FileParseError, assert_zip_not_bomb
from src.services.media.lanes import (
    PASSWORD_PROTECTED_TEXT,
    code_lane_refusal,
    is_code_lane,
    is_opc_archive,
    pdf_refusal,
)
from src.services.media.magic import ALLOWED_MEDIA, chip_kind_for, magic_matches
from src.services.ratelimit import rate_limit
from src.services.storage import (
    ObjectStorage,
    StorageNotFoundError,
    assert_owned,
    attachment_key,
    get_storage,
    sweep_blobs,
)

logger = structlog.get_logger()

router = APIRouter(prefix="/attachments", tags=["attachments"])

# Client-minted attachment id shape (Express `ID_RE`) — a safe object-key token.
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

ATTACHMENT_MAX_BYTES = 10 * 1024 * 1024
"""The per-file decoded cap, and THE ONLY PLACE THE SIZE QUESTION IS ASKED.

TEN MEGABYTES FOR EVERY FORMAT. It was four, and a second, far lower number applied to the two
delimited formats on the browser side alone — so a citizen with a 300 KB CSV export was refused
by a rule the server did not have and could not have explained. One number covers a photograph, a
scanned invoice, a workbook and a deck, and a citizen never has to know which of their files the
platform considers expensive.

EVERY REFUSAL THAT NAMES A SIZE INTERPOLATES THIS CONSTANT rather than spelling a number, so the
figure a citizen is told and the figure enforced cannot drift."""

ATTACHMENT_MAX_MB = ATTACHMENT_MAX_BYTES // (1024 * 1024)
"""The cap as a whole number of megabytes, for the sentences that have to say it out loud."""

_BODY_LIMIT_BYTES = 15 * 1024 * 1024
"""The request-body ceiling, and NOT a second opinion on the cap above.

It fires on RAW WIRE BYTES before the body is decoded, which is a different question: no client
can route around the framework's own buffering, so this is what stops a hostile body being read
into memory at all. It must therefore clear base64 of a legal file — 10 MiB encodes to 13,981,016
bytes — with room for the JSON around it. Set below that and the door would refuse a file it
means to accept, with a sentence about the request rather than the file."""

MAX_ATTACHMENTS_PER_CONVERSATION = 20
"""How many attachments one conversation may hold, counted SERVER-SIDE.

★ IT IS NOW THE WHOLE OF THE PER-CONVERSATION LIMIT. A byte budget used to sit beside it — 50 MB
per conversation — and the two were a single rule wearing two numbers: whichever bound bit first
decided, and a citizen could not tell which they had hit or predict either. A count is the one a
person can hold in their head, and it is the one the composer already showed them.

The browser has had this number since the beginning and it was never enforced here: the portal's
`validateConversationAttachmentCap` tallies attachments by walking the messages the browser has
loaded, so it reset to zero on every page reload. A cap a refresh clears is not a cap.

THE TRADE, STATED: nothing now bounds a citizen's TOTAL stored bytes, because many conversations
means many budgets. Taken deliberately — the byte budget it replaces was itself scoped to the
conversation, so that ceiling was already gone — and worth watching rather than pre-solving."""


ATTACHMENT_LANES_SENTENCE: Final = (
    "Attach a picture or a PDF and I'll look at it; attach a spreadsheet, document or slide "
    "deck and I'll open it with code."
)
"""ONE SENTENCE, EVERYWHERE. The composer, the help page and every unsupported-format
refusal carry these exact words — three sentences that drift is how the removed rule failed. Its
portal twin is `ATTACHMENT_LANES_SENTENCE` in `portal/src/utils/attachmentInput.ts`, and a test
holds the two byte-identical.

IT DESCRIBES WHAT HAPPENS TO A FILE, not which extensions are on a list. A list of ten formats
goes stale the moment the allowlist moves, and tells a citizen nothing about why a spreadsheet
behaves differently from a photograph."""


PDF_LOCKED_CODE: Final = "PDF_ENCRYPTED"
"""The machine-readable code beside the shared locked-file sentence, so a client can branch on
password protection without string-matching prose.

THE SENTENCE ITSELF IS `media.PASSWORD_PROTECTED_TEXT`, shared byte-for-byte with the locked
Office refusal. This route used to word its own — "that DOCUMENT is password-protected, remove
the password and UPLOAD it again" against the lane's "that FILE … ATTACH it again" — the same
situation told twice in different words."""

# The allowlist + magic-byte prefixes live in `src.services.media.magic` — the SINGLE source of
# truth shared with every other path that can put bytes in front of the model, so a block the
# upload path would reject cannot slip in through one of them. `ALLOWED_MEDIA` / `magic_matches`
# imported above.

# Attachment limiter (Express: ~30/min, POST + DELETE only; GET is never limited).
ATTACHMENT_RATE_LIMIT = 30
ATTACHMENT_RATE_WINDOW_SECONDS = 60


def storage_dependency() -> ObjectStorage:
    """The configured object store. A dependency (not a bare `get_storage()` at the callsite)
    so tests override it with an in-memory fake via `dependency_overrides`."""
    return get_storage()


async def _attachment_rate_key(user: CurrentUser) -> str:
    return f"attachment:{user.id}"


_attachment_limiter = rate_limit(
    _attachment_rate_key,
    limit=ATTACHMENT_RATE_LIMIT,
    window_seconds=ATTACHMENT_RATE_WINDOW_SECONDS,
    message="Too many attachment requests. Please slow down.",
)

# Every attachments route authenticates via `current_user` (bare HTTPException 401 ->
# `{"detail"}`), so each documents 401 via the shared `AUTH_401` (DetailBody) spec.

Storage = Annotated[ObjectStorage, Depends(storage_dependency)]


def _validate_attachment_bytes(media_type: str, b64: Any) -> str | None:
    """Validate a MODEL-LANE upload (image/PDF) against the allowlist + magic bytes.

    THE CODE LANE IS NOT CHECKED HERE, and that is the point rather than a gap. `ALLOWED_MEDIA` is
    the magic-byte gate, and it is applied on both paths that end at the
    model — this route, the store's rehydrator and `build_sessions/attachments.py`. Widening it to
    admit Office would make every one of them answer True for a deck, and a spreadsheet would reach
    the model as raw ZIP bytes on whichever path lost its refusal first. Office, CSV and TSV are
    admitted by `code_lane_refusal` instead, which runs only where an attachment is stored, so the
    model-facing consumers keep refusing them without a line changing in either of them.
    """
    if not isinstance(b64, str) or not b64:
        return "Invalid attachment: missing bytes."
    magic = ALLOWED_MEDIA.get(media_type)
    if magic is None:
        return f"Unsupported attachment type: {media_type}. {ATTACHMENT_LANES_SENTENCE}"
    # 24 base64 chars → 18 bytes: enough for any magic prefix + the WebP form-type at offset 8.
    try:
        prefix = base64.b64decode(b64[:24])
    except (binascii.Error, ValueError):  # fmt: skip  # ruff py314 strips parens
        prefix = b""
    if not magic_matches(prefix, magic):
        return f"Attachment bytes do not match the declared type {media_type}."
    if media_type == "image/webp" and prefix[8:12] != b"WEBP":
        return "Attachment bytes do not match the declared type image/webp."
    return None


def _attachment_name(value: Any) -> str:
    """The client-supplied display name. Absent (or `null`) → `""`, the column's defined default
    — name is optional. A PRESENT non-string is a client bug: coercing it stored a nameless
    attachment and silently lost the filename the SPA renders. Over-long is rejected HERE —
    the column is `String(MAX_ATTACHMENT_NAME)`, so letting it through 500s at the DB."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise AppApiError(400, "name must be a string.")
    if len(value) > MAX_ATTACHMENT_NAME:
        raise AppApiError(400, f"name must be at most {MAX_ATTACHMENT_NAME} characters.")
    return value


def _sniff_media_type(data: bytes) -> str | None:
    """Reverse-lookup the media type from the magic bytes (only allowlisted/validated bytes
    are ever stored), matching Express `sniffMediaType`. None → application/octet-stream."""
    for media, magic in ALLOWED_MEDIA.items():
        if magic_matches(data, magic):
            if media == "image/webp" and data[8:12] != b"WEBP":
                continue
            return media
    return None


CONVERSATION_ID_REQUIRED_CODE: Final = "CONVERSATION_ID_REQUIRED"
CONVERSATION_ID_REQUIRED_TEXT: Final = (
    "conversationId is required — create the conversation first, then upload its files against it."
)
"""★ A BREAKING CHANGE, TAKEN ON PURPOSE. The field was optional and no shipped client sent
it, so every stored row was NULL-linked and adopted afterwards by the send route.

The order is inverted now: the chat exists, then its files are uploaded against it, then the
message is sent. That deletes a whole class of orphan — a file uploaded for a message that is
never sent used to have no owner at all — and it makes the per-conversation count answerable at
the door rather than a scope that has to be reconstructed later.

400 RATHER THAN 422, because this route hand-parses its body: every other body error here renders
the data-plane `{"error":{"message","code"}}` envelope, which is what `uploadAttachment` reads. A
FastAPI 422 would render a different shape and the browser would show its fallback sentence."""


async def _resolve_conversation_link(db: DbSession, user_id: uuid.UUID, raw: Any) -> uuid.UUID:
    """Resolve the client-supplied `conversationId` to an OWNED, EXISTING conversation's id.

    ★ REQUIRED, AND THE ROW MUST ALREADY BE THERE. Neither an absent field nor a well-formed id
    whose row is unwritten is admitted, and linking at insert is what that buys: the count cap
    can be asked at the door, no adoption pass has to run afterwards, and a refused first
    message leaves no unowned file behind.

    Resolving it is referential integrity, NOT the tenancy boundary — the row is written and read
    under the caller's own `user_id` either way; what it buys is that an upload cannot be hung off
    a STRANGER's conversation.

    THE STRANGER CHECK IS UNCHANGED, which is why the owner is READ rather than filtered on: a row
    under another user answers exactly what a missing one does (ADR-0004). Absence and a stranger
    are one 404, and that must survive any future edit here.
    """
    if raw is None:
        raise AppApiError(400, CONVERSATION_ID_REQUIRED_TEXT, code=CONVERSATION_ID_REQUIRED_CODE)
    if not isinstance(raw, str) or not _ID_RE.match(raw):
        raise AppApiError(400, "Invalid conversation id.")
    try:
        cid = uuid.UUID(raw)
    except ValueError:
        # An ID_RE-valid token that isn't a UUID can key no stored conversation.
        raise AppApiError(404, "Conversation not found.") from None
    owner = await db.scalar(sa.select(Conversation.user_id).where(Conversation.id == cid))
    if owner != user_id:
        raise AppApiError(404, "Conversation not found.")
    return cid


async def _store_attachment_bytes(
    db: DbSession,
    storage: ObjectStorage,
    user_id: uuid.UUID,
    attachment_id: str,
    media_type: str,
    name: str,
    conversation_id: uuid.UUID,
    data: bytes,
) -> dict[str, Any]:
    """Enforce the per-conversation COUNT and store the bytes owner-scoped; return the file-part
    ref. Idempotent on a repeated id (reuses the row + key). Raises `AppApiError(413)` when the
    conversation is full. NOTE: the check-then-store has a concurrent-overspend window (as in the
    daily gate) — hardening deferred.

    `conversation_id` is stamped on the CREATE branch and refreshed on a re-upload — it is
    required now, so there is no absence for either branch to preserve."""
    size = len(data)
    # THE BUDGET IS THE CONVERSATION, and there is no second scope.
    #
    # An unlinked pool used to sit beside it: every new chat's first file was uploaded before the
    # conversation row existed, so it stored NULL and was counted against `conversation_id IS
    # NULL` until the send route adopted it. That whole arm is gone with the ordering — the chat
    # is created first, so an upload always names a conversation that is already there.
    scope = [Attachment.user_id == user_id, Attachment.conversation_id == conversation_id]
    existing = await db.scalar(
        sa.select(Attachment).where(
            Attachment.user_id == user_id, Attachment.attachment_id == attachment_id
        )
    )
    # THE COUNT, and only for a file this conversation does not already hold — a re-upload of the
    # same id replaces a row rather than adding one, so counting it would refuse an idempotent
    # retry at the boundary.
    #
    # ★ `existing is None` ALONE IS NOT THAT QUESTION, and the difference is a live bypass. A row
    # is looked up by (owner, id) with no conversation filter, so a known id re-uploaded against a
    # DIFFERENT conversation finds `existing` and skips the count — moving the row into a chat
    # that already holds twenty. The predicate has to be "is this conversation gaining a file",
    # which is the same comparison the deleted byte budget made for the same reason, and
    # `existing.conversation_id` can still be NULL here: `ON DELETE SET NULL` unlinks a row when
    # its conversation is deleted, and rows uploaded before this ordering landed are NULL too. A
    # re-upload of one of those into a conversation IS that conversation gaining a file, and the
    # comparison says so without a special case.
    if existing is None or existing.conversation_id != conversation_id:
        held = await db.scalar(sa.select(sa.func.count()).select_from(Attachment).where(*scope))
        # `scope` is the conversation when there is one and the unlinked pool when there is not.
        if int(held or 0) + 1 > MAX_ATTACHMENTS_PER_CONVERSATION:
            raise AppApiError(
                413,
                f"This conversation has reached its limit of "
                f"{MAX_ATTACHMENTS_PER_CONVERSATION} attachments. Start a new chat to add more.",
                code="CONVERSATION_ATTACHMENTS_FULL",
            )

    if existing is not None:
        key = existing.storage_key
    else:
        key = attachment_key(user_id, uuid.uuid7())
    # Store the bytes first; only then persist the row (a failed put leaves no dangling row).
    await storage.put(key, data, content_type=media_type)
    if existing is not None:
        existing.media_type, existing.name, existing.size = media_type, name, size
        existing.conversation_id = conversation_id
    else:
        db.add(
            Attachment(
                user_id=user_id,
                attachment_id=attachment_id,
                media_type=media_type,
                name=name,
                size=size,
                storage_key=key,
                conversation_id=conversation_id,
            )
        )
    await db.commit()
    return {
        "attachmentId": attachment_id,
        "key": key,
        "mediaType": media_type,
        "size": size,
        "name": name,
    }


def _assert_pdf_is_whole_and_unlocked(data: bytes, name: str) -> None:
    """Refuse a locked or truncated PDF, BEFORE anything is stored.

    ★ IT ASKS NOTHING ABOUT LENGTH, and it costs no subprocess. What stood here counted pages in
    a killable, memory-capped child, because a PDF is the worst-behaved thing this route accepts
    and a cross-reference stream declaring millions of entries costs eight kilobytes and tens of
    seconds to walk. That machinery — a process governor, a spawned child, an rlimit and a PDF
    library — existed to serve a cap on LENGTH, and the charge that cap was sized against is
    gone. Both checks below are byte scans over the last four kilobytes: no parse, no child, no
    dependency, and nothing for a hostile file to be hostile at.

    THE PAGE CHECK WAS ALSO THE ONLY READABILITY CHECK A PDF GOT, which is why removing it alone
    would have been a loss. `pdf_refusal` keeps that half — a file cut short in transfer is
    refused here rather than accepted, stored, and failing in front of the model — and keeps it
    FAILING OPEN, refusing only on positive evidence. See `media/lanes.py` for both scans.

    A locked file answers 415 rather than 413: nothing about its SIZE was the problem, and the
    citizen has something to do about it.
    """
    refusal = pdf_refusal(name, data)
    if refusal is None:
        return
    if refusal == PASSWORD_PROTECTED_TEXT:
        raise AppApiError(415, refusal, code=PDF_LOCKED_CODE)
    logger.info("pdf_refused_as_incomplete", size=len(data))
    raise AppApiError(415, refusal)


@router.post(
    "",
    status_code=201,
    response_model=UploadResponse,
    dependencies=[Depends(_attachment_limiter)],
    responses=error_responses(
        (
            400,
            ErrorEnvelope,
            "Missing or invalid conversationId, or an invalid attachment id, name, type or bytes",
        ),
        (404, ErrorEnvelope, "conversationId not found (or not owned by the caller)"),
        (413, ErrorEnvelope, "Attachment too large, or the conversation already holds 20 files"),
        # DECLARED BECAUSE IT IS RAISED — the locked and incomplete arms answer 415, and a status
        # the route really sends but the schema never mentions is the generated client's problem
        # later. It is 415 and not 413 for the reason given there: nothing about the SIZE was
        # wrong. (This route's own contract test asserts a SUBSET, so it did not catch the gap.)
        (
            415,
            ErrorEnvelope,
            "The file is password-protected, incomplete, or not a supported kind",
        ),
        (429, ErrorEnvelope, "Too many attachment requests"),
        AUTH_401,
    ),
)
async def upload_attachment(
    request: Request, user: CurrentUser, db: DbSession, storage: Storage
) -> JSONResponse:
    # The wire ceiling — refuse a huge body before buffering it. Not the size cap; see the
    # constant for why the two are different questions.
    content_length = request.headers.get("content-length")
    if (
        content_length is not None
        and content_length.isdigit()
        and int(content_length) > _BODY_LIMIT_BYTES
    ):
        raise AppApiError(413, "Attachment request is too large.")

    try:
        body: Any = await request.json()
    except (ValueError, TypeError):  # fmt: skip  # ruff py314 strips parens
        body = {}
    if not isinstance(body, dict):
        body = {}

    attachment_id = body.get("attachmentId")
    if not isinstance(attachment_id, str) or not _ID_RE.match(attachment_id):
        raise AppApiError(400, "Invalid attachment id.")
    media_type = body.get("mediaType")
    if not isinstance(media_type, str):
        raise AppApiError(400, "mediaType is required.")
    # THE `text/*` REFUSAL INVERTS FOR THE TWO DELIMITED FORMATS. It used to refuse every
    # text type, because text rode inside the prompt rather than being uploaded. That lane is
    # gone: every attachment is now an uploaded file with a stored identity, which is what lets a
    # chip be rebuilt on reload for every format by one fix. CSV and TSV are ordinary uploads.
    #
    # `text/plain` stays refused, and that is a WITHDRAWAL rather than an oversight — it works on
    # the branch today and stops. The mechanism argument for refusing it died with the inline
    # lane; the surviving reason is that no client requirement names it, and every format costs a
    # reader arm, refusal copy, a test and a line in the help page.
    if media_type.startswith("text/") and not is_code_lane(media_type):
        raise AppApiError(400, f"That file type is not supported. {ATTACHMENT_LANES_SENTENCE}")
    # Parsed ONCE here, before the branch, so every upload kind shares the same contract. The
    # optional conversation link is resolved owner-scoped here too (a bad conversationId 404s
    # before any bytes are parsed or stored — no orphaned object on the reject path).
    name = _attachment_name(body.get("name"))
    conversation_id = await _resolve_conversation_link(db, user.id, body.get("conversationId"))
    # THE THREE ADMISSION ARMS COLLAPSE INTO TWO. Office and deck each had their own,
    # because each ran a different server-side conversion before storing: docx/xlsx were extracted
    # to Markdown, and a deck was rendered to PDF by a converter that was never deployed. Both are
    # gone. A file is now stored as itself and read where it can actually be read, so what is left
    # is the routing rule and nothing else — the model reads these bytes, or code does.
    b64 = body.get("base64")
    # ASKED ONCE, FOR BOTH LANES, and it is the same refusal `_validate_attachment_bytes` returns
    # for an absent field — so the model lane's wording is unchanged by being asked here. It is
    # also the narrow everything below relies on, which is why it is a guard rather than a branch.
    if not isinstance(b64, str) or not b64:
        raise AppApiError(400, "Invalid attachment: missing bytes.")
    # WHICH LANE, decided once. The model reads images and PDFs itself; code in the workspace
    # reads everything else. Neither branch is a list of extensions the other has to stay in step
    # with — `is_code_lane` is the single answer both use.
    if not is_code_lane(media_type):
        err = _validate_attachment_bytes(media_type, b64)
        if err is not None:
            raise AppApiError(400, err)
    try:
        data = base64.b64decode(b64, validate=False)
    except (binascii.Error, ValueError):  # fmt: skip  # ruff py314 strips parens
        raise AppApiError(
            400, f"Attachment bytes do not match the declared type {media_type}."
        ) from None
    if len(data) > ATTACHMENT_MAX_BYTES:
        raise AppApiError(413, f"Attachment is too large (max {ATTACHMENT_MAX_MB} MB).")
    # AFTER the magic-byte and size checks and BEFORE the store, so a refused document leaves
    # no object and no row. Split on media type rather than run for everything because the two
    # scans read PDF syntax; an image has no trailer to look in.
    if media_type == PDF_MEDIA_TYPE:
        _assert_pdf_is_whole_and_unlocked(data, name)
    if is_code_lane(media_type):
        # AFTER the size check and BEFORE the store, like the PDF arm above: a refused file
        # leaves no object and no row. Password protection is checked in here too, for every
        # format that can carry it — a locked workbook gets the same sentence a locked PDF does,
        # rather than being stored, charged, and failing inside the sandbox several turns later.
        refusal = code_lane_refusal(media_type, name, data)
        if refusal is not None:
            raise AppApiError(415, refusal)
        # THE ARCHIVE BOUND, ON THE HALF OF THE LANE THAT ACTUALLY CARRIES ARCHIVES.
        # Office files are ZIPs, and one inside the size cap can declare gigabytes uncompressed.
        #
        # `is_opc_archive`, NOT `is_code_lane`, and the difference was a live defect: gated on the
        # whole lane this refused every CSV and TSV with "Malformed archive (no ZIP
        # end-of-central-directory)" — true about a file that was never an archive, and
        # unactionable to a citizen holding a normal spreadsheet export. Delimited files are bytes
        # of text with no central directory to bound; the size cap is their bound.
        #
        # Its previous three
        # callers were all server-side extraction arms that this work deletes, and its own suite
        # calls it directly — so it proves the algorithm and would never have told us it had gone
        # unwired. This path is stricter than what it replaces, not looser: the old office lane
        # extracted inside a killable, memory-capped subprocess and never stored a file it could
        # not read, while this one stores the archive and hands it to a reader in the citizen's
        # own sandbox, where neither that ceiling nor that deadline reaches.
        if is_opc_archive(media_type):
            try:
                assert_zip_not_bomb(data)
            except FileParseError as exc:
                raise AppApiError(413, str(exc)) from None

    ref = await _store_attachment_bytes(
        db, storage, user.id, attachment_id, media_type, name, conversation_id, data
    )
    kind = chip_kind_for(media_type)
    return JSONResponse(status_code=201, content={"attachment": {**ref, "kind": kind}})


async def _load_owned(db: DbSession, user_id: uuid.UUID, attachment_id: str) -> Attachment | None:
    result: Attachment | None = await db.scalar(
        sa.select(Attachment).where(
            Attachment.user_id == user_id, Attachment.attachment_id == attachment_id
        )
    )
    return result


@router.get(
    "/{attachment_id}",
    # Returns raw bytes (`Response`) — no response_model. Errors still documented.
    responses=error_responses(
        (400, ErrorEnvelope, "Invalid attachment id"),
        (404, ErrorEnvelope, "Attachment not found"),
        AUTH_401,
    ),
)
async def download_attachment(
    attachment_id: str, user: CurrentUser, db: DbSession, storage: Storage
) -> Response:
    if not _ID_RE.match(attachment_id):
        raise AppApiError(400, "Invalid attachment id.")
    att = await _load_owned(db, user.id, attachment_id)
    if att is None:
        raise AppApiError(404, "Attachment not found.")
    assert_owned(att.storage_key, user.id)
    try:
        data = await storage.get(att.storage_key)
    except StorageNotFoundError:
        raise AppApiError(404, "Attachment not found.") from None
    # Content-Type is SNIFFED from the bytes (not the stored media_type), matching Express.
    media = _sniff_media_type(data) or "application/octet-stream"
    return Response(
        content=data, media_type=media, headers={"Cache-Control": "private, max-age=3600"}
    )


@router.delete(
    "/{attachment_id}",
    response_model=OkResponse,
    dependencies=[Depends(_attachment_limiter)],
    responses=error_responses(
        (400, ErrorEnvelope, "Invalid attachment id"),
        (429, ErrorEnvelope, "Too many attachment requests"),
        AUTH_401,
    ),
)
async def delete_attachment(
    attachment_id: str, user: CurrentUser, db: DbSession, storage: Storage
) -> JSONResponse:
    if not _ID_RE.match(attachment_id):
        raise AppApiError(400, "Invalid attachment id.")
    att = await _load_owned(db, user.id, attachment_id)
    if att is not None:
        assert_owned(att.storage_key, user.id)
        key = att.storage_key
        # ★ ROW FIRST, BLOB SECOND — the same rollback discipline every other delete here follows.
        #
        # The old order deleted the object and then the row, so a commit that failed afterwards
        # left a row pointing at a blob that was already gone: the chip stays in the composer, the
        # file opens to nothing, and the only way out is another delete. That is the one
        # composer-reachable path to a dead row, and it is what this closes.
        #
        # THE TRADE IS REAL AND IS NOT FREE. Once the row is committed-deleted the blob is
        # invisible to every cleanup path we have — `reclaim_orphaned_attachments` is row-driven
        # and nothing anywhere lists the object store — so a failed sweep leaks the object for
        # good. `sweep_blobs` never raises and returns what survived, so the leak is at least
        # written down with the id it belonged to, which is the most a post-commit sweep can owe.
        #
        # NO DERIVED SIBLING TO SWEEP ANY MORE. A deck used to be rendered to PDF and the
        # `{key}.pdf` stored beside the original, so a delete had to remove both or leak one.
        # Nothing derives anything from an attachment now.
        await db.delete(att)
        await db.commit()
        survived = await sweep_blobs(storage, [key])
        if survived:
            logger.warning(
                "attachment_blob_sweep_survived",
                attachment_id=attachment_id,
                key_count=len(survived),
            )
    # Delete is always idempotent and 200, even when the id is unknown (Express behavior).
    return JSONResponse(content={"ok": True})
