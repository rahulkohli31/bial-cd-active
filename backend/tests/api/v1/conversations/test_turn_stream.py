"""The turn transport, end to end: POST starts a detached turn, GET subscribes with
catch-up-snapshot-then-tail, stop is the explicit cancel, and the frame union parses with
the callable-discriminator discipline (malformed KNOWN tag raises; unknown tag captured).

Also home to the HTTP-level no-overrides kind-gating proof deferred here: a Plan-kind
turn's model-visible tool list carries no write tools, through the REAL route + engine.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from types import SimpleNamespace
from typing import Any, get_args

import pytest
import sqlalchemy as sa
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
from src.db.models.conversation import ChatKind, Conversation
from src.services.turns import engine as engine_module
from src.services.turns.engine import (
    _TURN_FAILED_MESSAGE,
    _TurnState,
)
from tests.api.v1.conversations.conftest import _headers
from tests.factories import ConversationFactory, ProjectFactory, UserFactory
from tests.transcript import rendered_text

# Explicit rather than autouse: conftest.py's turn-driving fixtures are shared by four
# files, but the other files in this directory drive no turns.
pytestmark = pytest.mark.usefixtures("_fresh_engine", "_override_billing")


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
    assert frames[0].type == "snapshot"
    assert frames[0].turn_id == turn_id
    assert frames[0].turn_status == "completed"
    # PROSE ONLY: the acknowledgement is retracted by the first real words (`_push_text`), so
    # a settled prose-only turn carries no step part at all — see
    # `test_a_turn_that_only_writes_prose_retracts_the_acknowledgement_too`. The two deltas
    # merge into one text part because `new_block` only fires on a genuinely new block.
    assert [part.type for part in frames[0].parts] == ["text"]
    assert not any(
        part.type == "step" and part.tool_call_id == engine_module.ACK_TOOL_CALL_ID
        for part in frames[0].parts
    )
    assert [part.text for part in frames[0].parts if part.type == "text"] == ["hello world"]
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
    while not state.text_blocks():  # the first block is out — we are genuinely mid-turn
        await asyncio.sleep(0.01)

    # httpx's ASGITransport buffers a streaming response until the app completes, so the GET
    # rides a concurrent task: it SUBSCRIBES mid-turn (proven below by the snapshot's `running`
    # status, built at request time), then the gate releases and the result carries both halves.
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
    # The snapshot carries the prose already written, at the position it was written. Nothing
    # is held any more — a block is on the wire and in the turn's ordered parts the moment it
    # is written — so a reattaching reader picks the answer up where it really is, acknowledgement
    # already retracted.
    assert [part.type for part in frames[0].parts] == ["text"]
    snapshot_text = [part.text for part in frames[0].parts if part.type == "text"]
    assert snapshot_text == ["first "]
    # …and the tail continues that block rather than opening a second one: `new_block` is the
    # only thing that tells a client where one paragraph ends and the next begins, so a `True`
    # here would break one sentence in half on the reconnecting reader's screen.
    tail = [f for f in frames if f.type == "text_delta"]
    assert [f.new_block for f in tail] == [False]
    deltas = "".join(f.text for f in tail)
    assert deltas == "second"
    assert "".join(snapshot_text) + deltas == "first second"
    assert frames[-1].type == "turn_ended" and frames[-1].status == "completed"
    assert events.text.endswith("data: [DONE]\n\n")

    # SETTLE THE TASK, not just the stream. `turn_ended` is delivered from inside the run, and
    # the run's `finally` still has work after it — it drains the preview watcher and writes the
    # durable turn-terminal row, which opens a session of its own. A test that returns on the
    # last frame leaves that session unclosed and the connection is torn down by the garbage
    # collector against an event loop pytest has already closed, which surfaces as an error at
    # teardown rather than as a failure here.
    await _settle(_fresh_engine, conv.id)


async def test_a_turn_that_writes_acts_and_writes_again_reads_the_same_live_and_caught_up(
    client, db_session, set_chat_model, _fresh_engine
) -> None:
    """★ THE HEADLINE PROPERTY, END TO END, ON THE WIRE.

    Prose beside a tool call used to be deleted once the call arrived, so a turn could only
    end in one text block, always last. This turn writes, reads a file, and writes again, so
    the live tail and the reloaded snapshot disagree if either half of that fix is missing —
    compared as LISTS, not joined strings, since where the step sits is the whole point."""
    from pydantic_ai.models.function import DeltaToolCall, DeltaToolCalls

    gate = asyncio.Event()
    at_the_door = asyncio.Event()

    async def _writes_acts_writes(messages: list[ModelMessage], info: AgentInfo):
        if len(messages) == 1:
            # HELD AT THE DOOR so the subscription below is open for the whole turn: its
            # snapshot must carry nothing but the acknowledgement, or the tail is not the whole
            # record and the comparison at the end proves less than it claims to.
            at_the_door.set()
            await gate.wait()
            yield "Let me see what is on the page. "
            yield DeltaToolCalls(
                {
                    0: DeltaToolCall(
                        name="read_file",
                        json_args=json.dumps({"path": "app/page.tsx"}),
                        tool_call_id="look-1",
                    )
                }
            )
        else:
            yield "That is the starter template, untouched."

    user, conv = await _auth_with_conversation(db_session)
    set_chat_model(FunctionModel(stream_function=_writes_acts_writes))
    headers = _headers(user)
    assert (await _post_turn(client, headers, conv)).status_code == 202

    state = _fresh_engine.peek(conv.id)
    assert state is not None
    # Wait until the model is actually at the door before subscribing. The app and fixtures
    # share ONE session; a GET issued before the turn's user-message write finishes would wait
    # on that same session while the turn waits on a gate this test opens only once the GET has
    # subscribed — a circular wait that hangs the suite rather than failing it.
    await asyncio.wait_for(at_the_door.wait(), timeout=10)
    # ASGITransport buffers until the app completes (see the test above), so the GET again
    # rides a concurrent task that subscribes while the model is still at the gate.
    reader = asyncio.create_task(
        client.get(f"/v1/conversations/{conv.id}/events", headers=headers)
    )
    while not state.subscribers:  # the route registered its queue — subscription is live
        await asyncio.sleep(0.01)
    gate.set()
    events = await asyncio.wait_for(reader, timeout=10)
    await _settle(_fresh_engine, conv.id)

    frames = _frames_of(events.text)
    assert frames[0].type == "snapshot" and frames[0].turn_status == "running"
    # Nothing but the acknowledgement had happened when the subscription opened, so everything
    # this turn produced is in the tail rather than split across the snapshot and the tail.
    assert [part.type for part in frames[0].parts] == ["step"]
    assert frames[0].parts[0].tool_call_id == engine_module.ACK_TOOL_CALL_ID

    live: list[tuple[str, str]] = []
    for frame in frames[1:]:
        if frame.type == "text_delta":
            if frame.new_block or not live or live[-1][0] != "text":
                live.append(("text", frame.text))
            else:
                live[-1] = ("text", live[-1][1] + frame.text)
        elif frame.type == "step" and frame.phase == "started":
            live.append(("step", frame.tool_call_id))
    # Liveness: the stream really carried this turn to its end, so the sequence compared below
    # is a whole turn and not a truncated read that happens to match another truncated read.
    assert frames[-1].type == "turn_ended" and frames[-1].status == "completed"

    # THE CITIZEN WHO RELOADED: the same turn, rebuilt from its catch-up snapshot alone.
    replay = await client.get(f"/v1/conversations/{conv.id}/events", headers=headers)
    snapshot = _frames_of(replay.text)[0]
    assert snapshot.type == "snapshot" and snapshot.turn_status == "completed"
    caught_up = [
        ("text", part.text) if part.type == "text" else ("step", part.tool_call_id)
        for part in snapshot.parts
    ]

    # Steps are compared by `tool_call_id` — the key both shapes carry and the one the browser
    # replaces a pending card on — rather than by label, which is the renderer's business and
    # would make this a test of the label translator instead of a test of the order.
    assert live == caught_up
    assert [kind for kind, _ in live] == ["text", "step", "text"]
    assert [body for kind, body in live if kind == "text"] == [
        "Let me see what is on the page. ",
        "That is the starter template, untouched.",
    ]


async def test_the_acknowledgement_actually_reaches_a_subscriber(
    client, db_session, set_chat_model, _fresh_engine
) -> None:
    """★ THE DELIVERY TEST, and the one whose absence hid the bug.

    The ack is `seq == 1`, emitted synchronously inside `start_turn` before the detached task
    exists. Every client POSTs then only afterward opens the stream, so by the time it
    subscribes the route's snapshot already sets `last_sent = snapshot.seq` past 1 — the ack
    was behind the cursor before anyone could see it, in neither the snapshot nor the tail,
    and three prior tests missed it entirely by asserting against the in-memory ring instead
    of the wire actually read. Assert against the wire here: that distinction is the defect."""
    gate = asyncio.Event()
    at_the_door = asyncio.Event()

    async def _paced(messages: list[ModelMessage], info: AgentInfo):
        at_the_door.set()
        await gate.wait()
        yield "done"

    user, conv = await _auth_with_conversation(db_session)
    set_chat_model(FunctionModel(stream_function=_paced))
    resp = await _post_turn(client, _headers(user), conv)
    assert resp.status_code == 202

    state = _fresh_engine.peek(conv.id)
    assert state is not None

    # The turn task must be AT THE DOOR before the subscription opens — see the test above.
    await asyncio.wait_for(at_the_door.wait(), timeout=10)
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

    # Settle the task, not just the stream — see test_mid_turn_subscribe_gets_snapshot_then_tail
    # for why the run's post-`turn_ended` `finally` work needs the session still open.
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
    # Wait for the turn to reach the model before posting again (the two tests below do the
    # same): the app and fixtures share ONE session, so a second request issued mid-flush lands
    # on a session in `prepared` state and fails on plumbing, not the 409 this test is about.
    state = _fresh_engine.peek(conv.id)
    assert state is not None
    while not state.text_blocks():
        await asyncio.sleep(0.01)
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
    while not state.text_blocks():
        await asyncio.sleep(0.01)

    stop = await client.post(f"/v1/conversations/{conv.id}/turns/{turn_id}/stop", headers=headers)
    assert stop.status_code == 200 and stop.json()["status"] == "stopping"
    await _settle(_fresh_engine, conv.id)
    assert state.status == "stopped"
    # The half-sentence they already read is still theirs: a stop that swept `partial ` back
    # out of the turn's parts would leave the record disagreeing with what they actually saw.
    assert state.text_blocks() == ["partial "]

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

    # ★ WHAT THE CITIZEN READS ON A FAILED TURN IS OURS, not the model's. Asserting the
    # composed prompt contains the audience instruction only proves the instruction was
    # present, which nobody doubted — it says nothing about whether the model obeyed it. Here
    # the model's own words are gone entirely, so what reaches the screen is a platform
    # constant, not a claim about the model's behavior.
    assert state.error_message == _TURN_FAILED_MESSAGE
    error_frames = [f for f in state.ring if f.type == "error"]
    assert [f.message for f in error_frames] == [_TURN_FAILED_MESSAGE]
    # The model's half-sentence is kept, not withdrawn: `before ` was already on the citizen's
    # screen when the run died, so the record keeps it and delivers the platform's message
    # separately on the error frame, never mixed into the model's own prose.
    assert state.text_blocks() == ["before "]
    assert "before" not in (state.error_message or "")
    assert "upstream fell over" not in rendered_text(state)  # nor the raw exception text


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
    """The HTTP-level no-overrides gating proof: through the REAL route, engine, and
    toolsets, a Plan turn's model-visible tool list is exactly the read surface plus the
    plan-confirmation tool — no write_file / edit_file / insert_lines / declare_done.

    `present_plan_options` belongs in the expected set below: Ask and Plan collapsed into one
    `ChatKind.PLAN`, and `toolsets_for_kind` hands every Plan-kind run the confirmation tool
    alongside the read surface, so there is no read-only-without-the-card surface left to
    assert."""
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
        # tell_the_user / propose_first_slice are shared by BOTH kinds. Exact-set on purpose:
        # a tool meant for both arms that reached only one is drift a subset check would miss.
        "tell_the_user",
        "propose_first_slice",
    }


async def test_write_mode_accepts_a_send_like_every_other_mode(
    client, db_session, set_chat_model, _fresh_engine
) -> None:
    """THE FLIP. Write used to 400 here with copy telling the user to switch modes — which was
    a real refusal for a real reason (Write had no toolset and no composable prompt) and is a
    lie now. A citizen who just built something can keep talking to it in the mode they are
    already in."""
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

    assert (await _post_turn(client, _headers(user), conv)).status_code == 202
    await _settle(_fresh_engine, conv.id)


async def test_a_build_in_another_thread_now_refuses_this_one_by_name(
    client, db_session, set_chat_model, _fresh_engine, building
) -> None:
    """Two distinct questions, both gates: "is THIS chat's agent mid-reply?" is
    per-conversation; "is this user's one workspace already committed elsewhere?" is
    per-user. A planning turn reads the project's live workspace now, like every other turn,
    so a send here while another of this user's chats holds that workspace is a second claim
    on the one thing there is only one of — not incidental traffic. The refusal carries a
    machine code distinct from the other 409 on this route, which has a different cause and
    remedy."""
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
    """★ A refusal that costs the citizen their message is a worse bug than
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


