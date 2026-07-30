"""U1 consumer pin — the turns engine's `_watch_preview` believes `ready` before `running`.

`/dev/status.ready` now means "something is serving the dev port" (observed truth), which
mints a state that never existed before: `running=False, ready=True` — the supervisor's own
child is dead, but a server the agent relaunched itself answers the port. The watcher already
reads that as "serving" because its control flow consults `ready` first; these tests PIN that
ordering so a future reorder (consulting `running` first) goes red instead of silently
resurrecting the round-3 never-frames/false-reconnect bug.

The file also pins the watcher's LIFETIME: every mode that attaches the live container starts
one, so every mode's terminal must stop it — not just Write's own loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid

import pytest
from pydantic import SecretStr
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, FunctionModel

import src.services.turns.engine as engine_mod
from src.config import settings
from src.db.models.conversation import ConversationMode
from src.services.agent.mode_prompts import PromptContext
from src.services.build_sessions.manager import SessionManager
from src.services.orchestrator.deps import SandboxSession
from src.services.sandbox import DevStatus, SandboxHandle
from src.services.sandbox.config import SandboxConfig
from src.services.turns.engine import TurnEngine, _TurnState, set_turn_engine_for_tests
from src.services.turns.guard import _mid_reply
from tests.factories import ConversationFactory, ProjectFactory, UserFactory
from tests.fakes import FakeSandboxClient


class _ScriptedStatusSandbox(FakeSandboxClient):
    """`dev_status` returns exactly the scripted flags — the two are deliberately UNCOUPLED,
    because the state under test is precisely the one where they disagree."""

    def __init__(self, *, running: bool, ready: bool) -> None:
        super().__init__()
        self.scripted_running = running
        self.scripted_ready = ready

    async def dev_status(self, handle: SandboxHandle) -> DevStatus:
        return DevStatus(running=self.scripted_running, ready=self.scripted_ready, port=3000)


def _framed_state(client: FakeSandboxClient) -> _TurnState:
    """A Write-turn state whose preview is already up on screen — the demo's mid-build shape."""
    state = _TurnState(
        turn_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        mode=ConversationMode.WRITE,
    )
    fqdn = "sbx-test.westeurope.azurecontainerapps.io"
    state.sandbox = SandboxSession(
        sandbox_client=client,
        handle=SandboxHandle(
            fqdn=fqdn,
            token="tok-test",  # noqa: S106 - a fake, never a real bearer
            app_name="sbx-test",
            preview_url=f"https://{fqdn}/",
            ready=True,
        ),
        app_id=uuid.uuid4(),
    )
    state.preview_framed = True
    return state


async def _poll_a_while(state: _TurnState) -> None:
    task = asyncio.create_task(TurnEngine()._watch_preview(state))
    for _ in range(50):  # plenty of zero-delay poll iterations
        await asyncio.sleep(0)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def _reconnecting_frames(state: _TurnState) -> list[object]:
    return [
        f
        for f in state.ring
        if getattr(f, "type", None) == "preview" and getattr(f, "state", None) == "reconnecting"
    ]


async def test_a_framed_preview_survives_a_dead_child_that_still_serves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE demo row (`running=False, ready=True`): the child was pkill'd, the agent's nohup
    replacement serves the port. The framed preview must NOT flip to reconnecting — the app is
    live. Reorder the watcher to consult `running` before `ready` and this goes red."""
    monkeypatch.setattr(engine_mod, "READINESS_POLL_S", 0)
    state = _framed_state(_ScriptedStatusSandbox(running=False, ready=True))

    await _poll_a_while(state)

    assert _reconnecting_frames(state) == []
    assert state.preview_state != "reconnecting"


async def test_a_framed_preview_with_a_dead_port_still_reconnects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The companion boundary: `running=False, ready=False` (child dead, nothing serving) must
    still emit the reconnecting frame — the probe widened `ready`, not the crash signal."""
    monkeypatch.setattr(engine_mod, "READINESS_POLL_S", 0)
    state = _framed_state(_ScriptedStatusSandbox(running=False, ready=False))

    await _poll_a_while(state)

    assert len(_reconnecting_frames(state)) == 1  # an edge, not one per poll
    assert state.preview_state == "reconnecting"


# ─── the watcher's lifetime: read-mode turns must not leak it ─────────────────────────────


@pytest.fixture
def _sandbox_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings,
        "sandbox",
        SandboxConfig(
            subscription_id="s",
            resource_group="r",
            region="westeurope",
            managed_environment_name="aca-env",
            acr_server="acr.azurecr.io",
            acr_username="acr-user",
            acr_password=SecretStr("acr-pass"),
            image_ref="acr/img:latest",
        ),
    )


@pytest.fixture
def session_factory(db_session):
    @contextlib.asynccontextmanager
    async def _session():
        yield db_session

    return lambda: _session()


async def test_an_ask_turn_stops_the_watcher_it_started(
    _sandbox_configured, db_session, session_factory, fake_redis, fake_storage
) -> None:
    """★ The leak pin. `_attach_sandbox` starts the watcher for EVERY mode that attaches the
    live container, but only the Write loop's own `finally` stopped it — an Ask turn ended
    with the task still polling, free to land a preview frame after the transport had already
    sent [DONE]. The universal backstop in `_run_turn`'s terminal `finally` is what this
    asserts: remove it and `preview_task` is still a live task at the terminal."""
    _mid_reply.clear()
    engine = TurnEngine()
    set_turn_engine_for_tests(engine)
    try:
        user = await UserFactory.create(db_session, email="pw-ask@rvaiglobal.com")
        project = await ProjectFactory.create(db_session, user.id)
        conv = await ConversationFactory.create(
            db_session, user.id, project_id=project.id, mode=ConversationMode.ASK
        )

        async def _answer(_messages: list[ModelMessage], _info: AgentInfo):
            yield "It lists the visitors."

        async def _noop() -> None:
            return None

        await engine.start_turn(
            conversation=conv,
            user_id=user.id,
            prompt="what does the home page do?",
            history=[],
            prompt_context=PromptContext(
                user_name="Ada", project_name="Visitors", project_description=None
            ),
            app_id=None,
            project_id=project.id,
            model=FunctionModel(stream_function=_answer),
            session_factory=session_factory,
            persist_user_turn=_noop,
            manager=SessionManager(),
            sandbox_client=FakeSandboxClient(),
        )
        state = engine.peek(conv.id)
        assert state is not None and state.task is not None
        await asyncio.wait_for(state.task, timeout=10)

        assert state.status == "completed"
        assert state.sandbox is not None  # the container attached, so the watcher DID start
        assert state.preview_task is None  # …and the terminal stopped it — nothing left polling
    finally:
        set_turn_engine_for_tests(None)
        _mid_reply.clear()
