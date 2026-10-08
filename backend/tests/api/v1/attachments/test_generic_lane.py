"""The generic chat's lanes: code-lane files only when the analysis runtime is configured, refusals
in wording it can honour, and every model-lane binary reaching the model with its own name.

Both doors decide on the conversation's KIND and on whether the runtime is configured — never on a
second allowlist. The unconfigured posture is a supported deployment, so its tests take no
analysis fixture at all. `tests/api/v1/attachments/conftest.py`
supplies `fake_storage`, autoused; the turn-driving fixtures (`_fresh_engine`, `_override_billing`,
`set_chat_model`) are imported rather than redeclared, exactly as `tests/api/v1/generic_chat/`
does for the same reason — this directory's own `conftest.py` carries none of them.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import uuid

import pytest
from openpyxl import Workbook
from pydantic_ai import BinaryContent
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy import func, select

from src.api.v1.attachments.router import (
    ATTACHMENT_LANES_SENTENCE,
    GENERIC_ATTACHMENT_LANES_SENTENCE,
    GENERIC_LANE_REFUSED_CODE,
)
from src.db.models.attachment import Attachment
from src.db.models.conversation import ChatKind
from src.db.models.message import Message, MessageEntryKind
from src.services.media import ALLOWED_MEDIA
from src.services.media.lanes import EXCEL_MEDIA_TYPE, MODEL_LANE_MEDIA
from src.services.messages.store import append_batch
from src.services.storage import attachment_key
from tests.api.v1.conversations.conftest import (
    _fresh_engine as _fresh_engine,
)
from tests.api.v1.conversations.conftest import (
    _headers,
)
from tests.api.v1.conversations.conftest import (
    _override_billing as _override_billing,
)
from tests.api.v1.conversations.conftest import (
    set_chat_model as set_chat_model,
)
from tests.factories import ConversationFactory, UserFactory
from tests.pdfs import pdf_with_pages

pytestmark = pytest.mark.usefixtures("_override_billing")

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


@pytest.fixture(autouse=True)
def _bind_storage_singleton(fake_storage):
    """The turns route's rehydrator reads storage through `chat_storage()`, which calls
    `get_storage()` directly — a different seam from the attachments router's own
    `storage_dependency`, which this directory's `conftest.py` already overrides with the same
    `fake_storage` instance. Binding it as the app-level singleton too is what makes a file
    uploaded through the router reachable by a turn's send-time rehydrator, matching
    `tests/journeys/test_journey_attach_chat.py`'s note on the same seam."""
    from src.services.storage import accessor as _storage_accessor

    _storage_accessor._backend_singleton = fake_storage
    yield
    _storage_accessor._backend_singleton = None


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _xlsx_bytes() -> bytes:
    workbook = Workbook()
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


async def _conversation(db_session, kind: ChatKind):
    user = await UserFactory.create(db_session)
    if kind is ChatKind.GENERIC:
        conversation = await ConversationFactory.create(
            db_session, user.id, kind=kind, project_id=None
        )
    else:
        conversation = await ConversationFactory.create(db_session, user.id, kind=kind)
    return user, conversation


async def _upload(client, user, conversation_id, attachment_id, media_type, data, *, name=None):
    body = {
        "conversationId": str(conversation_id),
        "attachmentId": attachment_id,
        "mediaType": media_type,
        "base64": _b64(data),
    }
    if name is not None:
        body["name"] = name
    return await client.post("/v1/attachments", headers=_headers(user), json=body)


async def _post_turn(client, user, conversation, *, text, attachment_ids=()):
    return await client.post(
        f"/v1/conversations/{conversation.id}/turns",
        headers=_headers(user),
        json={
            "message": {
                "text": text,
                "attachmentTexts": [],
                "attachmentIds": list(attachment_ids),
            }
        },
    )


async def _settle(engine, conversation_id) -> None:
    state = engine.peek(conversation_id)
    assert state is not None and state.task is not None
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(state.task, timeout=10)


