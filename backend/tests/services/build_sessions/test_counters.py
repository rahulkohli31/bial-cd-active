"""U25 — the outcomes this plan's success criteria name, counted where an operator can read them.

R32. There is no metrics system in this deployment, so an outcome is observable only if the
platform writes it down. After a week in production, "did the verdict block a false claim, how
often did we restore, and did any turn fail to reach a durable copy" has to be answerable from
this table alone — that is the acceptance condition, and it is what these tests pin.

THE TWO THAT MATTER MOST:

* `test_a_counter_that_did_not_exist_at_migration_time_still_writes` — the companion plan emits
  three counters of its own and ships no migration. A counter that needs a schema change to exist
  is a counter that does not get added.
* `test_a_broken_counter_never_fails_the_thing_it_is_counting` — every call site is on a path
  doing something else. This whole plan exists because a platform lied about an app; a metric that
  turns into a second incident is the wrong lesson to draw from it.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import Callable

import pytest
import redis.asyncio as aioredis
import sqlalchemy as sa
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

import src.services.turns.engine as engine_mod
from src.config import settings
from src.db.models.conversation import ChatKind
from src.db.models.harness_counter import HarnessCount, HarnessCounter
from src.services.build_sessions import counters as counters_module
from src.services.build_sessions.counters import count
from src.services.build_sessions.manager import SessionManager
from src.services.orchestrator.deps import SandboxSession
from src.services.sandbox import DevStatus, SandboxHandle
from src.services.sandbox.config import SandboxConfig
from src.services.turns.engine import TurnEngine, _TurnState
from tests.factories import ProjectFactory, UserFactory
from tests.fakes import FakeSandboxClient


@pytest.fixture(autouse=True)
async def _empty_table(empty_harness_counts: None) -> None:
    """Clear the table in its OWN session, because `count` writes in one too.

    That is not a test smell, it is the feature: a count is a historical fact about something that
    HAPPENED and must not disappear because the surrounding transaction rolled back. The
    consequence is that these rows escape the test transaction, so each test has to start from a
    known-empty table rather than relying on a rollback that cannot reach them. The clearing
    itself lives in `tests/conftest.py`, shared with the two suites that read these rows back."""


APP = uuid.UUID("0198f2c0-3333-7000-8000-00000000c007")
BUILD = uuid.UUID("0198f2c0-4444-7000-8000-00000000b111")


async def _rows(db: AsyncSession, name: str) -> list[HarnessCount]:
    return list(
        (await db.execute(sa.select(HarnessCount).where(HarnessCount.name == name)))
        .scalars()
        .all()
    )


async def test_each_counter_increments_on_its_own_event_and_no_other(
    db_session: AsyncSession,
) -> None:
    """A counter that fires on two different things measures neither."""
    await count(HarnessCounter.CLAIM_BLOCKED, app_id=APP)
    await count(HarnessCounter.RESTORE_PERFORMED, app_id=APP)

    assert len(await _rows(db_session, HarnessCounter.CLAIM_BLOCKED.value)) == 1
    assert len(await _rows(db_session, HarnessCounter.RESTORE_PERFORMED.value)) == 1
    assert await _rows(db_session, HarnessCounter.RECOVERY_WRITE_MISSED.value) == []


async def test_a_counter_that_did_not_exist_at_migration_time_still_writes(
    db_session: AsyncSession,
) -> None:
    """★ THE PROPERTY THE SHAPE EXISTS FOR. The companion plan emits three adoption counters at
    the tool boundary and ships no migration of its own; with a column per counter, each of those
    would need one, and a counter that needs a schema change is a counter that does not get added.

    Mutation check: give the table a column per counter and this cannot be written at all."""
    await count("some_counter_invented_next_quarter", value=17, app_id=APP)

    rows = await _rows(db_session, "some_counter_invented_next_quarter")
    assert len(rows) == 1
    assert rows[0].value == 17


async def test_the_per_build_token_counter_reads_as_one_number(db_session: AsyncSession) -> None:
    """★ R32 asks for "a counter to watch", and a number that takes a join and a judgement call to
    read is not one — it will not be watched. One query, one value, for one build."""
    await count(HarnessCounter.BUILD_TOKENS, value=1200, build_id=BUILD)
    await count(HarnessCounter.BUILD_TOKENS, value=800, build_id=BUILD)
    await count(HarnessCounter.BUILD_TOKENS, value=9999, build_id=uuid.uuid4())

    total = await db_session.scalar(
        sa.select(sa.func.sum(HarnessCount.value)).where(
            HarnessCount.name == HarnessCounter.BUILD_TOKENS.value,
            HarnessCount.build_id == BUILD,
        )
    )

    assert total == 2000


async def test_the_served_page_head_is_stored_beside_the_verdict_it_explains(
    db_session: AsyncSession,
) -> None:
    """An operator asking "why was this claim blocked" wants the page the platform actually
    loaded. It arrives already scrubbed and capped at the container boundary — the raw bytes never
    reach here, because a served page can carry a credential in a query string."""
    await count(
        HarnessCounter.CLAIM_BLOCKED, app_id=APP, served_head="<!DOCTYPE html><h1>Template</h1>"
    )

    rows = await _rows(db_session, HarnessCounter.CLAIM_BLOCKED.value)
    assert rows[0].served_head == "<!DOCTYPE html><h1>Template</h1>"


async def test_a_broken_counter_never_fails_the_thing_it_is_counting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ Every call site is on a path doing something else — finishing a turn, refusing a claim,
    restoring a workspace. This whole plan exists because a platform lied about an app; a metric
    that turns into a second incident is the wrong lesson to draw from it.

    Mutation check: narrow the `except` to a database error and this goes red, because the thing
    that breaks in production is rarely the exception you predicted."""

    def explode() -> None:
        raise RuntimeError("the session factory itself is broken")

    monkeypatch.setattr(counters_module, "async_session_factory", explode)

    await count(HarnessCounter.CLAIM_BLOCKED, app_id=APP)  # must not raise


