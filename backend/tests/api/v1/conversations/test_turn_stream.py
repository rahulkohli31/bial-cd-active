"""The U10 turn transport, end to end: POST starts a detached turn, GET subscribes with
catch-up-snapshot-then-tail, stop is the explicit cancel, and the frame union parses with
the callable-discriminator discipline (malformed KNOWN tag raises; unknown tag captured).

Also home to the HTTP-level no-overrides mode-gating proof U8 deferred here: an Ask-mode
turn's model-visible tool list carries no write tools, through the REAL route + engine.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid

import pytest
from pydantic import ValidationError
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, FunctionModel

from src.api.v1.conversations.schemas import TURN_STREAM_FRAME_ADAPTER, UnknownFrame
from src.api.v1.conversations.turns import KEEPALIVE_SECONDS
from src.config import settings
from src.services.auth.csrf import issue_csrf_token
from src.services.auth.session_jwt import mint_session_jwt
from src.services.turns.engine import TurnEngine, set_turn_engine_for_tests
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


async def _auth_with_conversation(db_session, *, mode=None):
    from src.db.models.conversation import ConversationMode

    user = await UserFactory.create(db_session)
    conv = await ConversationFactory.create(
        db_session, user.id, mode=mode if mode is not None else ConversationMode.ASK
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
    while not state.text_parts:  # the first delta is out — we are genuinely mid-turn
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
    assert frames[0].text_so_far == "first "
    # Snapshot + tail converge to the uninterrupted client's final text.
    deltas = "".join(f.text for f in frames if f.type == "text_delta")
    assert frames[0].text_so_far + deltas == "first second"
    assert frames[-1].type == "turn_ended" and frames[-1].status == "completed"
    assert events.text.endswith("data: [DONE]\n\n")


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
    while not state.text_parts:
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


async def test_ask_mode_model_sees_no_write_tools(
    client, db_session, set_chat_model, _fresh_engine
) -> None:
    """The HTTP-level no-overrides gating proof (U8's deferred test): through the REAL
    route, engine, and toolsets, an Ask turn's model-visible tool list is exactly the
    read surface — no write_file / edit_file / insert_lines / declare_done, and no
    present_plan_options either (that is Plan's)."""
    seen: dict[str, set[str]] = {}

    async def _capture(messages: list[ModelMessage], info: AgentInfo):
        seen["tools"] = {t.name for t in info.function_tools}
        yield "grounded answer"

    user, conv = await _auth_with_conversation(db_session)
    set_chat_model(FunctionModel(stream_function=_capture))
    assert (await _post_turn(client, _headers(user), conv)).status_code == 202
    await _settle(_fresh_engine, conv.id)
    assert seen["tools"] == {"read_file", "list_files", "search_files", "run_command"}


async def test_write_mode_direct_post_is_typed_400(
    client, db_session, set_chat_model, _fresh_engine
) -> None:
    from src.db.models.conversation import ConversationMode

    user = await UserFactory.create(db_session)
    conv = await ConversationFactory.create(db_session, user.id, mode=ConversationMode.WRITE)
    set_chat_model(_streaming_text("x"))
    resp = await _post_turn(client, _headers(user), conv)
    assert resp.status_code == 400
    assert "Build-it" in resp.json()["error"]["message"]


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
    while not state.text_parts:
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


def test_keepalive_budget_stays_pinned_under_the_client_stall_window() -> None:
    """The cross-repo timeout inequality (streamed-reply learning): the server keepalive
    must sit WELL under the portal reader's stall window. The portal side pins its 60s
    constant in `useConversationStream.test.ts` — 4x margin, re-derived on both sides."""
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
                    json_args="{}",
                    tool_call_id="opt-api" if minted["n"] == 1 else f"opt-api-{minted['n']}",
                )
            }
        )

    return FunctionModel(stream_function=_stream)


async def test_refine_click_resolves_over_the_api(
    client, db_session, set_chat_model, _fresh_engine
) -> None:
    from src.db.models.conversation import ConversationMode

    user, conv = await _auth_with_conversation(db_session, mode=ConversationMode.PLAN)
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
    from src.db.models.conversation import ConversationMode
    from src.services.turns.plan_options import find_pending

    user, conv = await _auth_with_conversation(db_session, mode=ConversationMode.PLAN)
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