def _user_prompt_contents(messages: list[ModelMessage]) -> list[object]:
    """Every content item of every user-prompt part, across the whole message list, in order.

    `object`, not `str | BinaryContent`: pydantic-ai's own content union is wider than what this
    codebase ever puts in a user part, and every call site below narrows with `isinstance`."""
    items: list[object] = []
    for message in messages:
        if not isinstance(message, ModelRequest):
            continue
        for part in message.parts:
            if not isinstance(part, UserPromptPart):
                continue
            content = part.content
            items.extend(content if isinstance(content, list) else [content])
    return items


async def _stored_spreadsheet(db_session, fake_storage, user, conversation, attachment_id):
    """A workbook the chat already holds, written past the upload door: a chat keeps the files it
    took while the runtime was configured."""
    data = _xlsx_bytes()
    key = attachment_key(user.id, uuid.uuid7())
    await fake_storage.put(key, data, content_type=EXCEL_MEDIA_TYPE)
    db_session.add(
        Attachment(
            user_id=user.id,
            attachment_id=attachment_id,
            media_type=EXCEL_MEDIA_TYPE,
            name="q3.xlsx",
            size=len(data),
            storage_key=key,
            conversation_id=conversation.id,
        )
    )
    await db_session.flush()


async def _sent_earlier(db_session, user, conversation, attachment_id) -> None:
    """An answered earlier turn that carried the file, recorded as the send route records one."""
    await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        messages=[
            ModelRequest(parts=[UserPromptPart(content="Here are the Q3 numbers.")]),
            ModelResponse(parts=[TextPart(content="I have them.")]),
        ],
        entry_kind=MessageEntryKind.TURN,
        kind=ChatKind.GENERIC,
        file_attachment_ids=[attachment_id],
    )


def _handed(engine, conversation_id) -> list[str]:
    """The code-lane files the started turn was handed, which its session is filled from."""
    state = engine.peek(conversation_id)
    assert state is not None
    return [file.attachment_id for file in state.attachments.files] if state.attachments else []


async def _messages_in(db_session, conversation_id) -> int:
    return await db_session.scalar(
        select(func.count()).select_from(Message).where(Message.conversation_id == conversation_id)
    )


def _answering(text: str) -> FunctionModel:
    async def _stream(_messages: list[ModelMessage], _info: AgentInfo):
        yield text

    return FunctionModel(stream_function=_stream)


# --- the lane constant itself -----------------------------------------------------------


def test_the_model_lane_constant_agrees_with_the_byte_gate() -> None:
    """`MODEL_LANE_MEDIA` cannot import `magic.ALLOWED_MEDIA` directly — `magic.py` already
    imports `is_code_lane` from `lanes.py`, and the reverse import would cycle — so a second
    literal carries the same five formats. This is what keeps the two from drifting apart.

    Mutation receipt: drop `image/webp` from `MODEL_LANE_MEDIA` and this goes red while every
    upload-route test stays green, because nothing else exercises this equality."""
    assert MODEL_LANE_MEDIA == frozenset(ALLOWED_MEDIA)


# --- the door: admitted, refused, sized -------------------------------------------------


async def test_a_pdf_and_an_image_are_accepted_on_a_generic_conversation(
    client, db_session, fake_storage
) -> None:
    user, conversation = await _conversation(db_session, ChatKind.GENERIC)

    pdf = await _upload(
        client,
        user,
        conversation.id,
        "att_pdf",
        "application/pdf",
        pdf_with_pages(1),
        name="brief.pdf",
    )
    assert pdf.status_code == 201, pdf.text

    image = await _upload(
        client, user, conversation.id, "att_img", "image/png", _PNG, name="photo.png"
    )
    assert image.status_code == 201, image.text
    # Liveness: both uploads really landed in the store, not just a 201 with nothing behind it.
    assert len(fake_storage.objects) == 2


