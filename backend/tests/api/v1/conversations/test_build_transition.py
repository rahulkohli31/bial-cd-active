"""The atomic Build-it transition (U12): record → flip → lock → start in one endpoint,
idempotent on the card id, with typed failure outcomes that RE-ARM the card and a
stale-plan warning that leaves the choice with the user. Drives the REAL SessionManager
(FakeSandboxClient + FakeBrain) so the lock, provision, and finalize seams are genuine."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel

from src.api.v1.build_sessions.deps import (
    run_build_dependency,
    sandbox_dependency,
    sandbox_or_none_dependency,
    session_manager_dependency,
)
from src.config import settings
from src.db.models.conversation import Conversation, ConversationMode
from src.db.models.message import Message, MessageEntryKind
from src.db.models.user_limit import UserLimit
from src.services.build_sessions import SessionManager
from src.services.messages.projection import PlanOptionsItem, project_rows
from src.services.messages.store import load_rows
from src.services.turns.engine import TurnEngine, set_turn_engine_for_tests
from src.services.turns.guard import _mid_reply
from src.services.usage.gate import record_usage
from tests.api.v1.build_sessions.conftest import BlockingBrain, _sandbox_config
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
    """The build_sessions `wire` fixture, replicated for this package (fixtures don't
    cross conftest packages; the helpers do)."""
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
    """The transition's stale-plan check reads the snapshot through `chat_storage` — bind
    it to the same fake the test seeds."""
    from src.api.v1.claude.router import chat_storage

    app.dependency_overrides[chat_storage] = lambda: fake_storage


def _plan_model(call_id: str = "opt-build"):
    async def _stream(messages: list[ModelMessage], info: AgentInfo):
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


async def _drain_active(wire, user_id: uuid.UUID) -> None:
    session = wire.manager.active_session_for(user_id)
    if session is not None and session.task is not None:
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(session.task, timeout=10)
        if session.finalize_task is not None:
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(session.finalize_task, timeout=10)


def _build_url(conv, call_id: str = "opt-build") -> str:
    return f"/v1/conversations/{conv.id}/plan-options/{call_id}/build"


async def test_build_it_starts_flips_and_records(
    client, db_session, set_chat_model, _fresh_engine, wire, fake_redis, fake_storage
) -> None:
    user, conv, headers = await _plan_conversation_with_card(
        client, db_session, set_chat_model, _fresh_engine
    )

    resp = await client.post(_build_url(conv), headers=headers, json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["outcome"] == "started"
    assert body["sessionId"] is not None and body["appId"] is not None
    await _drain_active(wire, user.id)

    # The mode flipped, the durable marker exists, and the card resolved as build.
    reloaded = await db_session.get(Conversation, conv.id)
    assert reloaded is not None and reloaded.mode is ConversationMode.WRITE
    rows = list(
        await db_session.scalars(
            sa.select(Message).where(Message.conversation_id == conv.id).order_by(Message.seq)
        )
    )
    markers = [r for r in rows if r.entry_kind is MessageEntryKind.MODE_SWITCH]
    assert len(markers) == 1
    assert "plan" in str(markers[0].payload) and "write" in str(markers[0].payload)
    cards = [i for i in project_rows(rows) if isinstance(i, PlanOptionsItem)]
    assert cards[-1].state == "build"

    # Idempotent double-click: the build already started — never a second session.
    again = await client.post(_build_url(conv), headers=headers, json={})
    assert again.json()["outcome"] == "already_built"


async def test_lock_held_records_failure_and_rearms(
    client, db_session, set_chat_model, _fresh_engine, wire, fake_redis, fake_storage
) -> None:
    user, conv, headers = await _plan_conversation_with_card(
        client, db_session, set_chat_model, _fresh_engine
    )
    # A genuinely live build occupies the one-per-user slot.
    blocker = BlockingBrain()
    wire.app.dependency_overrides[run_build_dependency] = lambda: blocker
    from tests.factories import ProjectFactory

    other_project = await ProjectFactory.create(db_session, user.id)
    start = await client.post(
        "/v1/build-sessions",
        headers=headers,
        json={"projectId": str(other_project.id), "prompt": "occupy the slot"},
    )
    assert start.status_code == 201

    resp = await client.post(_build_url(conv), headers=headers, json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["outcome"] == "build_failed" and body["reason"] == "lock_held"
    assert body["conflictSessionId"] is not None

    # The card re-armed (build_failed projects actionable) and the mode did NOT flip.
    rows = list(
        await load_rows(db_session, user_id=user.id, conversation_id=conv.id, include_hidden=True)
    )
    cards = [i for i in project_rows(rows) if isinstance(i, PlanOptionsItem)]
    assert cards[-1].state == "build_failed" and cards[-1].reason == "lock_held"
    reloaded = await db_session.get(Conversation, conv.id)
    assert reloaded is not None and reloaded.mode is ConversationMode.PLAN

    blocker.release()
    await _drain_active(wire, user.id)

    # RETRY after the failure: the slot is free — the same card now starts the build.
    wire.app.dependency_overrides[run_build_dependency] = lambda: FakeBrain()
    retry = await client.post(_build_url(conv), headers=headers, json={})
    assert retry.json()["outcome"] == "started"
    await _drain_active(wire, user.id)


async def test_daily_cap_records_typed_failure(
    client, db_session, set_chat_model, _fresh_engine, wire, fake_redis, fake_storage
) -> None:
    user, conv, headers = await _plan_conversation_with_card(
        client, db_session, set_chat_model, _fresh_engine
    )
    db_session.add(UserLimit(user_id=user.id, daily_token_limit=10))
    await db_session.flush()
    await record_usage(db_session, user.id, input_tokens=10, output_tokens=0)

    resp = await client.post(_build_url(conv), headers=headers, json={})
    body = resp.json()
    assert body["outcome"] == "build_failed" and body["reason"] == "daily_cap"
    rows = list(
        await load_rows(db_session, user_id=user.id, conversation_id=conv.id, include_hidden=True)
    )
    cards = [i for i in project_rows(rows) if isinstance(i, PlanOptionsItem)]
    assert cards[-1].state == "build_failed" and cards[-1].reason == "daily_cap"


async def test_stale_plan_warns_and_force_proceeds(
    client, db_session, set_chat_model, _fresh_engine, wire, fake_redis, fake_storage
) -> None:
    """The card pinned head_sha=None (no app at Plan time); an app + snapshot appearing
    since is exactly a plan built on a stale picture — warn first, force proceeds."""
    from src.services.build_sessions.appdata import resolve_app_for_project
    from src.services.storage.keys import snapshot_key

    user, conv, headers = await _plan_conversation_with_card(
        client, db_session, set_chat_model, _fresh_engine
    )
    app_id = await resolve_app_for_project(db_session, user.id, conv.project_id)
    await db_session.flush()
    valid_bundle = b"# v2 git bundle\n" + b"a" * 40 + b" HEAD\n\nPACK"
    await fake_storage.put(snapshot_key(app_id), valid_bundle)

    resp = await client.post(_build_url(conv), headers=headers, json={})
    body = resp.json()
    assert body["outcome"] == "stale_plan"
    assert body["planHeadSha"] is None and body["currentHeadSha"] is not None

    # The card is STILL pending (the warning resolved nothing).
    rows = list(
        await load_rows(db_session, user_id=user.id, conversation_id=conv.id, include_hidden=True)
    )
    cards = [i for i in project_rows(rows) if isinstance(i, PlanOptionsItem)]
    assert cards[-1].state == "pending"

    forced = await client.post(_build_url(conv), headers=headers, json={"force": True})
    assert forced.json()["outcome"] == "started"
    await _drain_active(wire, user.id)


async def test_ownership_and_unknown_card(
    client, db_session, set_chat_model, _fresh_engine, wire, fake_redis, fake_storage
) -> None:
    user, conv, headers = await _plan_conversation_with_card(
        client, db_session, set_chat_model, _fresh_engine
    )
    other = await UserFactory.create(db_session, email="other-tr@rvaiglobal.com")
    assert (
        await client.post(_build_url(conv), headers=_headers(other), json={})
    ).status_code == 404
    unknown = await client.post(
        f"/v1/conversations/{conv.id}/plan-options/nope/build", headers=headers, json={}
    )
    assert unknown.status_code == 400
