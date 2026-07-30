"""U1 consumer pin — the turns engine's `_watch_preview` believes `ready` before `running`.

`/dev/status.ready` now means "something is serving the dev port" (observed truth), which
mints a state that never existed before: `running=False, ready=True` — the supervisor's own
child is dead, but a server the agent relaunched itself answers the port. The watcher already
reads that as "serving" because its control flow consults `ready` first; these tests PIN that
ordering so a future reorder (consulting `running` first) goes red instead of silently
resurrecting the round-3 never-frames/false-reconnect bug. No production change rides here.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid

import pytest

import src.services.turns.engine as engine_mod
from src.db.models.conversation import ConversationMode
from src.services.orchestrator.deps import SandboxSession
from src.services.sandbox import DevStatus, SandboxHandle
from src.services.turns.engine import TurnEngine, _TurnState
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