async def test_a_spreadsheet_is_refused_on_a_generic_conversation_without_analysis(
    client, db_session, fake_storage
) -> None:
    """Refused with wording that names what this chat accepts and does not offer to open it with
    code, and nothing is stored — the store and the database, not just the status.

    Mutation receipt: drop the `is_code_lane(media_type)` half of the guard in
    `upload_attachment` (refuse every kind on a generic conversation) and
    `test_a_pdf_and_an_image_are_accepted_on_a_generic_conversation` goes red instead of this
    one — which is why that test exists beside it. Drop the `ChatKind.GENERIC` half instead (refuse
    a spreadsheet everywhere) and
    `test_a_code_lane_file_is_still_accepted_on_a_plan_or_build_conversation` goes red. Drop the
    runtime half and the configured test below goes red."""
    user, conversation = await _conversation(db_session, ChatKind.GENERIC)

    resp = await _upload(
        client, user, conversation.id, "att_xlsx", EXCEL_MEDIA_TYPE, _xlsx_bytes(), name="q3.xlsx"
    )

    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["error"]["message"] == GENERIC_ATTACHMENT_LANES_SENTENCE
    assert body["error"]["code"] == GENERIC_LANE_REFUSED_CODE
    # Names what IS accepted...
    assert (
        "picture" in body["error"]["message"].lower() or "pdf" in body["error"]["message"].lower()
    )
    # ...and never promises what this chat cannot do.
    assert "code" not in body["error"]["message"].lower()
    # Nothing landed in the object store...
    assert fake_storage.objects == {}
    # ...and no row was written either — the absence half, paired with the liveness above (a
    # crashed request would satisfy an empty store just as well as a correctly refused one).
    count = await db_session.scalar(
        select(func.count()).select_from(Attachment).where(Attachment.user_id == user.id)
    )
    assert count == 0


async def test_a_spreadsheet_is_accepted_on_a_generic_conversation_with_analysis(
    client, db_session, fake_storage, fake_analysis
) -> None:
    """Stored exactly as Plan and Build store it, and the session is not touched: nothing is
    copied in until a reply first opens a file."""
    user, conversation = await _conversation(db_session, ChatKind.GENERIC)

    resp = await _upload(
        client, user, conversation.id, "att_xlsx", EXCEL_MEDIA_TYPE, _xlsx_bytes(), name="q3.xlsx"
    )

    assert resp.status_code == 201, resp.text
    assert len(fake_storage.objects) == 1
    linked = await db_session.scalar(
        select(Attachment.conversation_id).where(Attachment.attachment_id == "att_xlsx")
    )
    assert linked == conversation.id
    assert fake_analysis.calls == []


_UNSUPPORTED = [
    ("text/plain", "That file type is not supported."),
    ("image/tiff", "Unsupported attachment type: image/tiff."),
]


@pytest.mark.parametrize(("media_type", "opening"), _UNSUPPORTED)
async def test_an_unsupported_format_on_a_generic_conversation_promises_no_code_without_analysis(
    client, db_session, fake_storage, media_type: str, opening: str
) -> None:
    user, conversation = await _conversation(db_session, ChatKind.GENERIC)

    resp = await _upload(client, user, conversation.id, "att_x", media_type, _PNG)

    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["message"] == f"{opening} {GENERIC_ATTACHMENT_LANES_SENTENCE}"


@pytest.mark.parametrize(("media_type", "opening"), _UNSUPPORTED)
async def test_an_unsupported_format_on_a_generic_conversation_names_both_lanes_with_analysis(
    client, db_session, fake_storage, fake_analysis, media_type: str, opening: str
) -> None:
    user, conversation = await _conversation(db_session, ChatKind.GENERIC)

    resp = await _upload(client, user, conversation.id, "att_x", media_type, _PNG)

    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["message"] == f"{opening} {ATTACHMENT_LANES_SENTENCE}"


