"""Consumer pin — the turns engine's `_watch_preview` believes `ready` before `running`.

`/dev/status.ready` now means "something is serving the dev port" (observed truth), which
mints a state that never existed before: `running=False, ready=True` — the supervisor's own
child is dead, but a server the agent relaunched itself answers the port. The watcher already
reads that as "serving" because its control flow consults `ready` first; these tests PIN that
ordering so a future reorder (consulting `running` first) goes red instead of silently
resurrecting the never-frames/false-reconnect bug.

The file also pins the watcher's LIFETIME: every mode that attaches the live container starts
one, so every mode's terminal must stop it — not just Write's own loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid

import pytest
import redis.asyncio as aioredis
import sqlalchemy as sa
import structlog.testing
from pydantic import SecretStr
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, FunctionModel
from redis.exceptions import RedisError

import src.services.turns.engine as engine_mod
from src.config import settings
from src.db.base import async_session_factory
from src.db.models.conversation import ChatKind
from src.db.models.harness_counter import HarnessCount, HarnessCounter
from src.services.agent.mode_prompts import PromptContext
from src.services.build_sessions import locks
from src.services.build_sessions.alarms import (
    APP_SERVING_LOST_EVENT,
    SERVING_PROOF_STAMP_REFUSED,
)
from src.services.build_sessions.manager import SessionManager
from src.services.orchestrator.deps import SandboxSession
from src.services.redis import REGISTRY_STATE_READY, registry_key
from src.services.redis.keys import (
    REGISTRY_FIELD_APP_NAME,
    REGISTRY_FIELD_SERVING_SINCE,
    REGISTRY_FIELD_STATE,
)
from src.services.sandbox import DevStatus, SandboxError, SandboxHandle
from src.services.sandbox.config import SandboxConfig
from src.services.turns.engine import TurnEngine, _TurnState, set_turn_engine_for_tests
from src.services.turns.guard import _mid_reply
from tests.factories import ConversationFactory, ProjectFactory, UserFactory
from tests.fakes import FakeSandboxClient


class _ScriptedStatusSandbox(FakeSandboxClient):
    """`dev_status` returns exactly the scripted flags — the three are deliberately UNCOUPLED,
    because the states under test are precisely the ones where they disagree.

    `root_status` defaults to `None` and is a THIRD independent flag, not a derived one: it is
    what the app ROOT answered, while `ready` is the supervisor's fail-open "something answered
    the dev port" (a 404 and a 500 both count, on purpose). `None` is the reading every test
    written before the page gate existed was made under — the pre-`root_status` supervisor image,
    which `shows_a_page` grandfathers as a page — so leaving it out changes nothing, and the
    tests that mean the page-less reading pass 404 or 500 and say so."""

    def __init__(self, *, running: bool, ready: bool, root_status: int | None = None) -> None:
        super().__init__()
        self.scripted_running = running
        self.scripted_ready = ready
        self.root_status = root_status

    async def dev_status(self, handle: SandboxHandle) -> DevStatus:
        return DevStatus(
            running=self.scripted_running,
            ready=self.scripted_ready,
            port=3000,
            root_status=self.root_status,
        )


def _framed_state(client: FakeSandboxClient) -> _TurnState:
    """A Write-turn state whose preview is already up on screen — the demo's mid-build shape."""
    state = _TurnState(
        turn_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        kind=ChatKind.BUILD,
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


def _start_the_watcher(state: _TurnState) -> asyncio.Task[None]:
    return asyncio.create_task(TurnEngine()._watch_preview(state))


async def _let_it_poll() -> None:
    """Give a RUNNING watcher room for many polls without ending it — the half of the loop a
    test needs when the reading changes mid-turn and both sides of the change are the claim."""
    for _ in range(50):  # plenty of zero-delay poll iterations
        await asyncio.sleep(0)


async def _stop_the_watcher(task: asyncio.Task[None]) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def _poll_a_while(state: _TurnState) -> None:
    """One unchanging reading, polled to death — the shape most of this file wants."""
    task = _start_the_watcher(state)
    await _let_it_poll()
    await _stop_the_watcher(task)


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


class _SlowRenderSandbox(FakeSandboxClient):
    """The healthy-but-slow shape: `running=False` for the container's whole life (a dev server
    the agent started itself through the open-sandbox surface, which is the state
    `_dev_is_serving()` exists to see) and `ready` False only while the root route is still
    rendering."""

    def __init__(self, *, negative_polls: int) -> None:
        super().__init__()
        self.negative_polls = negative_polls
        self.polls = 0

    async def dev_status(self, handle: SandboxHandle) -> DevStatus:
        self.polls += 1
        return DevStatus(running=False, ready=self.polls > self.negative_polls, port=3000)


async def test_a_slow_render_does_not_read_as_a_dev_process_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ THE FLAP. `/dev/status` answers from a bounded wait on an in-flight probe (2s) while a
    real cold root render takes longer, and a negative is never cached — so a healthy app
    answers not-ready while rendering, and paired with `running: false` (the NORMAL state for
    a self-started dev server) used to read as a crash edge: the citizen's iframe re-mounted
    every few seconds over an app that was merely slow.

    TWO negatives, written as a literal rather than derived from the constant — a test that says
    `CRASH_EDGE_CONSECUTIVE_POLLS - 1` can never go red. Mutation check: set the constant to 1
    or 2 and this fails."""
    monkeypatch.setattr(engine_mod, "READINESS_POLL_S", 0)
    client = _SlowRenderSandbox(negative_polls=2)
    state = _framed_state(client)

    await _poll_a_while(state)

    assert client.polls > 3, "guard the premise: it really did poll past the slow window"
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


# ─── the serving proof this watcher writes and takes back ────────────────────────────────
#
# The watcher is the FIRST of the four observers to know a build's app is answering: it already
# polls `/dev/status` every second and already reads the one signal that means "a request to the
# app root actually succeeded". Until this change it threw that away, and the platform reported
# a container as running from the moment ACA SCHEDULED it — which is how, on 2026-09-10, a
# citizen's pane framed nginx's "This app isn't running right now" page for eight seconds inside
# a perfectly healthy build while the live region announced the preview was live.
#
# It is also the only observer that can see the app DIE inside a turn, so it owns the retraction
# too — debounced on the same streak the reconnecting frame uses, and never on anything weaker.


async def _the_registry_says(
    redis: aioredis.Redis, state: _TurnState, *, serving_since: str
) -> None:
    """The hash `_write_registry` leaves behind for the container this turn is watching."""
    assert state.sandbox is not None
    await redis.hset(
        registry_key(state.user_id),
        mapping={
            REGISTRY_FIELD_APP_NAME: state.sandbox.handle.app_name,
            REGISTRY_FIELD_STATE: REGISTRY_STATE_READY,
            REGISTRY_FIELD_SERVING_SINCE: serving_since,
        },
    )


async def _stamp(redis: aioredis.Redis, state: _TurnState) -> str | None:
    # `decode_responses=True` on the fixture means this is always `str | None`; redis-py's own
    # annotation still admits `bytes`, so the narrowing is spelled rather than asserted.
    raw = await redis.hget(registry_key(state.user_id), REGISTRY_FIELD_SERVING_SINCE)
    return raw.decode() if isinstance(raw, bytes) else raw


async def test_the_watcher_records_the_instant_it_watches_the_app_answer(
    fake_redis: aioredis.Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ THE FIX, from the observer's end. `/dev/status.ready` is the signal — deliberately NOT
    the dev server's own "Ready in Nms" stdout marker, which prints before the first route
    compiles and once had a blank page announced as finished."""
    monkeypatch.setattr(engine_mod, "READINESS_POLL_S", 0)
    state = _framed_state(_ScriptedStatusSandbox(running=True, ready=True))
    # The un-framed shape — a build's very first serve, where this watcher is also the emitter
    # that gets to claim the frame. Its sibling below is the same run with the claim gone.
    state.preview_framed = False
    await _the_registry_says(fake_redis, state, serving_since="")

    await _poll_a_while(state)

    stamped = await _stamp(fake_redis, state)
    assert stamped is not None and stamped != "", "the watcher saw the app answer and said nothing"
    assert state.preview_framed is True, "guard the premise: this run really did take the claim"


async def test_the_proof_is_recorded_even_when_another_emitter_took_the_frame_claim(
    fake_redis: aioredis.Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ THE ONE THAT WOULD HAVE SHIPPED SILENTLY BROKEN. `claim_preview_frame` is a
    once-per-turn ONE-SHOT with a second caller — the self-heal verify path — and the stamp was
    originally specified to live inside `if first_serve or reconnecting:`. Whenever verify won
    the claim, nothing would ever have stamped: the app serves, the pane sits on "getting your
    app ready" over a working app until the five-minute reaper, and every test in this file
    would have stayed green because none of them takes the claim first.

    The compare-and-set's own first-serve-wins rule supplies the once-only property the claim was
    being borrowed for, so the proof goes down on the OBSERVATION and the claim goes back to
    meaning what its name says: who emits the frame.

    Mutation-check: move the `_prove_it_serves` call back under `if first_serve or reconnecting:`
    and this goes red while its sibling above stays green."""
    monkeypatch.setattr(engine_mod, "READINESS_POLL_S", 0)
    # `_framed_state` is exactly the shape a spent claim leaves behind — `preview_framed` is
    # already True, so `claim_preview_frame()` answers False for the watcher from its first poll
    # onward, which is what the verify path winning the race looks like from in here.
    state = _framed_state(_ScriptedStatusSandbox(running=True, ready=True))
    await _the_registry_says(fake_redis, state, serving_since="")
    assert state.claim_preview_frame() is False, "the premise: the claim is already gone"

    await _poll_a_while(state)

    stamped = await _stamp(fake_redis, state)
    assert stamped is not None and stamped != "", (
        "the stamp is gated on a token another consumer can take, so nothing ever stamps when "
        "that consumer wins"
    )


async def test_a_dev_server_that_dies_inside_the_turn_has_its_proof_taken_back(
    fake_redis: aioredis.Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The crash edge, on the same debounced streak as the reconnecting frame. Without it an app
    that dies after serving keeps reading RUNNING, and the next page load frames nginx's
    app-gone 404 with no card over it — the reconnecting cover does not survive a reload.

    BACK TO THE SENTINEL, NEVER DELETED: absent is the pre-cutover reading and is grandfathered
    as PROVEN, so a delete here would turn a crashed app into a running one."""
    monkeypatch.setattr(engine_mod, "READINESS_POLL_S", 0)
    state = _framed_state(_ScriptedStatusSandbox(running=False, ready=False))
    await _the_registry_says(fake_redis, state, serving_since="2026-09-10T09:41:04+00:00")

    await _poll_a_while(state)

    assert await fake_redis.hexists(registry_key(state.user_id), REGISTRY_FIELD_SERVING_SINCE) == 1
    assert await _stamp(fake_redis, state) == ""


async def test_a_turn_that_framed_nothing_still_takes_back_an_earlier_turns_proof(
    fake_redis: aioredis.Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ THE CRASH EDGE'S SECOND LATCH, and why it is kept apart from `reconnecting`. That flag
    is gated on `state.preview_framed` — correct for the SSE frame, which must not announce a
    reconnect for an iframe it never told the client to mount — but wrong for the proof, which
    is a fact about the CONTAINER and outlives any one turn's framing.

    The shape: an Ask turn attaches a container an EARLIER turn already stamped, and watches it
    die without ever framing anything. A proof-clear riding `preview_framed` would leave that
    dead app reading RUNNING to every tab in the platform.

    Mutation-check: gate the retraction on `state.preview_framed` and this goes red while
    `test_a_dev_server_that_dies_inside_the_turn_has_its_proof_taken_back` stays green."""
    monkeypatch.setattr(engine_mod, "READINESS_POLL_S", 0)
    state = _framed_state(_ScriptedStatusSandbox(running=False, ready=False))
    state.preview_framed = False  # this turn never put an iframe on screen
    await _the_registry_says(fake_redis, state, serving_since="2026-09-10T09:41:04+00:00")

    await _poll_a_while(state)

    assert await _stamp(fake_redis, state) == ""
    assert _reconnecting_frames(state) == [], (
        "guard the premise: an un-framed turn announces no reconnect, so the retraction really "
        "did happen on its own latch"
    )


async def test_a_dead_child_that_still_serves_keeps_its_proof(
    fake_redis: aioredis.Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE demo row again, now for the proof rather than the frame. The agent can `pkill` our
    child and `nohup` its own replacement through the open-sandbox surface, so
    `running=False, ready=True` is a NORMAL state for an app serving its citizen perfectly well.
    Retracting on `running` alone would take the frame away from exactly those apps."""
    monkeypatch.setattr(engine_mod, "READINESS_POLL_S", 0)
    state = _framed_state(_ScriptedStatusSandbox(running=False, ready=True))
    await _the_registry_says(fake_redis, state, serving_since="2026-09-10T09:41:04+00:00")

    await _poll_a_while(state)

    assert await _stamp(fake_redis, state) == "2026-09-10T09:41:04+00:00"


async def test_a_slow_route_render_never_costs_the_app_its_proof(
    fake_redis: aioredis.Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ready=False` ALONE IS NOT A DEATH. The supervisor's readiness answer comes off a bounded
    2-second probe and a real cold render of a heavy route takes longer, so a healthy app answers
    not-ready while it works. A live child resets the streak on every poll, which is why this
    never reaches the crash edge at all.

    Mutation-check: drop `not status.running` from the crash-edge condition and this goes red."""
    monkeypatch.setattr(engine_mod, "READINESS_POLL_S", 0)
    state = _framed_state(_ScriptedStatusSandbox(running=True, ready=False))
    await _the_registry_says(fake_redis, state, serving_since="2026-09-10T09:41:04+00:00")

    await _poll_a_while(state)

    assert await _stamp(fake_redis, state) == "2026-09-10T09:41:04+00:00"


class _UnreachableSupervisor(FakeSandboxClient):
    """A container whose supervisor will not answer at all — a transport failure, not a verdict
    about the app."""

    async def dev_status(self, handle: SandboxHandle) -> DevStatus:
        raise SandboxError("the supervisor did not answer")


async def test_a_supervisor_that_will_not_answer_never_costs_the_app_its_proof(
    fake_redis: aioredis.Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UNREACHABLE IS NOT DEAD. The `except SandboxError` arm continues without counting the
    poll, so a wedged ingress or a busy supervisor cannot retract a citizen's standing proof —
    the same asymmetry the reconciler's probe is built on, and the reason a fleet-wide ARM
    outage cannot unframe the fleet."""
    monkeypatch.setattr(engine_mod, "READINESS_POLL_S", 0)
    state = _framed_state(_UnreachableSupervisor())
    await _the_registry_says(fake_redis, state, serving_since="2026-09-10T09:41:04+00:00")

    await _poll_a_while(state)

    assert await _stamp(fake_redis, state) == "2026-09-10T09:41:04+00:00"


async def test_a_container_that_never_served_logs_no_loss_when_it_dies(
    fake_redis: aioredis.Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The compare-and-set answering 0 is what stops the crash edge reporting a loss that never
    happened: this container had no proof to take back. An `app_serving_lost` line here would
    tell an operator an app stopped serving when it had never started."""
    monkeypatch.setattr(engine_mod, "READINESS_POLL_S", 0)
    state = _framed_state(_ScriptedStatusSandbox(running=False, ready=False))
    await _the_registry_says(fake_redis, state, serving_since="")

    with structlog.testing.capture_logs() as logs:
        await _poll_a_while(state)

    assert [e for e in logs if e.get("event") == APP_SERVING_LOST_EVENT] == []
    assert await _stamp(fake_redis, state) == ""


async def test_a_stamp_is_never_written_onto_another_projects_container(
    fake_redis: aioredis.Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ THE NEAR-MISS, from the watcher's end. The registry key is per USER and survives a
    container swap, so a turn still polling after this citizen's one workspace flipped to
    another of their projects would otherwise stamp "serving" onto a container it never watched
    — and the pane would frame the new project's app on the old one's evidence.

    The refusal is a WARNING and not silence, because this is the single most dangerous event in
    the design: it is the near-miss of proving the wrong container."""
    monkeypatch.setattr(engine_mod, "READINESS_POLL_S", 0)
    state = _framed_state(_ScriptedStatusSandbox(running=True, ready=True))
    assert state.sandbox is not None
    await fake_redis.hset(
        registry_key(state.user_id),
        mapping={
            REGISTRY_FIELD_APP_NAME: "sbx-somebody-elses",
            REGISTRY_FIELD_STATE: REGISTRY_STATE_READY,
            REGISTRY_FIELD_SERVING_SINCE: "",
        },
    )

    with structlog.testing.capture_logs() as logs:
        await _poll_a_while(state)

    assert await _stamp(fake_redis, state) == "", "the successor container was stamped"
    refusals = [e for e in logs if e.get("event") == SERVING_PROOF_STAMP_REFUSED]
    assert refusals, "the near-miss went unrecorded"
    # A BOOL, never the other project's container name: the id vocabulary in this log stays
    # user-scoped, and naming the loser would put one of the citizen's projects into another's
    # build trace.
    assert refusals[0]["found_app_present"] is True
    assert "sbx-somebody-elses" not in str(refusals[0])


async def test_a_store_that_will_not_answer_is_asked_again_and_named_once(
    fake_redis: aioredis.Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The turn stops asking only once the store has ANSWERED. An outage has said nothing about
    whether the app served, so the next poll asks again, and the outage is one line per turn
    rather than one a second.

    Mutation-check: latch the proof before the write is attempted and the stamp assertion goes
    red; drop the once-per-turn guard and the line count does."""
    monkeypatch.setattr(engine_mod, "READINESS_POLL_S", 0)
    state = _framed_state(_ScriptedStatusSandbox(running=True, ready=True))
    await _the_registry_says(fake_redis, state, serving_since="")
    asked = {"times": 0}

    async def down_for_three_asks(
        redis: aioredis.Redis,
        user_uuid: uuid.UUID,
        *,
        app_name: str,
        observer: str,
        cold: bool | None,
    ) -> None:
        asked["times"] += 1
        if asked["times"] <= 3:
            raise RedisError("redis is down")
        await locks.record_the_first_serve(
            redis, user_uuid, app_name=app_name, observer=observer, cold=cold
        )

    monkeypatch.setattr(engine_mod, "record_the_first_serve", down_for_three_asks)

    with structlog.testing.capture_logs() as logs:
        await _poll_a_while(state)

    stamped = await _stamp(fake_redis, state)
    assert stamped is not None and stamped != "", "an outage ended the turn's asking"
    assert asked["times"] == 4, "the turn kept asking after the store had answered"
    failed = [e for e in logs if e.get("event") == engine_mod.SERVING_PROOF_WRITE_FAILED_EVENT]
    assert len(failed) == 1


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
            db_session, user.id, project_id=project.id, kind=ChatKind.PLAN
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


# ─── The first route is compiled BEFORE the iframe is told to mount ───────────────────────


class _WarmOrderSandbox(FakeSandboxClient):
    """Records what the frame ring looked like AT THE MOMENT the warm request was made.

    Asserting that both the warm and the frame happened proves nothing about the ordering
    guarantee — the whole unit is that one precedes the other. This is the only shape that can
    go red on a reorder."""

    def __init__(self) -> None:
        super().__init__()
        self.state: _TurnState | None = None
        self.previews_when_warmed: list[int] = []

    async def someone_has_to_go_first(self, handle: SandboxHandle) -> int | None:
        assert self.state is not None
        self.previews_when_warmed.append(len(_ready_frames(self.state)))
        return await super().someone_has_to_go_first(handle)


def _ready_frames(state: _TurnState) -> list[object]:
    return [
        f
        for f in state.ring
        if getattr(f, "type", None) == "preview" and getattr(f, "state", None) == "ready"
    ]


def _unframed_state(client: FakeSandboxClient) -> _TurnState:
    state = _framed_state(client)
    state.preview_framed = False  # nothing on screen yet — the watcher gets to claim the frame
    return state


async def test_the_first_route_is_warmed_before_the_preview_is_framed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ The iframe must never mount onto a route Turbopack has not compiled. Warming at
    the emit chokepoint — rather than where readiness is DISCOVERED — is what makes this hold
    against the watcher's independent 1s poll."""
    monkeypatch.setattr(engine_mod, "READINESS_POLL_S", 0)
    client = _WarmOrderSandbox()
    state = _unframed_state(client)
    client.state = state

    await _poll_a_while(state)

    assert client.warmed, "the platform, not the citizen, pays the first compile"
    assert len(_ready_frames(state)) == 1
    assert client.previews_when_warmed[0] == 0, "warmed BEFORE the frame went out, not after"


async def test_a_failing_warm_request_still_frames_the_preview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ A 500 at the root is a compile error — a real one, which this test exists to catch. The
    frame must go out anyway: a broken app has to LOOK broken, not stay pending forever."""
    monkeypatch.setattr(engine_mod, "READINESS_POLL_S", 0)
    client = _WarmOrderSandbox()
    client.warm_status = 500
    state = _unframed_state(client)
    client.state = state

    await _poll_a_while(state)

    assert len(_ready_frames(state)) == 1, "the frame must never depend on the app being healthy"


class _WarmThatHangs(FakeSandboxClient):
    """A warm request that actually SUSPENDS. The shared fake returns synchronously, which makes
    the cancellation window under test unreachable — there is no await for a cancel to land in."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()

    async def someone_has_to_go_first(self, handle: SandboxHandle) -> int | None:
        self.warmed.append(handle.preview_url)
        self.entered.set()
        await asyncio.sleep(3600)  # the app that never answers; the caller's timeout owns this
        return 200


async def test_a_cancel_inside_the_warm_request_still_frames_the_preview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ THE ONE-SHOT GUARD IS SPENT BEFORE THE AWAIT. A cancellable, up-to-8s warm request sits
    between `claim_preview_frame()` — which fires exactly once per turn — and the emit.
    Cancel in that window and the frame is marked claimed and never sent: no later poll re-claims
    it, so the citizen loses the preview for the WHOLE turn while the app sits there serving.

    `_stop_preview_watcher` cancels this task at every terminal, so this is the ordinary
    end-of-turn shape, not a corner case. Mutation check: unwrap the `try/finally` in
    `_emit_preview_ready` and the ring holds no ready frame."""
    monkeypatch.setattr(engine_mod, "READINESS_POLL_S", 0)
    client = _WarmThatHangs()
    state = _unframed_state(client)

    task = asyncio.create_task(TurnEngine()._watch_preview(state))
    await asyncio.wait_for(client.entered.wait(), timeout=5)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert state.preview_framed, "guard the premise: the one-shot claim was already spent"
    assert len(_ready_frames(state)) == 1, "the frame must survive the cancelled warm request"
    assert state.preview_state == "ready"


async def test_the_watchers_repeated_polls_warm_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_watch_preview` polls forever and sees `ready` on every single pass. `claim_preview_frame`
    is what stops that becoming a warm request per second against a live dev server."""
    monkeypatch.setattr(engine_mod, "READINESS_POLL_S", 0)
    client = _WarmOrderSandbox()
    state = _unframed_state(client)
    client.state = state

    await _poll_a_while(state)

    assert len(client.warmed) == 1


# ─── the frame waits for a PAGE, which `/dev/status.ready` is not ─────────────────────────
#
# ★ THE READING NO TEST DOUBLE IN THIS REPO COULD PRODUCE UNTIL NOW. Every fake's `dev_status`
# answered `root_status=None`, which `shows_a_page` reads as the GRANDFATHER arm and calls a page
# — so the gate below had never once been False in a test, on any path, and reverting it to the
# bare `status.ready` left this whole file green. The fix was unguarded by construction.
#
# WHAT THE GATE IS FOR. `/dev/status.ready` is fail-open by the supervisor's own design: ANY
# answer on the dev port counts, 404 and 500 included, so that a compile error cannot wedge it
# False and mislead the model. A build spends the seconds between the dev server binding and the
# agent writing `app/page.tsx` answering 404s — genuinely ready, with nothing to show. Framing
# that window is how a blank white pane ends up on screen under a live-preview label, measured on
# 2026-09-10, and it arrived over THIS stream while the REST poll was still correctly answering
# STARTING.
#
# AND THE GATE IS AROUND THE CLAIM, NEVER AROUND THE EMIT, which is the difference between a fix
# and a worse defect: `claim_preview_frame` is a once-per-TURN one-shot, so spending it on a
# page-less reading would leave the real first serve — a second later, in this same loop — with
# nothing to frame the app WITH. Every test below asserts the absence AND the arrival, because an
# absence alone false-greens the moment the watcher dies.


class _PhasedStatusSandbox(FakeSandboxClient):
    """A `/dev/status` the test drives phase by phase, so ONE watcher can be asked about the
    reading before a change and the reading after it.

    The single-reading `_ScriptedStatusSandbox` cannot express the claim here: "no frame yet" and
    "the frame arrives later in the SAME turn" are two halves of one assertion, and a fake that
    can only answer one thing forever turns the second half into a different test with a
    different watcher — which is exactly the test that would stay green while the once-per-turn
    claim was being spent on a refusal."""

    def __init__(self, reading: DevStatus) -> None:
        super().__init__()
        self.reading = reading
        self.polls = 0

    async def dev_status(self, handle: SandboxHandle) -> DevStatus:
        self.polls += 1
        return self.reading


def _a_root_answering(status: int | None, *, ready: bool = True) -> DevStatus:
    return DevStatus(running=True, ready=ready, port=3000, root_status=status)


async def test_a_root_with_no_page_is_not_framed_and_the_claim_waits_for_the_real_one(
    fake_redis: aioredis.Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ THE KILLING PAIR. The dev server is up and answering — `ready` is True on every poll —
    and the app root is 404ing because the agent has not written a page yet. The watcher must
    frame nothing, take no proof, and SPEND NOTHING: when the page lands a second later, the
    same watcher in the same turn has to be able to frame it.

    Mutation-check: revert the gate to `if status.ready:` and the first half goes red (a 404 is
    framed and stamped); move the gate around the EMIT instead of the claim and the second half
    goes red (the one-shot was spent on the refusal and the real page never frames)."""
    monkeypatch.setattr(engine_mod, "READINESS_POLL_S", 0)
    client = _PhasedStatusSandbox(_a_root_answering(404))
    state = _unframed_state(client)
    await _the_registry_says(fake_redis, state, serving_since="")

    task = _start_the_watcher(state)
    await _let_it_poll()

    assert client.polls > 1, "guard the premise: the watcher really was polling the 404"
    assert _ready_frames(state) == [], "a container with nothing to show was framed"
    assert state.preview_framed is False, "the once-per-turn claim was spent on a refusal"
    assert await _stamp(fake_redis, state) == "", "a 404 was recorded as a serve"

    client.reading = _a_root_answering(200)  # the agent wrote `app/page.tsx`
    await _let_it_poll()
    await _stop_the_watcher(task)

    assert len(_ready_frames(state)) == 1, "the real first serve had nothing left to frame with"
    assert state.preview_framed is True
    assert await _stamp(fake_redis, state) not in ("", None)


async def test_a_recovered_client_is_not_re_framed_onto_a_root_with_no_page(
    fake_redis: aioredis.Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ THE RECONNECT ARM, and the arm that goes red if someone "simplifies" `reconnecting =
    False` back outside the page gate. After a crash the client is waiting to be told to re-mount
    its iframe, and `reconnecting` is that pending re-frame. Clearing it on a reading that has no
    page drops the re-frame just as permanently as spending the claim does — the app comes back a
    second later and nobody ever tells the browser.

    Mutation-check: dedent `reconnecting = False` out of the `if status.shows_a_page:` block and
    the last assertion goes red — the 500 poll clears the pending re-frame, and the 200 poll that
    follows has nothing left to announce."""
    monkeypatch.setattr(engine_mod, "READINESS_POLL_S", 0)
    client = _PhasedStatusSandbox(DevStatus(running=False, ready=False, port=3000))
    state = _framed_state(client)  # an iframe IS on screen — that is what makes this a re-frame
    await _the_registry_says(fake_redis, state, serving_since="2026-09-10T09:41:04+00:00")

    task = _start_the_watcher(state)
    await _let_it_poll()

    assert len(_reconnecting_frames(state)) == 1, "guard the premise: the crash edge fired"
    assert state.preview_state == "reconnecting"

    # BACK, AND STILL SHOWING NOTHING. A 500 at the root is a compile error mid-recovery: the dev
    # server is answering again, so `ready` is True, and the citizen still has no page.
    client.reading = _a_root_answering(500)
    await _let_it_poll()

    assert _ready_frames(state) == [], "the recovered client was re-mounted over a 500"
    assert state.preview_state == "reconnecting", "the pane stopped saying it was recovering"

    client.reading = _a_root_answering(200)
    await _let_it_poll()
    await _stop_the_watcher(task)

    assert len(_ready_frames(state)) == 1, "the pending re-frame was dropped by the 500 poll"


async def test_a_supervisor_that_cannot_say_what_the_root_answered_still_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ THE ROLLOUT ARM AT THIS CALL SITE. A container built before `root_status` existed
    answers `None`, and `shows_a_page` reads that as TODAY'S BEHAVIOUR on purpose: treating
    "cannot say" as "no page" would refuse to frame every container in the existing fleet — a
    false negative at fleet scale, which is worse than the window it would close.

    `tests/services/sandbox/test_base.py` pins that on the predicate. Nothing pinned that the
    ENGINE honours it, so a later hardening of the gate here would lock the pre-`root_status`
    fleet out of its own preview with a green suite.

    Mutation-check: harden the gate to `status.root_status is not None and status.root_status
    < 400` and this goes red while every 404 test above stays green."""
    monkeypatch.setattr(engine_mod, "READINESS_POLL_S", 0)
    state = _unframed_state(_ScriptedStatusSandbox(running=True, ready=True, root_status=None))

    await _poll_a_while(state)

    assert len(_ready_frames(state)) == 1, "the pre-`root_status` fleet lost its preview"


async def _reached_running_rows() -> list[uuid.UUID | None]:
    """The container-start ratio's numerator, read as columns: `count(...)` owns its own session
    and COMMITS, so these rows escape the test transaction and outlive the reader."""
    async with async_session_factory() as db:
        rows = (
            await db.execute(
                sa.select(HarnessCount.app_id).where(
                    HarnessCount.name == HarnessCounter.APP_START_REACHED_RUNNING.value
                )
            )
        ).all()
    return [app_id for (app_id,) in rows]


async def _wait_for_a_counted_start() -> list[uuid.UUID | None]:
    """Poll until the numerator row lands, then return it.

    NOT IMPATIENCE — CANCELLATION. Unlike every other assertion in this file, this row is written
    through a real database round trip from inside a task the test cancels, and a bare
    `sleep(0)` loop can stop the watcher mid-INSERT and read an empty table that proves nothing.
    Each read here IS a round trip, so the loop gets real work rather than bare ticks. It returns
    whatever it has when the budget runs out, so the ASSERTION reports the failure rather than a
    timeout swallowing it."""
    for _ in range(200):
        rows = await _reached_running_rows()
        if rows:
            return rows
        await asyncio.sleep(0)
    return await _reached_running_rows()


async def test_a_start_is_counted_as_reaching_running_only_once_the_app_shows_a_page(
    fake_redis: aioredis.Redis, monkeypatch: pytest.MonkeyPatch, empty_harness_counts
) -> None:
    """★ THIS NUMERATOR AND `relaunch_preview`'S ARE THE SAME EVENT, and this is what stops them
    drifting apart. `relaunch_preview` refuses its own `ready` for a root that answered without a
    page and withholds the count on exactly that reading; this writer has to mean the same thing
    by the same name, or the ratio is two different measurements added together and nobody can
    say what it is a ratio OF.

    ONE ROW, NOT ONE PER POLL: the count rides the once-per-turn claim, so the watcher seeing
    `ready` on every pass for the rest of the build adds nothing.

    Mutation-check: revert the gate to `if status.ready:` and the first half goes red — a start
    that never showed a page is booked as having reached running."""
    monkeypatch.setattr(engine_mod, "READINESS_POLL_S", 0)
    client = _PhasedStatusSandbox(_a_root_answering(404))
    state = _unframed_state(client)
    # This turn BROUGHT THE CONTAINER UP, which is what puts it in the denominator at all — a
    # turn that joined a container already serving is not a start and never lands in either half.
    state.started_a_container = True
    await _the_registry_says(fake_redis, state, serving_since="")

    task = _start_the_watcher(state)
    await _let_it_poll()

    # THE ABSENCE GETS THE SAME CHANCE THE ARRIVAL GETS, or it says only that a database round
    # trip is slower than fifty event-loop ticks. Each of these reads IS a round trip, so by the
    # last one the loop has had many times over the work one count needs to land.
    for _ in range(5):
        assert await _reached_running_rows() == [], "a page-less container was booked as running"
    assert client.polls > 1, "guard the premise: the watcher really was polling the 404"

    client.reading = _a_root_answering(200)
    counted = await _wait_for_a_counted_start()
    await _stop_the_watcher(task)

    assert state.sandbox is not None
    assert counted == [state.sandbox.app_id], (
        "the real first serve went uncounted, or was counted once per poll"
    )
    assert await _reached_running_rows() == counted, "the count fired again on a later poll"
