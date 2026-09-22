"""The generic chat's own lane: model-only, refused in wording it can honour, and every
model-lane binary reaches the model with its own name.

The upload route already resolves the conversation before admitting a file, so the refusal below
runs on the conversation's KIND — never a second allowlist. `tests/api/v1/attachments/conftest.py`
supplies `fake_storage`, autoused; the turn-driving fixtures (`_fresh_engine`, `_override_billing`,
`set_chat_model`) are imported rather than redeclared, exactly as `tests/api/v1/generic_chat/`
does for the same reason — this directory's own `conftest.py` carries none of them.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io

import pytest
from openpyxl import Workbook
from pydantic_ai import BinaryContent
from pydantic_ai.messages import ModelMessage, ModelRequest, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy import func, select

from src.api.v1.attachments.router import (
    ATTACHMENT_MAX_BYTES,
    ATTACHMENT_MAX_MB,
    GENERIC_ATTACHMENT_LANES_SENTENCE,
    GENERIC_LANE_REFUSED_CODE,
)
from src.db.models.attachment import Attachment
from src.db.models.conversation import ChatKind
from src.services.media import ALLOWED_MEDIA
from src.services.media.lanes import EXCEL_MEDIA_TYPE, MODEL_LANE_MEDIA
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


async def test_a_spreadsheet_is_refused_on_a_generic_conversation_with_honest_wording(
    client, db_session, fake_storage
) -> None:
    """Refused with wording that names what this chat accepts and does not offer to open it with
    code, and nothing is stored — the store and the database, not just the status.

    Mutation receipt: drop the `is_code_lane(media_type)` half of the guard in
    `upload_attachment` (refuse every kind on a generic conversation) and
    `test_a_pdf_and_an_image_are_accepted_on_a_generic_conversation` goes red instead of this
    one — which is why that test exists beside it. Drop the `ChatKind.GENERIC` half instead (refuse
    a spreadsheet everywhere) and
    `test_a_code_lane_file_is_still_accepted_on_a_plan_or_build_conversation` goes red."""
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


async def test_an_oversize_pdf_on_a_generic_conversation_is_refused_for_size_before_storage(
    client, db_session, fake_storage
) -> None:
    """A file over the ceiling is refused for size before any upload begins: the generic kind
    narrows which FORMATS are accepted, never the number of bytes."""
    user, conversation = await _conversation(db_session, ChatKind.GENERIC)
    oversized = b"%PDF" + b"\x00" * (ATTACHMENT_MAX_BYTES - 4 + 1)

    resp = await _upload(
        client, user, conversation.id, "att_huge", "application/pdf", oversized, name="huge.pdf"
    )

    assert resp.status_code == 413, resp.text
    assert resp.json() == {
        "error": {"message": f"Attachment is too large (max {ATTACHMENT_MAX_MB} MB)."}
    }
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