async def test_an_oversize_pdf_on_a_generic_conversation_is_refused_for_size_before_storage(
    client, db_session, fake_storage
) -> None:
    """A file over the ceiling is refused for size before any upload begins: the generic kind
    narrows which FORMATS are accepted, never the number of bytes."""
    user, conversation = await _conversation(db_session, ChatKind.GENERIC)
    oversized = b"%PDF" + b"\x00" * (20 * 1024 * 1024 - 4 + 1)

    resp = await _upload(
        client, user, conversation.id, "att_huge", "application/pdf", oversized, name="huge.pdf"
    )

    assert resp.status_code == 413, resp.text
    assert resp.json() == {"error": {"message": "Attachment is too large (max 20 MB)."}}
    assert fake_storage.objects == {}


@pytest.mark.parametrize("kind", [ChatKind.PLAN, ChatKind.BUILD])
async def test_a_code_lane_file_is_still_accepted_on_a_plan_or_build_conversation(
    client, db_session, fake_storage, kind: ChatKind
) -> None:
    """The other direction — without this the change reads as a deletion rather than a
    narrowing."""
    user, conversation = await _conversation(db_session, kind)

    resp = await _upload(
        client, user, conversation.id, "att_xlsx", EXCEL_MEDIA_TYPE, _xlsx_bytes(), name="q3.xlsx"
    )

    assert resp.status_code == 201, resp.text
    assert len(fake_storage.objects) == 1


# --- the send door ----------------------------------------------------------------------


async def test_a_message_carrying_a_spreadsheet_is_refused_at_send_without_analysis(
    client, db_session, fake_storage, set_chat_model, _fresh_engine
) -> None:
    """The refusal is the upload door's, so the citizen reads one sentence for one cause, and
    nothing is recorded: no turn, no message."""
    user, conversation = await _conversation(db_session, ChatKind.GENERIC)
    await _stored_spreadsheet(db_session, fake_storage, user, conversation, "att_q3")
    set_chat_model(_answering("never reached"))

    resp = await _post_turn(
        client, user, conversation, text="Total the Q3 column.", attachment_ids=["att_q3"]
    )

    assert resp.status_code == 400, resp.text
    assert resp.json()["error"] == {
        "message": GENERIC_ATTACHMENT_LANES_SENTENCE,
        "code": GENERIC_LANE_REFUSED_CODE,
    }
    assert _fresh_engine.peek(conversation.id) is None
    assert await _messages_in(db_session, conversation.id) == 0


async def test_a_plain_question_in_a_chat_holding_spreadsheets_is_answered_without_analysis(
    client, db_session, fake_storage, set_chat_model, _fresh_engine
) -> None:
    """A reply that opens no file needs no runtime.

    Mutation receipt: refuse whenever the chat holds a code-lane file, rather than only when this
    message carries one, and this goes red with a 400."""
    user, conversation = await _conversation(db_session, ChatKind.GENERIC)
    await _stored_spreadsheet(db_session, fake_storage, user, conversation, "att_q3")
    await _sent_earlier(db_session, user, conversation, "att_q3")
    set_chat_model(_answering("I can also help with PDFs and pictures."))

    resp = await _post_turn(client, user, conversation, text="What else can you help with?")

    assert resp.status_code == 202, resp.text
    # Liveness: the chat really does hold the earlier file, so the refusal had something to see.
    assert _handed(_fresh_engine, conversation.id) == ["att_q3"]
    await _settle(_fresh_engine, conversation.id)
    assert _fresh_engine.peek(conversation.id).status == "completed"