# --- no workspace service means the message is refused, not degraded ------------------


@pytest.mark.parametrize("kind", [ChatKind.PLAN, ChatKind.BUILD])
async def test_no_workspace_service_refuses_the_send_identically_in_both_kinds(
    client, db_session, set_chat_model, _fresh_engine, no_workspace_service, kind
) -> None:
    """★ Both kinds, one answer, said at the moment of sending.

    This replaced silence: a turn with no sandbox configured used to answer from the last
    saved copy of the app, a degradation the citizen was never told about. Both kinds read
    only the live app now, so refusing before the message is spent is the honest thing.
    The code is asserted, not just the status — it is how a browser tells this refusal
    family apart from the route's other 409s."""
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
    assert _fresh_engine.peek(conv.id) is None
    rows = await db_session.scalar(
        sa.select(sa.func.count()).select_from(Message).where(Message.conversation_id == conv.id)
    )
    assert (rows or 0) == 0


# --- a switched-off app is said in words, at the moment of sending -------------------------


@pytest.mark.parametrize("kind", [ChatKind.PLAN, ChatKind.BUILD])
async def test_a_switched_off_app_refuses_the_send_with_the_reason(
    client, db_session, set_chat_model, _fresh_engine, kind
) -> None:
    """A MESSAGE, not the gate. Read the sentence, then read what it is not.

    The enforcement lives in `resolve_app_for_project` and holds with or without this route
    ever asking (`tests/services/build_sessions/test_appdata.py` pins it there). What this
    line buys is WORDS: that refusal is raised inside the detached turn, where the engine's
    attach arm catches it as an unexpected failure and tells the citizen "the workspace
    service is not available" — wrong, and retryable-sounding, for an app an administrator
    deliberately switched off. Said here, the citizen gets the true reason and spends no turn.

    Both kinds, because the kill switch has no opinion about which chat you are in.
    """
    import sqlalchemy as sa

    from src.db.models.app_registry import AppRegistry, AppStatus
    from src.db.models.message import Message
    from src.services.build_sessions.appdata import APP_SWITCHED_OFF_CODE
    from tests.factories import AppRegistryFactory

    user, conv = await _auth_with_conversation(db_session, kind=kind)
    await AppRegistryFactory.create(
        db_session,
        user_id=user.id,
        project_id=conv.project_id,
        status=AppStatus.DISABLED,
    )
    set_chat_model(_streaming_text("never reached"))

    resp = await _post_turn(client, _headers(user), conv, text="add a column")

    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == APP_SWITCHED_OFF_CODE
    message = resp.json()["error"]["message"]
    # It says WHAT THEY CANNOT DO, and it does not say "publish": a never-published draft can
    # be switched off too, and its owner learns nothing from a publishing sentence.
    assert "cannot make changes" in message
    assert "publish" not in message.lower()
    # Nothing claimed, nothing written, no partial reply — the refusal sits above the first
    # committing write, exactly where the workspace-unavailable one does.
    assert _fresh_engine.peek(conv.id) is None
    rows = await db_session.scalar(
        sa.select(sa.func.count()).select_from(Message).where(Message.conversation_id == conv.id)
    )
    assert (rows or 0) == 0
    # And the row is untouched — a refused send is not a lifecycle event.
    fresh = await db_session.scalar(
        sa.select(AppRegistry).where(AppRegistry.project_id == conv.project_id)
    )
    assert fresh is not None and fresh.status is AppStatus.DISABLED