async def test_a_count_outlives_its_app(db_session: AsyncSession) -> None:
    """NO FOREIGN KEY on `app_id`, deliberately: a count is a historical fact, and the moment an
    operator most wants to read it back is after the app is gone."""
    await count(HarnessCounter.RECOVERY_WRITE_MISSED, app_id=uuid.uuid4())

    rows = await _rows(db_session, HarnessCounter.RECOVERY_WRITE_MISSED.value)
    assert len(rows) == 1


def test_every_counter_name_is_distinct() -> None:
    """Two members sharing a value would silently merge two different questions into one number."""
    values = [member.value for member in HarnessCounter]
    assert len(values) == len(set(values))


# --- U15 / R103: the turn's own attach, the seam the explicit start control cannot see ------
#
# `relaunch_preview` counts one way a container comes up; a turn counts the other, and the two
# never fire on the same path (the relaunch route is `relaunch_preview`'s only caller). These
# scenarios pin the turn side. The relaunch side keeps its own suite in
# `tests/api/v1/build_sessions/test_relaunch.py`, and this unit changed nothing there.


def _state_watching(client: FakeSandboxClient, *, started_a_container: bool) -> _TurnState:
    """A turn state parked exactly where `_attach_sandbox` leaves one: container held, preview
    not yet framed, watcher about to start."""
    state = _TurnState(
        turn_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        kind=ChatKind.BUILD,
    )
    fqdn = "sbx-count.westeurope.azurecontainerapps.io"
    state.sandbox = SandboxSession(
        sandbox_client=client,
        handle=SandboxHandle(
            fqdn=fqdn,
            token="tok-test",  # noqa: S106 - a fake, never a real bearer
            app_name="sbx-count",
            preview_url=f"https://{fqdn}/",
            ready=False,
        ),
        app_id=APP,
    )
    state.started_a_container = started_a_container
    return state


class _ServesAfter(FakeSandboxClient):
    """Not ready for `negative_polls` polls, then serving — a normal cold render."""

    def __init__(self, *, negative_polls: int = 1) -> None:
        super().__init__()
        self.negative_polls = negative_polls
        self.polls = 0

    async def dev_status(self, handle: SandboxHandle) -> DevStatus:
        self.polls += 1
        return DevStatus(running=True, ready=self.polls > self.negative_polls, port=3000)