async def test_a_generic_turn_is_handed_its_own_sent_spreadsheets_with_analysis(
    client, db_session, fake_storage, fake_analysis, set_chat_model, _fresh_engine
) -> None:
    """The set the reply's session is filled from: the file on the turn that sends it, and the
    same file on a later turn that carries none."""
    user, conversation = await _conversation(db_session, ChatKind.GENERIC)
    set_chat_model(_answering("ok"))
    up = await _upload(
        client, user, conversation.id, "att_q3", EXCEL_MEDIA_TYPE, _xlsx_bytes(), name="q3.xlsx"
    )
    assert up.status_code == 201, up.text

    first = await _post_turn(
        client, user, conversation, text="Total the Q3 column.", attachment_ids=["att_q3"]
    )
    assert first.status_code == 202, first.text
    assert _handed(_fresh_engine, conversation.id) == ["att_q3"]
    await _settle(_fresh_engine, conversation.id)

    second = await _post_turn(client, user, conversation, text="And the average?")
    assert second.status_code == 202, second.text
    assert _handed(_fresh_engine, conversation.id) == ["att_q3"]
    await _settle(_fresh_engine, conversation.id)


async def test_a_spreadsheet_from_another_of_the_users_chats_is_refused_at_send(
    client, db_session, fake_storage, fake_analysis, set_chat_model, _fresh_engine
) -> None:
    """A chat's session is named by that chat alone, so a file linked to another chat never joins
    this chat's set. Named here, it falls through to the model lane, which refuses it.

    Mutation receipt: drop `this_chat_only` from the send route and this turn starts with the
    other chat's file in its set."""
    user, here = await _conversation(db_session, ChatKind.GENERIC)
    elsewhere = await ConversationFactory.create(
        db_session, user.id, kind=ChatKind.GENERIC, project_id=None
    )
    set_chat_model(_answering("never reached"))
    up = await _upload(
        client, user, elsewhere.id, "att_elsewhere", EXCEL_MEDIA_TYPE, _xlsx_bytes(), name="p.xlsx"
    )
    assert up.status_code == 201, up.text

    resp = await _post_turn(
        client, user, here, text="Total this.", attachment_ids=["att_elsewhere"]
    )

    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["message"] == (
        "an attached file no longer matches its declared type; attach it again"
    )
    assert _fresh_engine.peek(here.id) is None
    assert await _messages_in(db_session, here.id) == 0
    assert fake_analysis.calls == []


async def test_another_users_spreadsheet_named_in_a_generic_message_is_refused_and_never_placed(
    client, db_session, fake_storage, fake_analysis, set_chat_model, _fresh_engine
) -> None:
    """Two predicates hold this, the owner scope and the chat's own link, so it goes red only
    with both removed from the code-lane read."""
    user, conversation = await _conversation(db_session, ChatKind.GENERIC)
    stranger, theirs = await _conversation(db_session, ChatKind.GENERIC)
    set_chat_model(_answering("never reached"))
    up = await _upload(
        client, stranger, theirs.id, "att_theirs", EXCEL_MEDIA_TYPE, _xlsx_bytes(), name="p.xlsx"
    )
    assert up.status_code == 201, up.text

    resp = await _post_turn(
        client, user, conversation, text="Total this.", attachment_ids=["att_theirs"]
    )

    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["message"] == (
        "an attached file is no longer available; remove it and attach it again"
    )
    assert _fresh_engine.peek(conversation.id) is None
    assert await _messages_in(db_session, conversation.id) == 0
    assert fake_analysis.calls == []


# --- the label: naming a binary to the model --------------------------------------------