def test_nothing_builds_a_saved_copy_workspace_for_a_turn() -> None:
    """An inertness guard over the retired degrade arm.

    `ExtractedSnapshotWorkspace` still EXISTS — the classification review and the deploy
    pipeline both read a saved bundle, legitimately — so the guard is not "the class is gone".
    It is that the turn engine no longer constructs one: the read surface a turn is given comes
    from one arm, and there is no second path for a chat to answer from a copy."""
    from src.services.turns import engine as engine_module

    # Asserted on the module NAMESPACE, not source text: a name check survives a refactor
    # that a text search would not, and the engine cannot construct what it never imported.
    for retired in (
        "ExtractedSnapshotWorkspace",
        "EmptyProjectWorkspace",
        "extract_snapshot",
        "NoAppYet",
    ):
        assert not hasattr(engine_module, retired), retired


# --- the stored-message ceiling refuses rather than trims ----------------------------


async def test_an_over_length_message_is_refused_at_the_boundary(
    client, db_session, set_chat_model, _fresh_engine
) -> None:
    """★ Refused by the schema, before anything is claimed or stored — and REFUSED, not
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
    # `kind` is fixed at creation in real traffic (no route mutates it) — this direct row
    # mutation is a TEST-ONLY shortcut to exercise the guard against a Build-kind row without
    # driving a real transition.
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
    while not state.text_blocks():
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
    # Nothing at or before the cursor is re-sent: `alpha ` reached the wire as its own frame
    # the moment it was written, so it is genuinely behind the cursor. Checked on BOTH the seq
    # numbers and the text — a resume that delivered nothing at all would still pass the seq
    # check alone, so the text is what proves the rest of the answer actually arrived.
    assert all(frame.seq > cursor for frame in frames)
    replayed = "".join(f.text for f in frames if f.type == "text_delta")
    assert replayed == "omega"
    assert frames[-1].type == "turn_ended"

    # Settle the task, not just the stream — see test_mid_turn_subscribe_gets_snapshot_then_tail
    # for why the run's post-`turn_ended` `finally` work needs the session still open.
    await _settle(_fresh_engine, conv.id)


async def test_active_turn_in_conversation_read_while_running(
    client, db_session, set_chat_model, _fresh_engine
) -> None:
    """The populated-while-running case: the GET reports {turnId, lastSeq} while
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
    while not state.text_blocks():
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
        # NO `title`, NO `cleanedStack` — the model's half of this frame is gone entirely.
        # Asserted as an exact dict rather than by absence checks: a field re-added anywhere
        # in the shape fails here, whatever it is named.
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