async def _watch_until(
    state: _TurnState, done: Callable[[], bool], *, budget_s: float = 10
) -> None:
    """Run the real watcher until `done()`, then stop it.

    BOUNDED BY WALL CLOCK, NOT BY A COUNT OF EVENT-LOOP TURNS, and the difference is the whole
    reason this helper exists. These states start UNFRAMED, so the first served poll really
    runs `_emit_preview_ready` and the counter write, both of which do database I/O — and how
    many `sleep(0)` turns that takes depends on what else is running. A counted spin passes on
    an idle machine and stops the watcher mid-emit under a loaded suite, which is a test that
    reports scheduling luck.

    The budget is a deadlock guard, never the thing being measured: every caller asserts on the
    poll count afterwards, so a watcher that stopped early fails loudly instead of reading as a
    pass."""
    task = asyncio.create_task(TurnEngine()._watch_preview(state))
    deadline = time.monotonic() + budget_s
    while not done() and time.monotonic() < deadline:
        await asyncio.sleep(0)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.fixture
def _sandbox_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """`ensure_sandbox` builds the app env, which fails closed when no sandbox is configured."""
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


async def test_a_turn_that_brings_a_container_up_is_one_attempt_that_reached(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage,
    _sandbox_configured: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ THE HAPPY PATH R103 IS ABOUT — a container that comes up on the way to answering.

    One attempt row and one reached row, both carrying the app id so Plan E can read the ratio
    back per app, and NO duration row: the turn's two start arms have different budgets and a
    mean over both would describe neither (R102 is the explicit control's question).

    Mutation check: drop the `not session.attached` guard on the attempt emit and the joining
    turn in the next test files a second attempt; drop the `started_a_container` guard on the
    numerator and that same turn reports a start it never made."""
    monkeypatch.setattr(engine_mod, "READINESS_POLL_S", 0)
    user = await UserFactory.create(db_session, email="u15a@rvaiglobal.com")
    project = await ProjectFactory.create(db_session, user.id)
    manager, client = SessionManager(), FakeSandboxClient()

    session = await manager.ensure_sandbox(
        db_session, user, project.id, sandbox_client=client, may_write=True
    )
    # The discriminator the emit is gated on: a fresh provision BROUGHT ONE UP.
    assert session.attached is False
    await count(HarnessCounter.APP_START_ATTEMPTED, app_id=session.app_id)

    watched = _ServesAfter()
    state = _state_watching(watched, started_a_container=True)
    # PAST the count, not merely up to the frame: the count sits after the frame now, so a
    # predicate that stops at "ready" would stop before the row this test is about was written.
    await _watch_until(state, lambda: state.preview_framed and watched.polls > 8)
    assert watched.polls > 8, "the watcher never got past the count — the test proves nothing"

    attempted = await _rows(db_session, HarnessCounter.APP_START_ATTEMPTED)
    assert [r.value for r in attempted] == [1]
    assert [r.app_id for r in attempted] == [session.app_id]
    reached = await _rows(db_session, HarnessCounter.APP_START_REACHED_RUNNING)
    assert [r.value for r in reached] == [1]
    assert [r.app_id for r in reached] == [APP]
    assert await _rows(db_session, HarnessCounter.APP_COLD_START_MS) == []


async def test_a_turn_that_joins_a_container_already_serving_started_nothing(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage,
    _sandbox_configured: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ THE ARM THAT WOULD HAVE RUINED THE RATIO. `_attach_sandbox` runs on EVERY turn of
    EVERY kind, and on most of them the container is already up — the second message in a chat,
    and every message after it. Those turns start nothing.

    Counting them would make the denominator "turns" rather than "starts" and hand R103 a ratio
    near 1 that means nothing, which is the failure this gate exists to prevent. It is also the
    plan's third outcome arriving by a different mechanism than expected: not a turn joining an
    in-flight start, but one joining a start that already finished. Either way it is neither an
    attempt nor a success, and it is excluded from both."""
    monkeypatch.setattr(engine_mod, "READINESS_POLL_S", 0)
    user = await UserFactory.create(db_session, email="u15b@rvaiglobal.com")
    project = await ProjectFactory.create(db_session, user.id)
    manager, client = SessionManager(), FakeSandboxClient()

    first = await manager.ensure_sandbox(
        db_session, user, project.id, sandbox_client=client, may_write=True
    )
    await manager.finish_turn_sandbox(first, client, touched=True)
    client.attach_handle = first.handle  # the live container is attachable, as in production
    second = await manager.ensure_sandbox(
        db_session, user, project.id, sandbox_client=client, may_write=True
    )

    assert second.attached is True  # nothing was brought up

    watched = _ServesAfter()
    state = _state_watching(watched, started_a_container=False)
    # Past the point where a count WOULD have been written, or this proves only that the loop
    # had not got there yet.
    await _watch_until(state, lambda: state.preview_framed and watched.polls > 8)
    assert watched.polls > 8

    # It still frames the preview for the citizen — the app IS serving. It just is not a start.
    assert state.preview_framed is True
    assert await _rows(db_session, HarnessCounter.APP_START_REACHED_RUNNING) == []


async def test_a_container_that_never_serves_is_an_attempt_that_did_not_reach(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ THE GAP THE COUNTER EXISTS TO EXPOSE, and the reason the numerator is not the handle's
    own `ready` flag.

    A framable URL is not a running app. `SandboxHandle.ready` is hard-coded False on both birth
    arms and, on the attach arm, is a `/dev/status` snapshot taken BEFORE the turn's own
    `dev_start` — so reading it here would report a near-zero success rate and make R103 measure
    the container's birth rather than the app. What the turn actually learns is the watcher's
    first SERVED poll, and a container that never serves one produces no numerator row at all.

    Mutation check: count on `state.sandbox.handle.ready` instead and this stays green while the
    happy-path test above goes red — which is why both exist."""
    monkeypatch.setattr(engine_mod, "READINESS_POLL_S", 0)
    never_serves = _ServesAfter(negative_polls=10_000)
    state = _state_watching(never_serves, started_a_container=True)

    await _watch_until(state, lambda: never_serves.polls > 20)

    assert never_serves.polls > 20, "the watcher never polled — the test proves nothing"
    assert state.preview_framed is False
    assert await _rows(db_session, HarnessCounter.APP_START_REACHED_RUNNING) == []


async def test_a_crash_and_recovery_is_not_a_second_start(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One emit per event, on a path where the obvious placement gives several.

    `_emit_preview_ready` has TWO callers — this watcher and the self-heal verify — and this
    watcher calls it again on every crash RECOVERY. Counting inside the emitter would file a
    fresh "reached running" row every time a long build's dev server flapped. The count is on
    `claim_preview_frame()`, the synchronous once-per-turn one-shot, so it is once by
    construction rather than by everyone remembering.

    Mutation check: move the count inside `_emit_preview_ready` and this goes red."""
    monkeypatch.setattr(engine_mod, "READINESS_POLL_S", 0)

    class _Flapping(FakeSandboxClient):
        """Serves, dies for a stretch long enough to trip the crash edge, then serves again."""

        def __init__(self) -> None:
            super().__init__()
            self.polls = 0

        async def dev_status(self, handle: SandboxHandle) -> DevStatus:
            self.polls += 1
            alive = not 3 <= self.polls <= 12
            return DevStatus(running=alive, ready=alive, port=3000)

    client = _Flapping()
    state = _state_watching(client, started_a_container=True)

    await _watch_until(state, lambda: client.polls > 14 and state.preview_state == "ready")

    assert client.polls > 14, "the flap never completed — the test proves nothing"
    assert state.preview_state == "ready", "the recovery never re-framed — nothing was re-emitted"
    reached = await _rows(db_session, HarnessCounter.APP_START_REACHED_RUNNING)
    assert [r.value for r in reached] == [1]


async def test_a_broken_counter_does_not_stop_the_preview_appearing(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The counter must never fail the thing it is counting, asserted at the NEW call site.

    `count` swallows everything by construction and there is a general test for that above; this
    one is about the consequence at THIS seam, which is a long-lived background loop. The count
    sits after the preview frame, so a raising counter could not cost the citizen their app on
    screen — what it would cost is everything the watcher does AFTERWARDS: the crash detection
    and the reconnect frame for the rest of the turn. So the assertion is that the loop is still
    polling well past the count, not merely that the frame got out before it.

    Mutation check: `raise` instead of swallowing inside `count` and the poll count stops dead
    at the first served poll."""
    monkeypatch.setattr(engine_mod, "READINESS_POLL_S", 0)

    def _explode(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("the counter table is gone")

    monkeypatch.setattr(counters_module, "async_session_factory", _explode)
    watched = _ServesAfter()
    state = _state_watching(watched, started_a_container=True)

    await _watch_until(state, lambda: state.preview_framed and watched.polls > 20)

    assert state.preview_state == "ready"
    assert watched.polls > 20, "the watcher stopped at the counter — the loop did not survive it"
    assert await _rows(db_session, HarnessCounter.APP_START_REACHED_RUNNING) == []