async def test_two_pdfs_each_reach_the_model_with_their_own_name(
    client, db_session, set_chat_model, _fresh_engine
) -> None:
    """A follow-up naming one of several attached documents must be tied to the right file.
    Proven by pairing each label with the binary that follows it, keyed on the exact bytes — a
    swapped pairing would fail this even though both files still reached the model.

    Mutation receipt: stop emitting `_attachment_label` in `resolve_binaries` and the content
    list holds two `BinaryContent`s with no preceding label, so `pairs` below comes back with
    identifiers instead of names and the final equality goes red."""
    user, conversation = await _conversation(db_session, ChatKind.GENERIC)
    north = pdf_with_pages(1)
    south = pdf_with_pages(2)

    up_north = await _upload(
        client,
        user,
        conversation.id,
        "att_north",
        "application/pdf",
        north,
        name="north-runway.pdf",
    )
    assert up_north.status_code == 201, up_north.text
    up_south = await _upload(
        client,
        user,
        conversation.id,
        "att_south",
        "application/pdf",
        south,
        name="south-runway.pdf",
    )
    assert up_south.status_code == 201, up_south.text

    seen: list[list[ModelMessage]] = []

    async def _capture(messages: list[ModelMessage], _info: AgentInfo):
        seen.append(list(messages))
        yield "The south runway document has two pages."

    set_chat_model(FunctionModel(stream_function=_capture))

    resp = await _post_turn(
        client,
        user,
        conversation,
        text="How many pages does the south runway document have?",
        attachment_ids=["att_north", "att_south"],
    )
    assert resp.status_code == 202, resp.text
    await _settle(_fresh_engine, conversation.id)
    assert len(seen) == 1

    content = _user_prompt_contents(seen[0])
    pairs: dict[str, bytes] = {}
    for index, item in enumerate(content):
        if isinstance(item, BinaryContent):
            label = content[index - 1]
            assert isinstance(label, str), "a binary must be preceded by its name label"
            pairs[label] = item.data

    assert pairs == {
        '<attachment name="north-runway.pdf"/>': north,
        '<attachment name="south-runway.pdf"/>': south,
    }

    # And the question really was answerable: the reply streamed back through the transcript.
    detail = await client.get(f"/v1/conversations/{conversation.id}", headers=_headers(user))
    assert detail.status_code == 200, detail.text
    texts = [
        item["text"] for item in detail.json()["projection"] if item["type"] == "assistant_text"
    ]
    assert texts == ["The south runway document has two pages."]


async def test_an_attachment_from_turn_one_still_carries_its_name_on_turn_three(
    client, db_session, set_chat_model, _fresh_engine
) -> None:
    """Edge case: an attachment sent on turn one is still present — labeled — in the content sent
    on turn three. The label is a plain string riding beside the binary in the SAME persisted
    content list, so it survives the reference-marker round trip with no store.py change: neither
    `_externalize_binaries` nor `_swap_refs` touches a node that is not a `kind: binary` dict.

    Mutation receipt: same as the two-PDF test above — remove the label and turn three's history
    holds the binary with no preceding name string."""
    user, conversation = await _conversation(db_session, ChatKind.GENERIC)
    brief = pdf_with_pages(1)
    up = await _upload(
        client, user, conversation.id, "att_brief", "application/pdf", brief, name="ops-brief.pdf"
    )
    assert up.status_code == 201, up.text

    seen: list[list[ModelMessage]] = []

    async def _capture(messages: list[ModelMessage], _info: AgentInfo):
        seen.append(list(messages))
        yield "ok"

    set_chat_model(FunctionModel(stream_function=_capture))

    first = await _post_turn(
        client, user, conversation, text="Summarize this.", attachment_ids=["att_brief"]
    )
    assert first.status_code == 202, first.text
    await _settle(_fresh_engine, conversation.id)

    second = await _post_turn(client, user, conversation, text="Go on.")
    assert second.status_code == 202, second.text
    await _settle(_fresh_engine, conversation.id)

    third = await _post_turn(client, user, conversation, text="One more thing.")
    assert third.status_code == 202, third.text
    await _settle(_fresh_engine, conversation.id)

    assert len(seen) == 3
    content = _user_prompt_contents(seen[2])
    binaries = [item for item in content if isinstance(item, BinaryContent)]
    assert len(binaries) == 1, "the file from turn one must still be in turn three's history"
    assert binaries[0].data == brief

    index = content.index(binaries[0])
    assert content[index - 1] == '<attachment name="ops-brief.pdf"/>'