# --- plan options over the API ------------------------------------------------------


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
    """The refusal must not be gated on Build: `_pin_workspace` attaches the project's LIVE
    container for every kind, so a Plan turn takes the workspace exactly as a Build turn does,
    and gating the preflight on Build would leave Plan on the silent path. Parametrised over
    both kinds rather than the three retired modes (Ask/Plan collapsed into `ChatKind.PLAN`)
    because a single-kind test lets "the workspace" be misread as "the Build workspace"."""
    from src.api.v1.build_sessions.deps import sandbox_or_none_dependency
    from src.services.build_sessions.manager import SandboxReclaimBlockedError, SessionManager
    from tests.fakes import FakeSandboxClient

    set_chat_model(_streaming_text("ok"))
    user, conv = await _auth_with_conversation(db_session, kind=kind)

    # Another project of this user's holds the workspace, and it has unsaved work.
    async def _blocked(*a, **k):
        raise SandboxReclaimBlockedError(
            project_id=uuid.uuid4(),
            project_name="Visitor Log",
            app_id=uuid.uuid4(),
            dirty=True,
            agent_working=True,
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
    # What the browser's dialog reads: which project holds the workspace, and whether its
    # agent is mid-thought.
    assert error["agentWorking"] is True
    assert error["building"] is False  # the narrow flag is untouched and travels separately


async def test_a_redis_outage_during_the_preflight_is_503_never_a_silent_reclaim(
    client, db_session, set_chat_model, fake_storage, app, monkeypatch
) -> None:
    """The guard used to wrap its registry read in a bare `except Exception: return`, and
    every `return` in that function PERMITS the teardown — so a Redis blip was read as "no
    registry, nothing to lose" and the incumbent's container was destroyed. The swallow is
    gone: the error now propagates, and `turns.py` wraps the preflight in
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


# --- the workspace question comes before anything durable exists -------------------------
#
# The bug was an ORDERING: a first message committed its conversation row a round trip
# earlier, in `POST /conversations`, before anything asked about the workspace. A refused or
# declined first message left a real, titled, empty conversation behind, named after the text
# that was refused.
#
# Every scenario below asserts THE LIST, not the response — the response was always a correct
# 409; what was wrong was what it left behind, invisible to a status-code check.


async def _conversation_count(db_session, user_id) -> int:
    """How many conversations this user actually owns, read fresh from the database.

    A COUNT query rather than `expire_all()` plus an ORM read, deliberately: expiring the session
    makes every attribute of every loaded object a lazy load, and the next `project.id` in the
    caller then raises `MissingGreenlet` rather than answering. The query is already a round trip;
    it needs no help being fresh."""
    from sqlalchemy import func, select

    from src.db.models.conversation import Conversation

    total = await db_session.scalar(
        select(func.count()).select_from(Conversation).where(Conversation.user_id == user_id)
    )
    return int(total or 0)


async def _create_chat(client, headers, chat_id, project_id, *, kind="build"):
    """The round trip that now precedes a chat's first message.

    ★ NO TITLE, and that is the composer's real shape rather than a shortcut: `deriveTitle` reads
    the draft, and the draft is not known until the send, one round trip later. Stamping refused
    text into a row nobody can delete would be worse than leaving it unnamed, so the first message
    that actually lands titles the chat.
    """
    return await client.post(
        "/v1/conversations",
        headers=headers,
        json={"id": str(chat_id), "projectId": str(project_id), "kind": kind},
    )


async def _post_first_message(
    client, headers, chat_id, project_id, *, kind="build", text="a visitor log"
):
    """A chat's FIRST message, in the order the composer now sends it: create the row, then post
    the turn against it.

    ★ THE `create` BLOCK IS GONE, and this helper is what that cost. It used to carry the
    chat's parentage on the turn itself, so the server could check the workspace and write the row
    in ONE transaction — every refusal above the creation left nothing behind, and the project's
    chat list was afterwards exactly as long as it was before (R-18).

    An upload names the conversation it belongs to now, so the row has to exist before the first
    FILE goes up, which is a round trip before the turn. Two orderings cannot both be true. The
    tests below assert what the trade actually produces rather than the guarantee it replaced.
    """
    created = await _create_chat(client, headers, chat_id, project_id, kind=kind)
    if created.status_code not in (200, 201):
        return created
    return await client.post(
        f"/v1/conversations/{chat_id}/turns",
        headers=headers,
        json={"message": {"text": text, "attachmentTexts": [], "attachmentIds": []}},
    )


async def test_the_first_message_of_a_new_chat_can_carry_a_spreadsheet(
    client, db_session, set_chat_model, fake_redis, fake_storage, app, _fresh_engine
) -> None:
    """★ THIS WAS A HARD 500, on the opening move of a demo.

    The 500 was an adoption UPDATE autoflushed into a foreign key that did not exist yet: the
    upload stored NULL because the chat's row was written by the first send, and the send route
    linked it afterwards. Both halves of that are gone — the row exists before the upload, so the
    link is stamped at insert and nothing adopts anything.

    THE TEST STAYS because the journey it covers is the one that broke: a brand-new chat whose
    very first message carries a spreadsheet, driven through the real routes in the real order.
    """
    import base64
    import io

    from openpyxl import Workbook

    from src.api.v1.build_sessions.deps import sandbox_or_none_dependency
    from src.api.v1.conversations._shared import chat_storage
    from src.db.models.attachment import Attachment
    from tests.fakes import FakeSandboxClient

    set_chat_model(_streaming_text("ok"))
    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    await db_session.commit()
    headers = _headers(user)
    chat_id = uuid.uuid4()

    # THE CHAT FIRST. The composer creates the row before it uploads anything against it.
    created = await _create_chat(client, headers, chat_id, project.id, kind="plan")
    assert created.status_code == 201, created.text

    # A real workbook, because the upload door checks that it is one.
    book = io.BytesIO()
    Workbook().save(book)
    uploaded = await client.post(
        "/v1/attachments",
        headers=headers,
        json={
            "conversationId": str(chat_id),
            "attachmentId": "att_first_book",
            "name": "roster.xlsx",
            "mediaType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "base64": base64.b64encode(book.getvalue()).decode(),
        },
    )
    assert uploaded.status_code == 201, uploaded.text

    # The turn route reads its store through `chat_storage`, not the upload's
    # `storage_dependency` the conftest binds — an attachment id with no store is a typed 503.
    app.dependency_overrides[chat_storage] = lambda: fake_storage
    app.dependency_overrides[sandbox_or_none_dependency] = lambda: FakeSandboxClient()
    try:
        resp = await client.post(
            f"/v1/conversations/{chat_id}/turns",
            headers=headers,
            json={
                "message": {
                    "text": "what is in this roster?",
                    "attachmentTexts": [],
                    "attachmentIds": ["att_first_book"],
                },
            },
        )
        assert resp.status_code == 202, resp.text
        # The turn runs on after the 202, on the session the test shares with the app — wait for
        # it before reading, as every other first-message test in this file does.
        await _settle(_fresh_engine, chat_id)
    finally:
        app.dependency_overrides.pop(sandbox_or_none_dependency, None)
        app.dependency_overrides.pop(chat_storage, None)

    # The file belongs to the chat from the moment it was stored, so turn two still finds it.
    db_session.expire_all()
    linked = await db_session.scalar(
        sa.select(Attachment.conversation_id).where(Attachment.attachment_id == "att_first_book")
    )
    assert linked == chat_id


async def test_a_first_message_refused_by_the_workspace_leaves_an_empty_chat_and_no_turn(
    client, db_session, set_chat_model, fake_redis, fake_storage, app
) -> None:
    """★ THE TRADE THE NEW ORDERING MAKES, ASSERTED RATHER THAN ASSUMED — this test used to
    say the opposite.

    It used to pin R-18: a first message refused by the workspace left NOTHING, because the row
    was staged inside the turn's transaction and rolled back with it. The row is created a round
    trip earlier now, so the refusal leaves it behind.

    WHAT IS STILL TRUE IS THE HALF THAT COSTS THE CITIZEN SOMETHING. The chat is EMPTY and
    UNTITLED — no turn row, no spent card, and nothing named after text the platform just refused.
    The observed failure this all began with was a citizen watching a build run for two minutes
    and then being asked whether they wanted the workspace at all, with a chat titled after the
    refused message sitting in their project; that does not happen.

    Sweeping the empty row is a separate, already-tracked task. Recorded here so it is a known
    cost rather than something the next person discovers.
    """
    from src.api.v1.build_sessions.deps import sandbox_or_none_dependency
    from src.db.models.message import Message
    from src.services.build_sessions.manager import SandboxReclaimBlockedError, SessionManager
    from tests.fakes import FakeSandboxClient

    set_chat_model(_streaming_text("ok"))
    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    await db_session.commit()
    user_id, project_id, headers = user.id, project.id, _headers(user)
    before = await _conversation_count(db_session, user_id)
    chat_id = uuid.uuid4()

    async def _blocked(*a, **k):
        raise SandboxReclaimBlockedError(
            project_id=uuid.uuid4(), project_name="Car pool apps", app_id=uuid.uuid4(), dirty=True
        )

    monkey = SessionManager.reclaim_preflight
    SessionManager.reclaim_preflight = _blocked  # type: ignore[method-assign]
    app.dependency_overrides[sandbox_or_none_dependency] = lambda: FakeSandboxClient()
    try:
        resp = await _post_first_message(client, headers, chat_id, project_id)
    finally:
        SessionManager.reclaim_preflight = monkey  # type: ignore[method-assign]
        app.dependency_overrides.pop(sandbox_or_none_dependency, None)

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "sandbox_reclaim_blocked"

    db_session.expire_all()
    # The row is there — the cost, stated.
    assert await _conversation_count(db_session, user_id) == before + 1
    row = await db_session.scalar(sa.select(Conversation).where(Conversation.id == chat_id))
    assert row is not None
    # ...and it is EMPTY and UNTITLED, which is the half that still protects the citizen.
    assert not row.title
    messages = await db_session.scalar(
        sa.select(sa.func.count()).select_from(Message).where(Message.conversation_id == chat_id)
    )
    assert int(messages or 0) == 0, "a refused message must not be recorded"


async def test_every_other_side_effect_free_refusal_records_no_turn_either(
    client, db_session, set_chat_model, fake_redis, fake_storage, app
) -> None:
    """A fix that only covered the reclaim refusal would leave three other ways to record a turn
    the citizen never got. Each refusal below sits above the first write, and each is asserted
    against the message count rather than against the conversation list — which the create call a
    round trip earlier has already added to."""
    from src.api.v1.build_sessions.deps import sandbox_or_none_dependency
    from src.db.models.message import Message

    set_chat_model(_streaming_text("ok"))
    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    await db_session.commit()
    project_id, headers = project.id, _headers(user)
    chat_id = uuid.uuid4()

    # The one refusal that needs the sandbox seam UNBOUND — written here rather than assumed
    # by the suite's default fixture.
    app.dependency_overrides[sandbox_or_none_dependency] = lambda: None
    try:
        refused = await _post_first_message(client, headers, chat_id, project_id)
    finally:
        app.dependency_overrides.pop(sandbox_or_none_dependency, None)

    assert refused.status_code == 503
    db_session.expire_all()
    messages = await db_session.scalar(
        sa.select(sa.func.count()).select_from(Message).where(Message.conversation_id == chat_id)
    )
    assert int(messages or 0) == 0


async def test_a_project_someone_else_owns_is_refused_and_creates_nothing(
    client, db_session, set_chat_model, fake_redis, fake_storage
) -> None:
    """OWNERSHIP IS CHECKED BEFORE ANYTHING IS READ OR WRITTEN, and the 404 is the same
    non-leaking answer an unknown project gets — existence under another owner is not
    distinguishable from absence.

    The check moved with the creation: it is `POST /v1/conversations` that refuses now, one round
    trip earlier, so the refusal arrives before the citizen has even typed a message.
    """
    set_chat_model(_streaming_text("ok"))
    mine = await UserFactory.create(db_session)
    theirs = await UserFactory.create(db_session)
    their_project = await ProjectFactory.create(db_session, theirs.id)
    await db_session.commit()
    mine_id, theirs_id, project_id, headers = mine.id, theirs.id, their_project.id, _headers(mine)
    before = await _conversation_count(db_session, mine_id)

    resp = await _post_first_message(client, headers, uuid.uuid4(), project_id)

    assert resp.status_code == 404
    assert await _conversation_count(db_session, mine_id) == before
    assert await _conversation_count(db_session, theirs_id) == 0


async def test_a_first_message_with_the_workspace_free_creates_exactly_one_conversation(
    client, db_session, set_chat_model, fake_redis, fake_storage, _fresh_engine
) -> None:
    """The happy path: one conversation, carrying the kind it was created with, and one turn."""
    set_chat_model(_streaming_text("ok"))
    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    await db_session.commit()
    user_id, project_id, headers = user.id, project.id, _headers(user)
    chat_id = uuid.uuid4()

    resp = await _post_first_message(client, headers, chat_id, project_id, kind="plan")

    assert resp.status_code == 202, resp.text
    await _settle(_fresh_engine, chat_id)

    row = await db_session.scalar(sa.select(Conversation).where(Conversation.id == chat_id))
    assert row is not None
    assert row.kind is ChatKind.PLAN  # the kind it was created with, and nothing may change it
    assert row.project_id == project_id
    assert await _conversation_count(db_session, user_id) == 1


async def test_the_first_message_lands_in_the_row_the_create_call_made(
    client, db_session, set_chat_model, fake_redis, fake_storage, _fresh_engine
) -> None:
    """★ THE PAIRING THAT REPLACED "durable together".

    The row and its first message used to become durable in one commit — a flush, never a commit,
    so a failure between the two left neither. The row is committed by its own route now, so what
    is left to assert is that the message lands in THAT row rather than in one the turn made for
    itself: two rows under one client-minted id would be the failure this ordering could produce.
    """
    from sqlalchemy import func, select

    from src.db.models.message import Message

    set_chat_model(_streaming_text("ok"))
    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    await db_session.commit()
    chat_id = uuid.uuid4()

    resp = await _post_first_message(client, _headers(user), chat_id, project.id)
    assert resp.status_code == 202
    await _settle(_fresh_engine, chat_id)

    db_session.expire_all()
    assert await db_session.get(Conversation, chat_id) is not None
    with_that_id = await db_session.scalar(
        select(func.count()).select_from(Conversation).where(Conversation.id == chat_id)
    )
    assert int(with_that_id or 0) == 1
    messages = await db_session.scalar(
        select(func.count()).select_from(Message).where(Message.conversation_id == chat_id)
    )
    assert int(messages or 0) >= 1, "the row exists but its first message does not"


async def test_a_second_message_in_an_existing_conversation_is_unchanged(
    client, db_session, set_chat_model, fake_redis, fake_storage, _fresh_engine
) -> None:
    """THIS IS THE FIRST-MESSAGE PATH ONLY. A chat that already exists takes no create call at
    all — the composer makes one only when `seq` is zero."""
    set_chat_model(_streaming_text("ok"))
    user, conv = await _auth_with_conversation(db_session)
    user_id, conv_id, headers = user.id, conv.id, _headers(user)
    before = await _conversation_count(db_session, user_id)

    plain = await _post_turn(client, headers, conv)
    assert plain.status_code == 202
    await _settle(_fresh_engine, conv_id)

    assert await _conversation_count(db_session, user_id) == before


async def test_creating_a_chat_that_already_exists_is_idempotent_not_a_conflict(
    client, db_session, set_chat_model, fake_redis, fake_storage, _fresh_engine
) -> None:
    """★ THE RETRY AFTER A REFUSED FIRST SEND, which the new ordering makes an ordinary case
    rather than a rare
    one: the refusal leaves a real, empty chat, and the citizen's second attempt calls create
    again on the same id. If that answered 409 the leftover row would refuse its own retry, and
    the citizen would be stuck in a chat they cannot use and cannot delete.

    Two tabs racing the same mint take the same path. The existing row wins outright: a chat's
    kind is fixed at creation, and re-creating it is not a route that changes one.
    """
    set_chat_model(_streaming_text("ok"))
    user, conv = await _auth_with_conversation(db_session, kind=ChatKind.BUILD)
    user_id, conv_id, project_id, headers = user.id, conv.id, conv.project_id, _headers(user)
    before = await _conversation_count(db_session, user_id)

    again = await _create_chat(client, headers, conv_id, project_id, kind="build")
    assert again.status_code == 200, again.text  # 200, not 201 and not 409

    resp = await _post_turn(client, headers, conv)
    assert resp.status_code == 202
    await _settle(_fresh_engine, conv_id)

    row = await db_session.scalar(sa.select(Conversation).where(Conversation.id == conv_id))
    assert row is not None
    assert row.kind is ChatKind.BUILD
    assert await _conversation_count(db_session, user_id) == before


# ★ THE TWO INSERT-RACE TESTS ARE GONE, WITH THE ARM THEY COVERED.
#
# They drove `turns.py`'s `except IntegrityError` branch: two first messages on one client-minted
# id in flight at once, both finding nothing at the idempotency read and one losing the insert —
# and they pinned the loser joining the winner's chat, taking the winner's PROJECT, and keeping
# its own message. That branch existed because this route created conversations.
#
# It does not any more, so the race moved with the creation to `POST /v1/conversations`,
# which has had the identical arm all along and is covered by `test_create.py`'s idempotency and
# conflict cases. Deleting the tests here rather than re-pointing them is deliberate: re-pointed,
# they would assert `test_create.py`'s behaviour under this file's name and through a route that
# no longer has a branch to take.


async def test_an_unknown_conversation_is_a_404_on_every_turn(
    client, db_session, set_chat_model, fake_redis, fake_storage
) -> None:
    """Unchanged for every turn, and now true of the first one as well: this route builds no row
    from anything, so an unknown id is a client bug — and a cross-user id is indistinguishable
    from it, which is one non-leaking answer."""
    set_chat_model(_streaming_text("ok"))
    user = await UserFactory.create(db_session)
    await db_session.commit()

    resp = await _post_turn(client, _headers(user), SimpleNamespace(id=uuid.uuid4()))

    assert resp.status_code == 404
