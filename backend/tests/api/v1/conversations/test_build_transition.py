"""The Build-it transition (U5): flip → record → START A TURN, in one endpoint.

Build-it no longer starts a build session; it starts a WRITE turn, because a build IS a turn
now. Two consequences these tests pin: the response carries a `turnId` (there is no session to
attach to), and the machine-authored seed prompt is persisted HIDDEN — it is the platform
instructing the model, and rendering it as a user bubble is the "I never said that" moment the
transcript must never produce.

Every former `build_failed` outcome is a typed HTTP status the client's fetch layer already
handles: 429 for the daily cap, 409 for a busy workspace, 503 for an unconfigured engine.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from types import SimpleNamespace

import pytest
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel

from src.api.v1.build_sessions.deps import (
    run_build_dependency,
    sandbox_dependency,
    sandbox_or_none_dependency,
    session_manager_dependency,
)
from src.config import settings
from src.db.models.conversation import ConversationMode
from src.db.models.message import MessageVisibility
from src.db.models.user_limit import UserLimit
from src.services.build_sessions import SessionManager
from src.services.messages.projection import PlanOptionsItem, project_rows
from src.services.messages.store import load_rows
from src.services.turns.engine import TurnEngine, set_turn_engine_for_tests
from src.services.turns.guard import _mid_reply
from src.services.usage.gate import record_usage
from tests.api.v1.build_sessions.conftest import _sandbox_config
from tests.api.v1.conversations.test_turn_stream import _headers
from tests.factories import ConversationFactory, UserFactory
from tests.fakes import FakeBrain, FakeSandboxClient


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


@pytest.fixture
def wire(app, db_session, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    monkeypatch.setattr(settings, "sandbox", _sandbox_config())

    @contextlib.asynccontextmanager
    async def _session():
        yield db_session

    manager = SessionManager(session_factory=lambda: _session())
    sbx = FakeSandboxClient()
    app.dependency_overrides[session_manager_dependency] = lambda: manager
    app.dependency_overrides[sandbox_dependency] = lambda: sbx
    app.dependency_overrides[sandbox_or_none_dependency] = lambda: sbx
    app.dependency_overrides[run_build_dependency] = lambda: FakeBrain()
    return SimpleNamespace(app=app, manager=manager, sbx=sbx)


@pytest.fixture(autouse=True)
def _bind_chat_storage(app, fake_storage) -> None:
    """The stale-plan check reads the snapshot through `chat_storage` — bind it to the same
    fake the test seeds."""
    from src.api.v1.claude.router import chat_storage

    app.dependency_overrides[chat_storage] = lambda: fake_storage


def _streaming_text(text: str):
    async def _stream(_messages: list[ModelMessage], _info: AgentInfo):
        yield text

    return FunctionModel(stream_function=_stream)


def _plan_model(call_id: str = "opt-build"):
    async def _stream(_messages: list[ModelMessage], _info: AgentInfo):
        yield "The plan:\n1. Add the visitors table\n2. Wire the form\n"
        yield DeltaToolCalls(
            {0: DeltaToolCall(name="present_plan_options", json_args="{}", tool_call_id=call_id)}
        )

    return FunctionModel(stream_function=_stream)


async def _plan_conversation_with_card(
    client, db_session, set_chat_model, engine, *, call_id: str = "opt-build"
):
    """A Plan conversation whose newest state is a REAL pending card, produced through the
    genuine engine path (turn POST → deferred call → pending row)."""
    user = await UserFactory.create(db_session)
    conv = await ConversationFactory.create(db_session, user.id, mode=ConversationMode.PLAN)
    set_chat_model(_plan_model(call_id))
    headers = _headers(user)
    resp = await client.post(
        f"/v1/conversations/{conv.id}/turns",
        headers=headers,
        json={
            "message": {
                "text": "plan the visitors app",
                "attachmentTexts": [],
                "attachmentIds": [],
            }
        },
    )
    assert resp.status_code == 202, resp.text
    state = engine.peek(conv.id)
    assert state is not None and state.task is not None
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(state.task, timeout=10)
    return user, conv, headers


async def _settle(engine, conversation_id) -> None:
    state = engine.peek(conversation_id)
    if state is not None and state.task is not None:
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(state.task, timeout=10)


def _build_url(conv, call_id: str = "opt-build") -> str:
    return f"/v1/conversations/{conv.id}/plan-options/{call_id}/build"


async def test_build_it_flips_records_and_starts_a_turn_with_a_hidden_seed(
    client, db_session, set_chat_model, wire, _fresh_engine
) -> None:
    user, conv, headers = await _plan_conversation_with_card(
        client, db_session, set_chat_model, _fresh_engine
    )
    set_chat_model(_streaming_text("building it now"))

    resp = await client.post(_build_url(conv), headers=headers, json={"force": True})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["outcome"] == "started"
    assert body["turnId"]  # a TURN, not a session — the build IS a turn now
    await _settle(_fresh_engine, conv.id)

    await db_session.refresh(conv)
    # And it STAYS in Write. The end-sequence restore is gone: Write is no longer a dead end
    # someone has to be rescued out of, which was the whole point of the convergence.
    assert conv.mode is ConversationMode.WRITE

    rows = list(
        await load_rows(db_session, user_id=user.id, conversation_id=conv.id, include_hidden=True)
    )
    seeds = [r for r in rows if (r.meta or {}).get("kind") == "write_seed"]
    assert len(seeds) == 1
    assert seeds[0].visibility is MessageVisibility.HIDDEN
    # The citizen's transcript never shows the platform's instruction as something they typed.
    visible = project_rows([r for r in rows if r.visibility is MessageVisibility.VISIBLE])
    assert not any("Execute the approved plan" in getattr(i, "text", "") for i in visible)


async def test_the_daily_cap_is_a_429_and_leaves_the_card_pending(
    client, db_session, set_chat_model, wire, _fresh_engine
) -> None:
    """Was a 200 `build_failed` that burned the card. It is a typed 429 now, and because every
    refusal precedes the first write, the card is untouched — the user clicks Build tomorrow."""
    user, conv, headers = await _plan_conversation_with_card(
        client, db_session, set_chat_model, _fresh_engine
    )
    db_session.add(UserLimit(user_id=user.id, daily_token_limit=10))
    await record_usage(db_session, user.id, input_tokens=999, output_tokens=999)
    await db_session.commit()

    resp = await client.post(_build_url(conv), headers=headers, json={"force": True})

    assert resp.status_code == 429
    await db_session.refresh(conv)
    assert conv.mode is ConversationMode.PLAN  # nothing flipped
    rows = list(
        await load_rows(db_session, user_id=user.id, conversation_id=conv.id, include_hidden=True)
    )
    cards = [i for i in project_rows(rows) if isinstance(i, PlanOptionsItem)]
    assert cards and cards[-1].state == "pending"  # the card survives


async def test_a_workspace_busy_on_another_thread_is_a_409(
    client, db_session, set_chat_model, wire, _fresh_engine
) -> None:
    user, conv, headers = await _plan_conversation_with_card(
        client, db_session, set_chat_model, _fresh_engine
    )
    other = uuid.uuid4()
    wire.manager._sessions[other] = SimpleNamespace(session_id=other, conversation_id=uuid.uuid4())
    wire.manager._active_by_user[user.id] = other

    resp = await client.post(_build_url(conv), headers=headers, json={"force": True})

    assert resp.status_code == 409
    await db_session.refresh(conv)
    assert conv.mode is ConversationMode.PLAN


async def test_stale_plan_warns_and_force_proceeds(
    client, db_session, set_chat_model, wire, _fresh_engine, fake_storage
) -> None:
    """The card pinned head_sha=None (no app at Plan time); an app + snapshot appearing since
    is exactly a plan built on a stale picture — warn first, force proceeds."""
    from src.services.build_sessions.appdata import resolve_app_for_project
    from src.services.storage.keys import snapshot_key

    user, conv, headers = await _plan_conversation_with_card(
        client, db_session, set_chat_model, _fresh_engine
    )
    app_id = await resolve_app_for_project(db_session, user.id, conv.project_id)
    await db_session.flush()
    await fake_storage.put(
        snapshot_key(app_id), b"# v2 git bundle\n" + b"a" * 40 + b" HEAD\n\nPACK"
    )

    warn = await client.post(_build_url(conv), headers=headers, json={"force": False})
    assert warn.status_code == 200
    assert warn.json()["outcome"] == "stale_plan"
    await db_session.refresh(conv)
    assert conv.mode is ConversationMode.PLAN  # warned, not built

    set_chat_model(_streaming_text("ok"))
    forced = await client.post(_build_url(conv), headers=headers, json={"force": True})
    assert forced.json()["outcome"] == "started"
    await _settle(_fresh_engine, conv.id)


async def test_ownership_and_unknown_card(
    client, db_session, set_chat_model, wire, _fresh_engine
) -> None:
    _, conv, headers = await _plan_conversation_with_card(
        client, db_session, set_chat_model, _fresh_engine
    )
    stranger = await UserFactory.create(db_session, email="stranger@rvaiglobal.com")
    assert (
        await client.post(_build_url(conv), headers=_headers(stranger), json={"force": True})
    ).status_code == 404
    assert (
        await client.post(_build_url(conv, "nope"), headers=headers, json={"force": True})
    ).status_code == 400
