"""The U10 turn transport, end to end: POST starts a detached turn, GET subscribes with
catch-up-snapshot-then-tail, stop is the explicit cancel, and the frame union parses with
the callable-discriminator discipline (malformed KNOWN tag raises; unknown tag captured).

Also home to the HTTP-level no-overrides kind-gating proof U8 deferred here: a Plan-kind
turn's model-visible tool list carries no write tools, through the REAL route + engine.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from typing import Any, get_args

import pytest
from pydantic import Tag, ValidationError
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, FunctionModel

from src.api.v1.build_sessions.schemas import ErrorSource
from src.api.v1.conversations.schemas import (
    _KNOWN_FRAME_TAGS,
    TURN_STREAM_FRAME_ADAPTER,
    DiagnosticFrame,
    PreviewFrame,
    QuotaFrame,
    SnapshotFrame,
    TurnEndedFrame,
    TurnStreamFrame,
    UnknownFrame,
    WorkspaceFrame,
)
from src.api.v1.conversations.turns import KEEPALIVE_SECONDS
from src.config import settings
from src.db.models.conversation import ChatKind
from src.services.auth.csrf import issue_csrf_token
from src.services.auth.session_jwt import mint_session_jwt
from src.services.turns import engine as engine_module
from src.services.turns.engine import TurnEngine, _TurnState, set_turn_engine_for_tests
from src.services.turns.guard import _mid_reply
from tests.factories import ConversationFactory, UserFactory

_TTL = settings.auth.access_ttl_seconds


def _headers(user, *, with_csrf: bool = True) -> dict[str, str]:
    jwt = mint_session_jwt(user.id, user.token_version, _TTL)
    if not with_csrf:
        return {"Cookie": f"session={jwt}"}
    csrf = issue_csrf_token(user.id, user.token_version)
    return {"Cookie": f"session={jwt}; csrf={csrf}", "X-CSRF-Token": csrf}


@pytest.fixture(autouse=True)
def _fresh_engine():
    _mid_reply.clear()
    engine = TurnEngine()
    set_turn_engine_for_tests(engine)
    yield engine
    set_turn_engine_for_tests(None)
    _mid_reply.clear()


@pytest.fixture(autouse=True)
def _override_billing(app, db_session) -> None:
    from src.api.v1.claude.router import billing_session_factory

    @contextlib.asynccontextmanager
    async def _session():
        yield db_session

    app.dependency_overrides[billing_session_factory] = lambda: lambda: _session()


@pytest.fixture
def set_chat_model(app):
    def _set(model) -> None:
        from src.api.v1.claude.router import chat_model

        app.dependency_overrides[chat_model] = lambda: model

    return _set


async def _auth_with_conversation(db_session, *, kind=None):
    user = await UserFactory.create(db_session)
    conv = await ConversationFactory.create(
        db_session, user.id, kind=kind if kind is not None else ChatKind.PLAN
    )
    return user, conv


def _streaming_text(*chunks: str):
    async def _stream(messages: list[ModelMessage], info: AgentInfo):
        for chunk in chunks:
            yield chunk

    return FunctionModel(stream_function=_stream)


def _frames_of(sse_text: str) -> list:
    """Parse an SSE body's data payloads through the validating union (skips pings and the
    [DONE] sentinel)."""
    frames = []
    for block in sse_text.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: "):
                payload = line[len("data: ") :]
                if payload == "[DONE]":
                    continue
                frames.append(TURN_STREAM_FRAME_ADAPTER.validate_python(json.loads(payload)))
    return frames


async def _post_turn(client, headers, conv, text="hello"):
    return await client.post(
        f"/v1/conversations/{conv.id}/turns",
        headers=headers,
        json={"message": {"text": text, "attachmentTexts": [], "attachmentIds": []}},
    )


async def _settle(engine, conversation_id) -> None:
    state = engine.peek(conversation_id)
    assert state is not None and state.task is not None
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(state.task, timeout=10)


# --- the happy path ---------------------------------------------------------------------


async def test_post_202_then_stream_replays_full_turn(
    client, db_session, set_chat_model, _fresh_engine
) -> None:
    user, conv = await _auth_with_conversation(db_session)
    set_chat_model(_streaming_text("hello ", "world"))
    headers = _headers(user)

    resp = await _post_turn(client, headers, conv)
    assert resp.status_code == 202, resp.text
    turn_id = resp.json()["turnId"]
    await _settle(_fresh_engine, conv.id)

    events = await client.get(f"/v1/conversations/{conv.id}/events", headers=_headers(user))
    assert events.status_code == 200
    assert events.headers["content-type"].startswith("text/event-stream")
    assert events.text.endswith("data: [DONE]\n\n")
    frames = _frames_of(events.text)
    # Ended turn within TTL: the snapshot IS the terminal (turn_status settled) + replay.
    assert frames[0].type == "snapshot"
    assert frames[0].turn_id == turn_id
    assert frames[0].turn_status == "completed"
    assert frames[0].text_so_far == "hello world"
    # The persisted user turn rides the snapshot's projected items.
    assert any(item.type == "user_text" and item.text == "hello" for item in frames[0].items)


async def test_mid_turn_subscribe_gets_snapshot_then_tail(
    client, db_session, set_chat_model, _fresh_engine
) -> None:
    gate = asyncio.Event()

    async def _paced(messages: list[ModelMessage], info: AgentInfo):
        yield "first "
        await gate.wait()
        yield "second"

    user, conv = await _auth_with_conversation(db_session)
    set_chat_model(FunctionModel(stream_function=_paced))
    resp = await _post_turn(client, _headers(user), conv)
    assert resp.status_code == 202

    state = _fresh_engine.peek(conv.id)
    assert state is not None
    while not (
        state.text_parts or state.pending_text
    ):  # the first delta is out — we are genuinely mid-turn
        await asyncio.sleep(0.01)

    # httpx's ASGITransport buffers a streaming response until the app completes, so the
    # GET rides a concurrent task: it SUBSCRIBES mid-turn (proven below by the snapshot's
    # `running` status — the snapshot is built at request time), then the gate releases
    # and the buffered result carries snapshot + tail.
    reader = asyncio.create_task(
        client.get(f"/v1/conversations/{conv.id}/events", headers=_headers(user))
    )
    while not state.subscribers:  # the route registered its queue — subscription is live
        await asyncio.sleep(0.01)
    gate.set()
    events = await asyncio.wait_for(reader, timeout=10)

    frames = _frames_of(events.text)
    assert frames[0].type == "snapshot"
    assert frames[0].turn_status == "running"  # built BEFORE the release: genuinely mid-turn
    # THE SNAPSHOT'S TEXT IS EMPTY MID-RESPONSE, and that is U4's stated cost rather than a
    # defect. Prose is held until its response ends and proves it called no tool, so a client
    # that reconnects mid-answer sees the steps and no partial text. The snapshot is still
    # doing its job — it reports the turn is RUNNING, which is what stops the client showing
    # a finished-looking transcript — and the answer arrives whole the moment it is released.
    assert frames[0].text_so_far == ""
    # Snapshot + tail still converge on the whole answer, which is the property that actually
    # matters to a reconnecting reader: nothing is lost and nothing is doubled.
    deltas = "".join(f.text for f in frames if f.type == "text_delta")
    assert frames[0].text_so_far + deltas == "first second"
    assert frames[-1].type == "turn_ended" and frames[-1].status == "completed"
    assert events.text.endswith("data: [DONE]\n\n")

    # SETTLE THE TASK, not just the stream. `turn_ended` is delivered from inside the run, and
    # the run's `finally` still has work after it — it drains the preview watcher and writes the
    # durable turn-terminal row, which opens a session of its own. A test that returns on the
    # last frame leaves that session unclosed and the connection is torn down by the garbage
    # collector against an event loop pytest has already closed, which surfaces as an error at
    # teardown rather than as a failure here.
    await _settle(_fresh_engine, conv.id)


async def test_the_acknowledgement_actually_reaches_a_subscriber(
    client, db_session, set_chat_model, _fresh_engine
) -> None:
    """★ THE DELIVERY TEST, and the one whose absence hid the bug.

    U17's acknowledgement had three passing tests — emitted before any model request, replaced by
    the first real step, never persisted — and reached NOBODY. All three asserted against the
    in-memory ring; none asserted against the stream a client actually reads.

    The mechanism: the ack is `seq == 1`, emitted synchronously inside `start_turn` before the
    detached task exists. Every client POSTs the turn and only then opens the stream, so by the
    time it subscribes the route builds a snapshot, sets `last_sent = snapshot.seq` (already past
    1), and yields only `seq > last_sent`. The ack frame was behind the cursor before anyone could
    see it, and it was deliberately kept out of `state.steps`, so it was in neither the snapshot
    nor the tail.

    So this test subscribes exactly the way the portal does — POST, then GET with no cursor — and
    asserts the acknowledgement is in the DELIVERED frames. Assert against the wire, not the ring:
    that distinction is the entire defect."""
    gate = asyncio.Event()

    async def _paced(messages: list[ModelMessage], info: AgentInfo):
        await gate.wait()
        yield "done"

    user, conv = await _auth_with_conversation(db_session)
    set_chat_model(FunctionModel(stream_function=_paced))
    resp = await _post_turn(client, _headers(user), conv)
    assert resp.status_code == 202

    state = _fresh_engine.peek(conv.id)
    assert state is not None

    reader = asyncio.create_task(
        client.get(f"/v1/conversations/{conv.id}/events", headers=_headers(user))
    )
    while not state.subscribers:
        await asyncio.sleep(0.01)
    gate.set()
    events = await asyncio.wait_for(reader, timeout=10)

    frames = _frames_of(events.text)
    assert frames[0].type == "snapshot"
    # Liveness first: the stream really carried this turn, so the assertion below is about the
    # acknowledgement's absence-or-presence and not about an empty read.
    assert frames[-1].type == "turn_ended"

    delivered = events.text
    assert engine_module.ACK_TEXT in delivered, (
        "the acknowledgement never reached the subscriber — it is emitted at seq 1, before any "
        "client can connect, so it has to ride the snapshot"
    )

    # SETTLE THE TASK, not just the stream. `turn_ended` is delivered from inside the run, and
    # the run's `finally` still has work after it — it drains the preview watcher and writes the
    # durable turn-terminal row, which opens a session of its own. A test that returns on the
    # last frame leaves that session unclosed and the connection is torn down by the garbage
    # collector against an event loop pytest has already closed, which surfaces as an error at
    # teardown rather than as a failure here.
    await _settle(_fresh_engine, conv.id)


async def test_second_post_while_running_is_409(
    client, db_session, set_chat_model, _fresh_engine
) -> None:
    gate = asyncio.Event()

    async def _stall(messages: list[ModelMessage], info: AgentInfo):
        yield "…"
        await gate.wait()
        yield "done"

    user, conv = await _auth_with_conversation(db_session)
    set_chat_model(FunctionModel(stream_function=_stall))
    headers = _headers(user)
    assert (await _post_turn(client, headers, conv)).status_code == 202
    second = await _post_turn(client, headers, conv, text="again")
    assert second.status_code == 409
    gate.set()
    await _settle(_fresh_engine, conv.id)


async def test_stop_endpoint_cancels_and_record_stays_truthful(
    client, db_session, set_chat_model, _fresh_engine
) -> None:
    gate = asyncio.Event()

    async def _stall(messages: list[ModelMessage], info: AgentInfo):
        yield "partial "
        await gate.wait()
        yield "never"

    user, conv = await _auth_with_conversation(db_session)
    set_chat_model(FunctionModel(stream_function=_stall))
    headers = _headers(user)
    turn_id = (await _post_turn(client, headers, conv)).json()["turnId"]

    state = _fresh_engine.peek(conv.id)
    assert state is not None
    while not (state.text_parts or state.pending_text):
        await asyncio.sleep(0.01)

    stop = await client.post(f"/v1/conversations/{conv.id}/turns/{turn_id}/stop", headers=headers)
    assert stop.status_code == 200 and stop.json()["status"] == "stopping"
    await _settle(_fresh_engine, conv.id)
    assert state.status == "stopped"

    again = await client.post(f"/v1/conversations/{conv.id}/turns/{turn_id}/stop", headers=headers)
    assert again.status_code == 200 and again.json()["status"] == "already_settled"

    unknown = await client.post(
        f"/v1/conversations/{conv.id}/turns/{uuid.uuid4()}/stop", headers=headers
    )
    assert unknown.status_code == 409  # not this conversation's in-flight turn


async def test_model_failure_travels_in_band(
    client, db_session, set_chat_model, _fresh_engine
) -> None:
    async def _explode(messages: list[ModelMessage], info: AgentInfo):
        yield "before "
        raise RuntimeError("upstream fell over")

    user, conv = await _auth_with_conversation(db_session)
    set_chat_model(FunctionModel(stream_function=_explode))
    assert (await _post_turn(client, _headers(user), conv)).status_code == 202
    await _settle(_fresh_engine, conv.id)

    events = await client.get(f"/v1/conversations/{conv.id}/events", headers=_headers(user))
    frames = _frames_of(events.text)
    assert frames[0].type == "snapshot" and frames[0].turn_status == "failed"
    # The in-band error + failed terminal are in the ring replay for a cursor resume;
    # a fresh subscriber reads the settled snapshot. Both truths, one record.
    state = _fresh_engine.peek(conv.id)
    assert state is not None
    types = [f.type for f in state.ring]
    assert "error" in types and types[-1] == "turn_ended"


# --- ownership + gating -------------------------------------------------------------------


async def test_cross_user_conversation_is_404_everywhere(
    client, db_session, set_chat_model, _fresh_engine
) -> None:
    owner, conv = await _auth_with_conversation(db_session)
    other = await UserFactory.create(db_session, email="other@rvaiglobal.com")
    set_chat_model(_streaming_text("x"))
    assert (await _post_turn(client, _headers(other), conv)).status_code == 404
    assert (
        await client.get(f"/v1/conversations/{conv.id}/events", headers=_headers(other))
    ).status_code == 404
    assert (
        await client.post(
            f"/v1/conversations/{conv.id}/turns/{uuid.uuid4()}/stop", headers=_headers(other)
        )
    ).status_code == 404


async def test_plan_kind_model_sees_no_write_tools(
    client, db_session, set_chat_model, _fresh_engine
) -> None:
    """The HTTP-level no-overrides gating proof (U8's deferred test): through the REAL
    route, engine, and toolsets, a Plan turn's model-visible tool list is exactly the read
    surface plus the plan-confirmation tool — no write_file / edit_file / insert_lines /
    declare_done.

    THIS USED TO PIN A DISTINCT "ASK" SURFACE WITHOUT `present_plan_options` (Ask's old
    three-valued-mode table had reads but no confirmation tool; only Plan carried it). Ask and
    Plan collapsed into the one `ChatKind.PLAN` (see `db/models/conversation.py`), and
    `toolsets_for_kind` hands every Plan-kind run the confirmation tool along with the read
    surface — there is no longer a read-only-without-the-card surface to assert, so the
    expected set below includes `present_plan_options`."""
    seen: dict[str, set[str]] = {}

    async def _capture(messages: list[ModelMessage], info: AgentInfo):
        seen["tools"] = {t.name for t in info.function_tools}
        yield "grounded answer"

    user, conv = await _auth_with_conversation(db_session)
    set_chat_model(FunctionModel(stream_function=_capture))
    assert (await _post_turn(client, _headers(user), conv)).status_code == 202
    await _settle(_fresh_engine, conv.id)
    assert seen["tools"] == {
        "read_file",
        "list_files",
        "search_files",
        "run_command",
        "present_plan_options",
        # The shared conversation toolset — carried by BOTH kinds, because it is about the
        # person waiting rather than about what this run can do. It is in an exact-set
        # assertion on purpose: a tool meant for both arms that reached only one is a drift
        # this test should catch, and a subset check would not.
        "tell_the_user",
    }


async def test_write_mode_accepts_a_send_like_every_other_mode(
    client, db_session, set_chat_model, _fresh_engine
) -> None:
    """THE FLIP. Write used to 400 here with copy telling the user to switch modes — which was
    a real refusal for a real reason (Write had no toolset and no composable prompt) and is a
    lie now. A citizen who just built something can keep talking to it in the mode they are
    already in, which is the whole of N6."""
    from src.db.models.conversation import ChatKind

    user = await UserFactory.create(db_session)
    conv = await ConversationFactory.create(db_session, user.id, kind=ChatKind.BUILD)
    set_chat_model(_streaming_text("x"))
    resp = await _post_turn(client, _headers(user), conv)
    assert resp.status_code == 202
    await _settle(_fresh_engine, conv.id)


async def test_a_live_build_in_this_thread_refuses_the_turn(
    client, db_session, set_chat_model, _fresh_engine, building
) -> None:
    """THE ONE GATE, server side. While the agent is building this app, this thread takes no
    chat turn — the portal shuts its composer for the same window, and this is what holds when
    the portal is stale, reloaded, or simply not the thing making the request.

    It is a LIVENESS check, not the Write-mode check: the two genuinely disagree (a build's
    first seconds run before the transition flips the mode, and `POST /build-sessions` never
    flips it at all), so the mode check alone would let a turn straight in."""
    user, conv = await _auth_with_conversation(db_session)
    set_chat_model(_streaming_text("should never stream"))

    with building(conv.id, user.id):
        refused = await _post_turn(client, _headers(user), conv)

    assert refused.status_code == 409
    # Citizen copy: what is happening, and when they get the chat back. Nothing internal.
    message = refused.json()["error"]["message"]
    assert "building your app" in message
    assert "as soon as it finishes" in message
    assert _fresh_engine.peek(conv.id) is None  # nothing started

    # …and the moment the build is over the very same send goes through. Chat re-enables.
    assert (await _post_turn(client, _headers(user), conv)).status_code == 202
    await _settle(_fresh_engine, conv.id)


async def test_a_build_in_another_thread_now_refuses_this_one_by_name(
    client, db_session, set_chat_model, _fresh_engine, building
) -> None:
    """★ AE6 / R93 — AND THIS EXPECTATION IS THE OPPOSITE OF WHAT IT USED TO BE.

    It used to read: "per-conversation, not per-user — a planning conversation elsewhere is
    legitimate traffic, and gating it would be the over-correction." That was true while a
    planning turn read a SAVED COPY of the app and touched no container. It reads the project's
    live workspace now, like every other turn, so a send here while another of this user's chats
    holds that workspace is not incidental traffic — it is a second claim on the one thing there
    is only one of.

    TWO QUESTIONS, STILL DISTINCT. "Is this chat's own agent mid-reply?" is per-conversation and
    unchanged. "Is this user's one workspace already committed elsewhere?" is per-user, and it
    is the one that changed. The refusal carries a machine code so a client can tell it from the
    other 409 on this route, which has a different cause and a different remedy."""
    from src.services.turns.copy import ALREADY_BUILDING_HERE_CODE
    from tests.factories import ConversationFactory

    user, conv = await _auth_with_conversation(db_session)
    other = await ConversationFactory.create(db_session, user.id)
    set_chat_model(_streaming_text("planning away"))

    with building(other.id, user.id):
        resp = await _post_turn(client, _headers(user), conv)

    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == ALREADY_BUILDING_HERE_CODE
    assert _fresh_engine.peek(conv.id) is None  # refused BEFORE anything was claimed


async def test_the_refused_send_is_not_stored_and_bills_nothing(
    client, db_session, set_chat_model, _fresh_engine, building
) -> None:
    """★ AE51's server half. A refusal that costs the citizen their message is a worse bug than
    the conflict it reports: they retype it, or they do not, and either way the platform took
    something for nothing."""
    import sqlalchemy as sa

    from src.db.models.message import Message
    from src.db.models.token_usage import TokenUsage
    from tests.factories import ConversationFactory

    user, conv = await _auth_with_conversation(db_session)
    other = await ConversationFactory.create(db_session, user.id)
    set_chat_model(_streaming_text("planning away"))

    with building(other.id, user.id):
        assert (await _post_turn(client, _headers(user), conv)).status_code == 409

    rows = await db_session.scalar(
        sa.select(sa.func.count()).select_from(Message).where(Message.conversation_id == conv.id)
    )
    assert (rows or 0) == 0
    usage = await db_session.scalar(
        sa.select(sa.func.count()).select_from(TokenUsage).where(TokenUsage.user_id == user.id)
    )
    assert (usage or 0) == 0


async def test_two_plan_chats_can_both_be_open_and_only_sending_takes_the_slot(
    client, db_session, set_chat_model, _fresh_engine
) -> None:
    """Open is free; SENDING is what claims. The gate is on the turn, not on the chat, so a
    citizen can keep several planning conversations in front of them — which is the whole point
    of them being cheap — and only discovers the one-workspace rule when two of them are
    actually working at once."""
    from tests.factories import ConversationFactory

    user, first = await _auth_with_conversation(db_session)
    second = await ConversationFactory.create(
        db_session, user.id, project_id=first.project_id, kind=ChatKind.PLAN
    )
    set_chat_model(_streaming_text("answering"))

    assert (await _post_turn(client, _headers(user), first)).status_code == 202
    await _settle(_fresh_engine, first.id)
    assert (await _post_turn(client, _headers(user), second)).status_code == 202
    await _settle(_fresh_engine, second.id)


# --- R98: no workspace service means the message is refused, not degraded ------------------


@pytest.mark.parametrize("kind", [ChatKind.PLAN, ChatKind.BUILD])
async def test_no_workspace_service_refuses_the_send_identically_in_both_kinds(
    client, db_session, set_chat_model, _fresh_engine, no_workspace_service, kind
) -> None:
    """★ AE53. Both kinds, one answer, said at the moment of sending.

    WHAT THIS REPLACED WAS SILENCE. A turn with no sandbox service configured used to answer
    from the last SAVED copy of the app — a degradation the citizen was never told about,
    wearing a branch on the chat's mode even though the condition it read was a deployment
    fact. Both kinds read the live app and only the live app now, so where there is nothing to
    read from, the honest thing is to refuse before the message is spent.

    The code is asserted, not just the status: this 503 shares its family with nothing else on
    this route, but the codes are how a browser tells the whole refusal family apart."""
    import sqlalchemy as sa

    from src.db.models.message import Message
    from src.services.turns.copy import WORKSPACE_UNAVAILABLE_CODE

    user, conv = await _auth_with_conversation(db_session, kind=kind)
    set_chat_model(_streaming_text("never reached"))

    resp = await _post_turn(client, _headers(user), conv, text="what does my app do?")

    assert resp.status_code == 503, resp.text
    assert resp.json()["error"]["code"] == WORKSPACE_UNAVAILABLE_CODE
    # ONE LINE, in the citizen's words — no container, no sandbox, no orchestrator.
    message = resp.json()["error"]["message"]
    assert "wasn't sent" in message
    for jargon in ("sandbox", "container", "orchestrator", "workspace service"):
        assert jargon not in message.lower()
    # Nothing claimed, nothing written, no partial reply.
    assert _fresh_engine.peek(conv.id) is None
    rows = await db_session.scalar(
        sa.select(sa.func.count()).select_from(Message).where(Message.conversation_id == conv.id)
    )
    assert (rows or 0) == 0


def test_nothing_builds_a_saved_copy_workspace_for_a_turn() -> None:
    """An inertness guard over the retired degrade arm.

    `ExtractedSnapshotWorkspace` still EXISTS — the classification review and the deploy
    pipeline both read a saved bundle, legitimately — so the guard is not "the class is gone".
    It is that the turn engine no longer constructs one: the read surface a turn is given comes
    from one arm, and there is no second path for a chat to answer from a copy."""
    from src.services.turns import engine as engine_module

    # Asserted on the module's NAMESPACE rather than on its source text: the engine cannot
    # construct a snapshot workspace it never imported, and a name check survives a refactor
    # that a text search would not.
    for retired in (
        "ExtractedSnapshotWorkspace",
        "EmptyProjectWorkspace",
        "extract_snapshot",
        "NoAppYet",
    ):
        assert not hasattr(engine_module, retired), retired


# --- R42a: the stored-message ceiling refuses rather than trims ----------------------------


async def test_an_over_length_message_is_refused_at_the_boundary(
    client, db_session, set_chat_model, _fresh_engine
) -> None:
    """★ AE18. Refused by the schema, before anything is claimed or stored — and REFUSED, not
    trimmed. A message cut at a ceiling is one the citizen believes they sent whole, and the
    platform has no way to tell them otherwise afterwards."""
    import sqlalchemy as sa

    from src.api.v1.conversations._shared import MAX_MESSAGE_TEXT_CHARS
    from src.db.models.message import Message

    user, conv = await _auth_with_conversation(db_session)
    set_chat_model(_streaming_text("never reached"))

    resp = await _post_turn(client, _headers(user), conv, text="x" * (MAX_MESSAGE_TEXT_CHARS + 1))

    assert resp.status_code == 422, resp.text
    rows = await db_session.scalar(
        sa.select(sa.func.count()).select_from(Message).where(Message.conversation_id == conv.id)
    )
    assert (rows or 0) == 0


async def test_a_message_one_character_under_the_ceiling_is_stored_whole(
    client, db_session, set_chat_model, _fresh_engine
) -> None:
    """The other side of the same rule, and the half that catches a trim: a ceiling enforced by
    truncation passes the refusal test above and fails this one."""
    from src.api.v1.conversations._shared import MAX_MESSAGE_TEXT_CHARS
    from src.services.messages.projection import UserTextItem, project_rows
    from src.services.messages.store import load_rows

    user, conv = await _auth_with_conversation(db_session)
    set_chat_model(_streaming_text("answered"))
    text = "y" * (MAX_MESSAGE_TEXT_CHARS - 1)

    assert (await _post_turn(client, _headers(user), conv, text=text)).status_code == 202
    await _settle(_fresh_engine, conv.id)

    items = project_rows(
        list(await load_rows(db_session, user_id=user.id, conversation_id=conv.id))
    )
    typed = [item for item in items if isinstance(item, UserTextItem)]
    assert [len(item.text) for item in typed] == [MAX_MESSAGE_TEXT_CHARS - 1]


async def test_over_daily_limit_keeps_the_dedicated_429_body(
    client, db_session, set_chat_model, _fresh_engine
) -> None:
    """The 429 contract the SPA's interceptor reads (limit/used/remaining) must survive this
    route too — flattening it into the plain envelope drops three of the five keys."""
    from src.db.models.user_limit import UserLimit
    from src.services.usage.gate import record_usage

    user, conv = await _auth_with_conversation(db_session)
    set_chat_model(_streaming_text("should not stream"))
    db_session.add(UserLimit(user_id=user.id, daily_token_limit=10))
    await db_session.flush()
    await record_usage(db_session, user.id, input_tokens=10, output_tokens=0)

    resp = await _post_turn(client, _headers(user), conv)
    assert resp.status_code == 429
    body = resp.json()
    assert set(body["error"]) == {"message", "code", "limit", "used", "remaining"}
    assert body["error"]["code"] == "daily_token_limit_exceeded"
    assert body["error"]["limit"] == 10
    assert body["error"]["used"] == 10
    assert body["error"]["remaining"] == 0


# --- a refused start must not burn the pending card ---------------------------------------


async def _pending_card_state(db_session, user_id, conversation_id) -> str | None:
    from src.services.turns.plan_options import find_pending

    card = await find_pending(db_session, user_id=user_id, conversation_id=conversation_id)
    return None if card is None else "pending"


async def test_a_refused_start_leaves_the_pending_plan_card_unresolved(
    client, db_session, set_chat_model, _fresh_engine, building
) -> None:
    """`resolve_pending_as_refine` is a WRITE. Every rejection that can be decided without it
    must come FIRST — otherwise a 400/409 the user never asked for silently consumes their
    Build-it card and the button goes dead."""
    from src.db.models.conversation import ChatKind, Conversation
    from src.services.turns.guard import claim_conversation, release_conversation

    user, conv = await _auth_with_conversation(db_session, kind=ChatKind.PLAN)
    set_chat_model(_plan_call_model())
    headers = _headers(user)
    assert (await _post_turn(client, headers, conv, text="plan it")).status_code == 202
    await _settle(_fresh_engine, conv.id)
    assert await _pending_card_state(db_session, user.id, conv.id) == "pending"

    # (a) a reply already in flight → 409, card untouched.
    claim_conversation(conv.id)
    try:
        busy = await _post_turn(client, headers, conv, text="hurry up")
        assert busy.status_code == 409
    finally:
        release_conversation(conv.id)
    assert await _pending_card_state(db_session, user.id, conv.id) == "pending"

    # (b) a build is live in this thread → 409, card still untouched. This gate sits ahead
    # of every other check, so it is the one most able to burn a card by accident.
    with building(conv.id, user.id):
        gated = await _post_turn(client, headers, conv, text="hurry up")
        assert gated.status_code == 409
    assert await _pending_card_state(db_session, user.id, conv.id) == "pending"

    # (c) the user's own sandbox is committed to ANOTHER thread → 409, card still untouched.
    # This replaces the old Write-mode 400: the refusal that remains is about the workspace
    # being busy elsewhere, never about the kind itself. `kind` is fixed at creation in real
    # traffic (R14/R15, no route mutates it) — this direct row mutation is a TEST-ONLY shortcut
    # to exercise the guard against a Build-kind row without driving a real transition.
    conversation = await db_session.get(Conversation, conv.id)
    assert conversation is not None
    conversation.kind = ChatKind.BUILD
    await db_session.flush()
    with building(uuid.uuid4(), user.id):  # live, but on ANOTHER thread
        refused = await _post_turn(client, headers, conv, text="hurry up")
        assert refused.status_code == 409
    assert await _pending_card_state(db_session, user.id, conv.id) == "pending"


# --- resume from a cursor (tail-only, no snapshot) ----------------------------------------


async def test_reconnect_with_cursor_resumes_tail_only_without_duplicating_text(
    client, db_session, set_chat_model, _fresh_engine
) -> None:
    """The `?turn=&cursor=` branch: a subscriber that can prove gap-free continuity gets a
    PLAIN replay — no consolidating snapshot, and therefore no re-delivered prefix."""
    gate = asyncio.Event()

    async def _paced(messages: list[ModelMessage], info: AgentInfo):
        yield "alpha "
        await gate.wait()
        yield "omega"

    user, conv = await _auth_with_conversation(db_session)
    set_chat_model(FunctionModel(stream_function=_paced))
    headers = _headers(user)
    turn_id = (await _post_turn(client, headers, conv)).json()["turnId"]

    state = _fresh_engine.peek(conv.id)
    assert state is not None
    while not (state.text_parts or state.pending_text):
        await asyncio.sleep(0.01)
    cursor = state.seq  # everything up to here is already in hand

    reader = asyncio.create_task(
        client.get(
            f"/v1/conversations/{conv.id}/events?turn={turn_id}&cursor={cursor}", headers=headers
        )
    )
    while not state.subscribers:
        await asyncio.sleep(0.01)
    gate.set()
    events = await asyncio.wait_for(reader, timeout=10)

    frames = _frames_of(events.text)
    assert frames, "the resume delivered nothing"
    assert frames[0].type != "snapshot"  # tail-only: continuity was provable
    # NOTHING AT OR BEFORE THE CURSOR IS RE-SENT — the property this test is actually about,
    # asserted on the sequence numbers rather than on the words. It used to be asserted by
    # showing that "alpha" was absent, which only worked while the two deltas reached the wire
    # separately: prose is held now (U4), so the whole answer arrives as one block AFTER the
    # cursor. That block is legitimately in the tail, and "alpha not in replayed" would now be
    # asserting that the resume lost half the answer.
    assert all(frame.seq > cursor for frame in frames)
    replayed = "".join(f.text for f in frames if f.type == "text_delta")
    assert replayed == "alpha omega"
    assert frames[-1].type == "turn_ended"

    # SETTLE THE TASK, not just the stream. `turn_ended` is delivered from inside the run, and
    # the run's `finally` still has work after it — it drains the preview watcher and writes the
    # durable turn-terminal row, which opens a session of its own. A test that returns on the
    # last frame leaves that session unclosed and the connection is torn down by the garbage
    # collector against an event loop pytest has already closed, which surfaces as an error at
    # teardown rather than as a failure here.
    await _settle(_fresh_engine, conv.id)


async def test_active_turn_in_conversation_read_while_running(
    client, db_session, set_chat_model, _fresh_engine
) -> None:
    """U6's deferred populated-while-running case: the GET reports {turnId, lastSeq} while
    the turn runs and null after it settles."""
    gate = asyncio.Event()

    async def _stall(messages: list[ModelMessage], info: AgentInfo):
        yield "alive "
        await gate.wait()
        yield "still"

    user, conv = await _auth_with_conversation(db_session)
    set_chat_model(FunctionModel(stream_function=_stall))
    headers = _headers(user)
    turn_id = (await _post_turn(client, headers, conv)).json()["turnId"]
    state = _fresh_engine.peek(conv.id)
    assert state is not None
    while not (state.text_parts or state.pending_text):
        await asyncio.sleep(0.01)

    detail = (await client.get(f"/v1/conversations/{conv.id}", headers=headers)).json()
    assert detail["activeTurn"] is not None
    assert detail["activeTurn"]["turnId"] == turn_id
    assert detail["activeTurn"]["lastSeq"] >= 1

    gate.set()
    await _settle(_fresh_engine, conv.id)
    detail = (await client.get(f"/v1/conversations/{conv.id}", headers=headers)).json()
    assert detail["activeTurn"] is None


# --- wire discipline ----------------------------------------------------------------------


async def test_idle_conversation_subscribe_closes_cleanly(client, db_session) -> None:
    user, conv = await _auth_with_conversation(db_session)
    events = await client.get(f"/v1/conversations/{conv.id}/events", headers=_headers(user))
    frames = _frames_of(events.text)
    assert len(frames) == 1
    assert frames[0].type == "snapshot" and frames[0].turn_status == "idle"
    assert frames[0].turn_id is None
    assert events.text.endswith("data: [DONE]\n\n")


def test_frame_union_parses_with_callable_discriminator() -> None:
    # Malformed KNOWN tag → raises (never silently swallowed)…
    with pytest.raises(ValidationError):
        TURN_STREAM_FRAME_ADAPTER.validate_python({"type": "text_delta", "seq": 1})
    # …unknown tag → captured verbatim without degrading known members.
    unknown = TURN_STREAM_FRAME_ADAPTER.validate_python(
        {"type": "shiny_new_frame", "seq": 7, "payload": "whatever"}
    )
    assert isinstance(unknown, UnknownFrame)
    assert unknown.type == "shiny_new_frame" and unknown.seq == 7


def _union_member_tags() -> set[str]:
    """The tags actually reachable through the union, read off its `Tag` metadata."""
    union, *_discriminator = get_args(TurnStreamFrame)
    return {
        meta.tag
        for member in get_args(union)
        for meta in get_args(member)[1:]
        if isinstance(meta, Tag)
    }


def test_every_known_frame_tag_is_also_a_union_member() -> None:
    """The both-places trap: a frame type registered in `_KNOWN_FRAME_TAGS` but missing from
    the union still PARSES — as `UnknownFrame` — so the forward-compat escape hatch swallows
    our own frame and the client silently sees nothing. Set equality catches it in either
    direction (an unreachable member is just as wrong as a tagless one)."""
    assert _union_member_tags() == set(_KNOWN_FRAME_TAGS) | {"unknown"}


def test_build_frames_round_trip_without_degrading_to_unknown() -> None:
    frames: list[Any] = [
        WorkspaceFrame(seq=1, state="preparing", message="Warming up your workspace…"),
        PreviewFrame(seq=2, state="ready", preview_url="https://preview.example/app"),
        DiagnosticFrame(seq=3, source=ErrorSource.TSC),
        QuotaFrame(seq=4, limit=1_000_000, used=1_000_042, resets_at="2026-07-30T00:00:00Z"),
    ]
    for frame in frames:
        parsed = TURN_STREAM_FRAME_ADAPTER.validate_python(
            json.loads(frame.model_dump_json(by_alias=True))
        )
        assert not isinstance(parsed, UnknownFrame), f"{frame.type} degraded to UnknownFrame"
        assert parsed == frame


def test_build_frames_speak_camel_case_on_the_wire() -> None:
    """The transport ships `model_dump_json(by_alias=True)`, and the portal narrows on the
    camelCase key — a snake_case leak here is a field the client never reads."""
    preview = PreviewFrame(seq=2, state="ready", preview_url="https://preview.example/app")
    assert json.loads(preview.model_dump_json(by_alias=True)) == {
        "type": "preview",
        "seq": 2,
        "state": "ready",
        "previewUrl": "https://preview.example/app",
    }
    diagnostic = DiagnosticFrame(seq=3, source=ErrorSource.SERVER)
    assert json.loads(diagnostic.model_dump_json(by_alias=True)) == {
        "type": "diagnostic",
        "seq": 3,
        "source": "server",
        # NO `title`, NO `cleanedStack` — U14 took the model's half off this frame entirely.
        # Asserted as an exact dict rather than by absence checks, which is what makes this the
        # egress test: a field re-added anywhere in the shape fails here, whatever it is named.
        # U16 — the citizen-facing half, derived from the error class because the producer
        # supplied none. It is asserted HERE, on the exact wire dict, for the reason this test
        # exists at all: the portal narrows on the camelCase key, so a snake_case spelling of
        # either field is a sentence the citizen never reads and a blank error row.
        "userMessage": "Your app ran into a problem while it was starting up.",
        "userAction": (
            "Nothing to do right now — we're working on it. "
            "If it keeps happening, try asking for something simpler."
        ),
    }
    quota = QuotaFrame(seq=4, limit=10, used=11, resets_at="2026-07-30T00:00:00Z")
    assert json.loads(quota.model_dump_json(by_alias=True)) == {
        "type": "quota",
        "seq": 4,
        "limit": 10,
        "used": 11,
        "resetsAt": "2026-07-30T00:00:00Z",
    }


def test_extended_frames_stay_parseable_without_the_new_fields() -> None:
    """Additive only: the portal narrows field by field, so a frame minted before these
    fields existed — and an older parser meeting a new one — must both keep working."""
    ended = TURN_STREAM_FRAME_ADAPTER.validate_python(
        {"type": "turn_ended", "seq": 9, "turnId": "t-1", "status": "completed"}
    )
    assert isinstance(ended, TurnEndedFrame)
    assert (ended.reason, ended.preview_url, ended.snapshot_committed) == (None, None, None)

    snapshot = TURN_STREAM_FRAME_ADAPTER.validate_python(
        {"type": "snapshot", "seq": 0, "turnId": None, "turnStatus": "idle"}
    )
    assert isinstance(snapshot, SnapshotFrame)
    assert (snapshot.workspace_state, snapshot.preview_url, snapshot.preview_state) == (
        None,
        None,
        None,
    )

    rich = TURN_STREAM_FRAME_ADAPTER.validate_python(
        {
            "type": "snapshot",
            "seq": 4,
            "turnId": "t-1",
            "turnStatus": "running",
            "workspaceState": "ready",
            "previewUrl": "https://preview.example/app",
            "previewState": "reconnecting",
        }
    )
    assert isinstance(rich, SnapshotFrame)
    assert rich.workspace_state == "ready"
    assert rich.preview_url == "https://preview.example/app"
    assert rich.preview_state == "reconnecting"


def test_snapshot_committed_keeps_unknown_distinct_from_not_saved() -> None:
    """Tri-state on purpose: null means UNKNOWN (a non-Write turn, or a terminal that never
    reached the finalize), false means the finalize ran and did not save. Collapsing the two
    tells a citizen their work is gone when it may well be on disk."""
    unknown = TurnEndedFrame(seq=1, turn_id="t-1", status="completed")
    not_saved = TurnEndedFrame(
        seq=1, turn_id="t-1", status="failed", reason="sandbox_gone", snapshot_committed=False
    )
    assert unknown.snapshot_committed is None
    assert not_saved.snapshot_committed is False
    assert json.loads(unknown.model_dump_json(by_alias=True))["snapshotCommitted"] is None
    assert json.loads(not_saved.model_dump_json(by_alias=True))["snapshotCommitted"] is False


def test_build_snapshot_carries_the_workspace_and_preview_facts(_fresh_engine) -> None:
    """A `preview` frame that fired before the client connected is gone from the ring by the
    time a mid-Write reconnect asks. The catch-up snapshot is the only thing left that can
    answer, so it carries the trio and the reattach needs no second REST call."""
    from src.db.models.conversation import ChatKind

    state = _TurnState(
        turn_id=uuid.uuid7(),
        conversation_id=uuid.uuid7(),
        user_id=uuid.uuid7(),
        kind=ChatKind.BUILD,
    )
    # A chat turn never touches a workspace, so the trio starts as "nothing to say".
    blank = _fresh_engine.build_snapshot(state)
    assert (blank.workspace_state, blank.preview_url, blank.preview_state) == (None, None, None)

    state.workspace_state = "ready"
    state.preview_url = "https://preview.example/app"
    state.preview_state = "reconnecting"
    snapshot = _fresh_engine.build_snapshot(state)
    assert snapshot.workspace_state == "ready"
    assert snapshot.preview_url == "https://preview.example/app"
    assert snapshot.preview_state == "reconnecting"


def test_keepalive_budget_stays_pinned_under_the_client_stall_window() -> None:
    """The cross-repo timeout inequality (streamed-reply learning): the server keepalive
    must sit WELL under the portal reader's stall window. The portal side pins its 60s
    constant in `turnStreamApi.test.ts` — 4x margin, re-derived on both sides."""
    assert KEEPALIVE_SECONDS == 15.0
    assert KEEPALIVE_SECONDS * 4 <= 60.0


async def test_csrf_required_on_turn_posts(client, db_session, set_chat_model) -> None:
    user, conv = await _auth_with_conversation(db_session)
    set_chat_model(_streaming_text("x"))
    resp = await client.post(
        f"/v1/conversations/{conv.id}/turns",
        headers=_headers(user, with_csrf=False),
        json={"message": {"text": "hi", "attachmentTexts": [], "attachmentIds": []}},
    )
    assert resp.status_code == 403


# --- plan options over the API (U11) ------------------------------------------------------


_OFFERED_PLAN = "Your visitor log will list today's visitors, newest first."


def _plan_call_model():
    minted = {"n": 0}

    async def _stream(messages: list[ModelMessage], info: AgentInfo):
        yield "Plan:\n1. Table\n2. Form\n"
        from pydantic_ai.models.function import DeltaToolCall, DeltaToolCalls

        minted["n"] += 1
        yield DeltaToolCalls(
            {
                0: DeltaToolCall(
                    name="present_plan_options",
                    # THE PLAN RIDES THE ARGUMENT. An offer with nothing in it is refused
                    # and never recorded, so `"{}"` here would leave every assertion below
                    # looking for a card that does not exist.
                    json_args=json.dumps({"plan": _OFFERED_PLAN}),
                    tool_call_id="opt-api" if minted["n"] == 1 else f"opt-api-{minted['n']}",
                )
            }
        )

    return FunctionModel(stream_function=_stream)


async def test_refine_click_resolves_over_the_api(
    client, db_session, set_chat_model, _fresh_engine
) -> None:
    from src.db.models.conversation import ChatKind

    user, conv = await _auth_with_conversation(db_session, kind=ChatKind.PLAN)
    set_chat_model(_plan_call_model())
    headers = _headers(user)
    assert (await _post_turn(client, headers, conv, text="plan it")).status_code == 202
    await _settle(_fresh_engine, conv.id)

    resolve_url = f"/v1/conversations/{conv.id}/plan-options/opt-api/resolve"
    first = await client.post(resolve_url, headers=headers, json={"choice": "refine"})
    assert first.status_code == 200
    assert first.json() == {"state": "refine", "alreadyResolved": False}
    second = await client.post(resolve_url, headers=headers, json={"choice": "refine"})
    assert second.json() == {"state": "refine", "alreadyResolved": True}

    unknown = await client.post(
        f"/v1/conversations/{conv.id}/plan-options/nope/resolve",
        headers=headers,
        json={"choice": "refine"},
    )
    assert unknown.status_code == 400

    other = await UserFactory.create(db_session, email="other-po@rvaiglobal.com")
    foreign = await client.post(resolve_url, headers=_headers(other), json={"choice": "refine"})
    assert foreign.status_code == 404


async def test_free_text_while_pending_resolves_as_implicit_refine(
    client, db_session, set_chat_model, _fresh_engine
) -> None:
    from src.db.models.conversation import ChatKind
    from src.services.turns.plan_options import find_pending

    user, conv = await _auth_with_conversation(db_session, kind=ChatKind.PLAN)
    set_chat_model(_plan_call_model())
    headers = _headers(user)
    assert (await _post_turn(client, headers, conv, text="plan it")).status_code == 202
    await _settle(_fresh_engine, conv.id)
    assert await find_pending(db_session, user_id=user.id, conversation_id=conv.id) is not None

    # The user types instead of clicking — the pending card resolves as refine and the
    # new turn proceeds (a fresh card presents at its end).
    assert (
        await _post_turn(client, headers, conv, text="actually add exports")
    ).status_code == 202
    await _settle(_fresh_engine, conv.id)
    events = await client.get(f"/v1/conversations/{conv.id}/events", headers=headers)
    frames = _frames_of(events.text)
    cards = [f for f in frames[0].items if getattr(f, "type", "") == "plan_options"]
    # The superseded card projects as refine; the new turn's card is the pending one.
    states = [c.state for c in cards]
    assert "refine" in states and "pending" in states


@pytest.mark.parametrize("kind", [ChatKind.PLAN, ChatKind.BUILD])
async def test_a_turn_in_any_kind_refuses_to_reclaim_another_projects_unsaved_work(
    client, db_session, set_chat_model, fake_redis, fake_storage, app, kind
) -> None:
    """#83 — the refusal must not be gated on Build.

    `_pin_workspace` attaches the project's LIVE container for every kind ("Resolve the
    turn-pinned read surface ONCE, for EVERY mode"), so a Plan turn takes the one-per-user
    workspace exactly as a Build turn does. Gating the preflight on Build meant Plan still
    destroyed the incumbent's unsaved work — and did it inside the detached turn, where the
    only thing the user saw was "Your workspace could not be started right now": no dialog,
    no named project, and no way to save. Found in live testing.

    PARAMETRISED OVER BOTH KINDS, not the three modes ("ask", "plan", "write") this test used
    to run against: Ask and Plan collapsed into the one `ChatKind.PLAN` (`db/models/
    conversation.py`), so a third arm would just repeat the Plan case under a retired name.
    Two arms still catch the original bug — this went wrong because someone (me) read "the
    workspace" as "the Build workspace", and a single-kind test would let that back in."""
    from src.api.v1.build_sessions.deps import sandbox_or_none_dependency
    from src.services.build_sessions.manager import SandboxReclaimBlockedError, SessionManager
    from tests.fakes import FakeSandboxClient

    set_chat_model(_streaming_text("ok"))
    user, conv = await _auth_with_conversation(db_session, kind=kind)

    # Another project of this user's holds the workspace, and it has unsaved work.
    async def _blocked(*a, **k):
        raise SandboxReclaimBlockedError(
            project_id=uuid.uuid4(), project_name="Visitor Log", app_id=uuid.uuid4(), dirty=True
        )

    monkey = SessionManager.reclaim_preflight
    SessionManager.reclaim_preflight = _blocked  # type: ignore[method-assign]
    app.dependency_overrides[sandbox_or_none_dependency] = lambda: FakeSandboxClient()
    try:
        resp = await _post_turn(client, _headers(user), conv)
    finally:
        SessionManager.reclaim_preflight = monkey  # type: ignore[method-assign]
        app.dependency_overrides.pop(sandbox_or_none_dependency, None)

    assert resp.status_code == 409
    error = resp.json()["error"]
    assert error["code"] == "sandbox_reclaim_blocked"  # NOT the generic "try again shortly"
    assert error["projectName"] == "Visitor Log"  # names what is in the way


async def test_a_redis_outage_during_the_preflight_is_503_never_a_silent_reclaim(
    client, db_session, set_chat_model, fake_storage, app, monkeypatch
) -> None:
    """#83 REVIEW, FINDING 5. The guard used to wrap its registry read in a bare
    `except Exception: return`, and every `return` in that function PERMITS the teardown — so
    a Redis blip was read as "no registry, nothing to lose" and the incumbent's container was
    destroyed. `locks.py` names that exact anti-pattern: swallowing in an answer-bearing
    primitive "manufactures a certain-looking answer out of an ambiguous store".

    The swallow is gone, so the error now propagates — and `turns.py` wraps the preflight in
    `build_coordination_or_503` so it lands as the same 503 every other coordination route
    gives, rather than an undocumented 500. An unreadable store is not an empty one."""
    from redis.exceptions import ConnectionError as RedisConnectionError

    from src.api.v1.build_sessions.deps import sandbox_or_none_dependency
    from src.services.redis import client as redis_client
    from tests.fakes import FakeSandboxClient

    class _DeadRedis:
        """Every command raises — the shape a real outage takes once the bounded retry is
        spent. `__getattr__` rather than a method list, so a new command cannot silently
        escape the outage. (Mirrors the `dead_redis` fixture in the build_sessions conftest,
        inlined because that one is not in scope here.)"""

        def __getattr__(self, name: str):
            async def the_store_is_gone(*a: object, **k: object) -> object:
                raise RedisConnectionError(f"connection refused ({name})")

            return the_store_is_gone

    set_chat_model(_streaming_text("ok"))
    user, conv = await _auth_with_conversation(db_session)
    monkeypatch.setattr(redis_client, "_redis_singleton", _DeadRedis())
    app.dependency_overrides[sandbox_or_none_dependency] = lambda: FakeSandboxClient()
    try:
        resp = await _post_turn(client, _headers(user), conv)
    finally:
        app.dependency_overrides.pop(sandbox_or_none_dependency, None)

    assert resp.status_code == 503
    assert resp.status_code != 500  # the shape before the seam was added
    assert "try again" in resp.json()["error"]["message"].lower()
