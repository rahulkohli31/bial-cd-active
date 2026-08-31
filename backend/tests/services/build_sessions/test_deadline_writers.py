"""U13 — every keep-alive deadline has a NAMED writer and a stated precedence (R12–R15).

Before this, a sandbox stayed up because *something* renewed *something*, and no operator could
say what. That is not a metaphor for the origin incident, it IS the origin incident: containers
outlived every human who might have stopped them, and nobody could name the thing holding them
open.

What this file pins:

* the closed writer set, and that registration alone is not on it;
* monotonic extension — a weaker writer arriving later cannot SHORTEN a stronger one's reprieve;
* provenance recorded beside the deadline, so "what is holding this open?" has an answer;
* the negative space R13 cares most about: an open tab, a framed preview and a held-open
  connection extend NOTHING. Those are proved on the browser side
  (`portal/src/hooks/__tests__/useBuildSession.test.ts`), because the loop that made an open tab
  a writer lived there and the only honest way to prove it is gone is that it makes no calls.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import redis.asyncio as aioredis

from src.api.v1.build_sessions.schemas import (
    RELAUNCH_PREVIEW_STAY_SECONDS,
    SERVED_TRAFFIC_STAY_SECONDS,
    TURN_ENDED_UNCHANGED_STAY_SECONDS,
    BuildSessionStatus,
)
from src.services.build_sessions import locks
from src.services.build_sessions.locks import DeadlineWriter, grant_stay_of_execution
from src.services.build_sessions.manager import BuildSession, SessionManager
from src.services.build_sessions.reaper import reconcile_user
from src.services.redis import registry_key
from src.services.redis.keys import (
    REGISTRY_FIELD_APP_NAME,
    REGISTRY_FIELD_PREVIEW_STAY_UNTIL,
    REGISTRY_FIELD_STAY_WRITER,
)
from src.services.sandbox import SandboxHandle
from tests.fakes import FakeSandboxClient, a_sandbox_name

USER = uuid.uuid4()


async def _register_as(redis: aioredis.Redis, user_id: uuid.UUID) -> None:
    await redis.hset(registry_key(user_id), mapping={REGISTRY_FIELD_APP_NAME: "sbx-x"})


async def _register(redis: aioredis.Redis) -> None:
    await _register_as(redis, USER)


def _text(value: bytes | str | None) -> str | None:
    """`decode_responses=True` hands back `str`, but the stub's union still admits `bytes`."""
    if value is None:
        return None
    return value.decode() if isinstance(value, bytes) else str(value)


async def _stay_for(
    redis: aioredis.Redis, user_id: uuid.UUID
) -> tuple[datetime | None, str | None]:
    reg = await redis.hgetall(registry_key(user_id))
    raw = _text(reg.get(REGISTRY_FIELD_PREVIEW_STAY_UNTIL))
    return (
        datetime.fromisoformat(raw) if raw else None,
        _text(reg.get(REGISTRY_FIELD_STAY_WRITER)),
    )


async def _stay(redis: aioredis.Redis) -> tuple[datetime | None, str | None]:
    return await _stay_for(redis, USER)


# --- the writer set is closed, and named ------------------------------------------


def test_the_writer_set_is_exactly_four() -> None:
    """A CLOSED SET is the requirement, not a side effect. Adding a way to keep a container alive
    should be a deliberate act with a review attached — the failure this unit exists to prevent
    is a deadline nobody can attribute. U12/R100 adds the fourth member, `turn_ended_unchanged`,
    for a turn that held the workspace but wrote nothing to it — still a reviewed member of the
    closed set, not an anonymous extension."""
    assert {w.value for w in DeadlineWriter} == {
        "turn_in_flight",
        "app_served_traffic",
        "builder_acted",
        "turn_ended_unchanged",
    }


def test_a_turn_that_changed_nothing_buys_the_least_of_the_four() -> None:
    """R100's whole point, stated as an ordering rather than a single number: the weakest
    evidence — a turn that pinned the workspace and produced nothing — buys the shortest
    reprieve of any writer in the set, strictly less than either bounded-but-real-activity
    writer above it."""
    assert TURN_ENDED_UNCHANGED_STAY_SECONDS < SERVED_TRAFFIC_STAY_SECONDS
    assert TURN_ENDED_UNCHANGED_STAY_SECONDS < RELAUNCH_PREVIEW_STAY_SECONDS


async def test_a_grant_records_which_writer_made_it(fake_redis: aioredis.Redis) -> None:
    """Provenance, not control flow — nothing branches on this. It exists so an operator staring
    at a container that refuses to lapse can answer "what is holding this open?" without reading
    four call sites."""
    await _register(fake_redis)

    await grant_stay_of_execution(fake_redis, USER, writer=DeadlineWriter.APP_SERVED_TRAFFIC)

    _, writer = await _stay(fake_redis)
    assert writer == "app_served_traffic"


async def test_each_writer_gets_its_own_ttl_from_its_identity(fake_redis: aioredis.Redis) -> None:
    """The TTL comes from WHO is asking, not from the call site. Served traffic buys less than a
    deliberate action because it is weaker evidence of intent: a left-open app tab polling in the
    background is still traffic, and nobody is working."""
    await _register(fake_redis)
    before = datetime.now(UTC)

    await grant_stay_of_execution(fake_redis, USER, writer=DeadlineWriter.APP_SERVED_TRAFFIC)
    traffic, _ = await _stay(fake_redis)
    assert traffic is not None
    assert traffic - before <= timedelta(seconds=SERVED_TRAFFIC_STAY_SECONDS + 5)
    assert SERVED_TRAFFIC_STAY_SECONDS < RELAUNCH_PREVIEW_STAY_SECONDS


# --- the deadline never moves backward --------------------------------------------


async def test_a_weaker_writer_cannot_shorten_a_stronger_ones_reprieve(
    fake_redis: aioredis.Redis,
) -> None:
    """THE PRECEDENCE RULE, and it needs no lock to be correct because it is monotonic.

    A builder presses Save (thirty minutes). Fifteen seconds later their app serves a background
    poll (fifteen minutes). Without this, the poll would truncate the Save's reprieve by half an
    hour — a container reclaimed out from under someone who had just acted, because their app was
    *also* being used. Extension only ever moves the deadline forward.

    Mutation-check: drop the `standing >= deadline` guard and this goes red."""
    await _register(fake_redis)
    saved = await grant_stay_of_execution(fake_redis, USER, writer=DeadlineWriter.BUILDER_ACTED)

    await grant_stay_of_execution(fake_redis, USER, writer=DeadlineWriter.APP_SERVED_TRAFFIC)

    standing, writer = await _stay(fake_redis)
    assert standing == saved
    # ...and the provenance still names the writer that actually bought the time.
    assert writer == "builder_acted"


async def test_a_later_grant_that_buys_more_time_does_move_the_deadline(
    fake_redis: aioredis.Redis,
) -> None:
    """Monotonic must not mean frozen: continued use has to keep extending, or a builder working
    for an hour loses the container at minute thirty."""
    await _register(fake_redis)
    first = await grant_stay_of_execution(
        fake_redis, USER, writer=DeadlineWriter.APP_SERVED_TRAFFIC
    )

    second = await grant_stay_of_execution(fake_redis, USER, writer=DeadlineWriter.BUILDER_ACTED)

    standing, writer = await _stay(fake_redis)
    assert second > first
    assert standing == second
    assert writer == "builder_acted"


async def test_an_unreadable_standing_stay_does_not_block_a_fresh_grant(
    fake_redis: aioredis.Redis,
) -> None:
    """The OPPOSITE fail direction from `stay_of_execution_is_current`, and deliberately so.

    There, an unparseable value must not SPARE a container — it fails closed. Here it must not
    BLOCK an extension, or a corrupt field would strand a live preview with no way to renew it.
    Same value, two readers, two correct-but-opposite defaults."""
    await _register(fake_redis)
    await fake_redis.hset(registry_key(USER), REGISTRY_FIELD_PREVIEW_STAY_UNTIL, "not-a-timestamp")

    granted = await grant_stay_of_execution(fake_redis, USER, writer=DeadlineWriter.BUILDER_ACTED)

    standing, _ = await _stay(fake_redis)
    assert standing == granted


# --- a deadline needs a container --------------------------------------------------


async def test_a_grant_without_a_registry_is_loud_and_writes_nothing(
    fake_redis: aioredis.Redis,
) -> None:
    """Guarded exactly like `mark_registry_ending`: never conjure a partial registry hash for a
    user who has no sandbox. The skip is LOUD because the caller discards the return, so silence
    would make "no registry, no lease" indistinguishable from success."""
    await grant_stay_of_execution(fake_redis, USER, writer=DeadlineWriter.BUILDER_ACTED)

    assert await fake_redis.exists(registry_key(USER)) == 0


@pytest.mark.parametrize("writer", list(DeadlineWriter))
async def test_no_writer_can_buy_more_than_its_own_ceiling(
    fake_redis: aioredis.Redis, writer: DeadlineWriter
) -> None:
    """`stay_of_execution_is_current` refuses a deadline further out than this module could ever
    have granted, so a writer that could exceed the ceiling would write a stay that reads as
    absurd and spares nothing — protection that returns True and buys zero."""
    await _register(fake_redis)

    await grant_stay_of_execution(fake_redis, USER, writer=writer)

    assert await locks.stay_of_execution_is_current(fake_redis, USER) is True


# --- U12/R100: `_pardon_the_container` picks the writer from what the turn DID -----
# `_pardon_the_container` is the one place `finish_turn_sandbox` (an ordinary chat turn's end)
# and `_do_finalize` (a completed build's end) hand a container its keep-alive stay. These
# tests drive it directly — no HTTP layer, no database, no sandbox client beyond the fake C2
# stub the sweep needs — because the fact under test is entirely a Redis-visible one: which
# writer, and which deadline, `grant_stay_of_execution` ends up recording.


def _pardoned_session(*, user_id: uuid.UUID) -> BuildSession:
    """A minimal `BuildSession` — only `user_id` and `lock_token` are read by
    `_pardon_the_container`; the rest of the dataclass exists for other callers and is filled
    with harmless placeholders so the type stays honest about what a real session carries."""
    return BuildSession(
        session_id=uuid.uuid7(),
        user_id=user_id,
        project_id=uuid.uuid4(),
        app_id=uuid.uuid4(),
        prompt="",
        lock_token="tok",
        handle=SandboxHandle(
            fqdn="x.example",
            token="t",
            app_name=a_sandbox_name("x"),
            preview_url="https://x.example/",
            ready=True,
        ),
    )


async def test_a_turn_that_wrote_files_grants_the_long_stay_under_the_existing_writer(
    fake_redis: aioredis.Redis,
) -> None:
    """Happy path. `touched=True` changes NOTHING about today's behaviour — the same writer,
    the same TTL a build or a writing turn has always earned."""
    user_id = uuid.uuid4()
    await _register_as(fake_redis, user_id)
    manager = SessionManager()
    session = _pardoned_session(user_id=user_id)

    await manager._pardon_the_container(fake_redis, session, touched=True)

    deadline, writer = await _stay_for(fake_redis, user_id)
    assert writer == DeadlineWriter.TURN_IN_FLIGHT.value
    assert deadline is not None
    assert deadline - datetime.now(UTC) <= timedelta(seconds=RELAUNCH_PREVIEW_STAY_SECONDS + 5)
    assert deadline - datetime.now(UTC) > timedelta(seconds=TURN_ENDED_UNCHANGED_STAY_SECONDS)


async def test_a_turn_that_wrote_nothing_against_a_fresh_container_grants_the_short_stay(
    fake_redis: aioredis.Redis,
) -> None:
    """Happy path, and the qualifier in its own name is load-bearing (see the next test): this
    is a container with NO STANDING STAY, so the short stay is what actually lands and the
    registry records the NEW writer as the reason — an operator asking "what is holding this
    open?" gets `turn_ended_unchanged`, not a guess."""
    user_id = uuid.uuid4()
    await _register_as(fake_redis, user_id)
    manager = SessionManager()
    session = _pardoned_session(user_id=user_id)

    await manager._pardon_the_container(fake_redis, session, touched=False)

    deadline, writer = await _stay_for(fake_redis, user_id)
    assert writer == DeadlineWriter.TURN_ENDED_UNCHANGED.value
    assert deadline is not None
    assert deadline - datetime.now(UTC) <= timedelta(seconds=TURN_ENDED_UNCHANGED_STAY_SECONDS + 5)


async def test_a_read_only_turn_inside_a_write_turns_stay_leaves_it_untouched(
    fake_redis: aioredis.Redis,
) -> None:
    """Edge case, and the one the monotonic guarantee exists for. A write turn ends and stamps
    the long stay; a read-only turn ends moments later, INSIDE that stay. R100 must not mean a
    weaker writer can shorten a stronger one's reprieve — `grant_stay_of_execution`'s own
    `max(existing, computed)` is what this pins, applied through the exact call
    `_pardon_the_container` makes rather than assumed."""
    user_id = uuid.uuid4()
    await _register_as(fake_redis, user_id)
    manager = SessionManager()
    write_session = _pardoned_session(user_id=user_id)
    await manager._pardon_the_container(fake_redis, write_session, touched=True)
    long_deadline, _ = await _stay_for(fake_redis, user_id)

    read_only_session = _pardoned_session(user_id=user_id)
    await manager._pardon_the_container(fake_redis, read_only_session, touched=False)

    deadline, writer = await _stay_for(fake_redis, user_id)
    assert deadline == long_deadline, "the longer deadline must not be truncated"
    assert writer == DeadlineWriter.TURN_IN_FLIGHT.value, "provenance still names who bought it"


async def test_a_turn_that_failed_after_writing_files_still_grants_the_long_stay(
    fake_redis: aioredis.Redis,
) -> None:
    """Edge case: WHAT was done, not how the turn ended. `_pardon_the_container` reads only
    `touched` — a session left in a FAILED-looking state that nonetheless wrote to the tree
    still earns the long stay, because the citizen still has something on screen worth keeping
    the container alive to look at."""
    user_id = uuid.uuid4()
    await _register_as(fake_redis, user_id)
    manager = SessionManager()
    session = _pardoned_session(user_id=user_id)
    session.status = BuildSessionStatus.FAILED  # the turn did not end cleanly...

    await manager._pardon_the_container(fake_redis, session, touched=True)  # ...but it wrote.

    _, writer = await _stay_for(fake_redis, user_id)
    assert writer == DeadlineWriter.TURN_IN_FLIGHT.value


async def test_the_sweep_spares_a_container_inside_the_short_stay_and_reaps_through_it_after(
    fake_redis: aioredis.Redis,
) -> None:
    """Integration: the short stay is honoured by the SAME sweep predicate as any other writer's
    — no special-casing for the new member — and once it lapses the container is reaped exactly
    as an expired `builder_acted`/`app_served_traffic` stay already is. Nothing else about the
    sweep changes; this pins that `TURN_ENDED_UNCHANGED` is a value in an existing mechanism,
    not a second one."""
    user_id = uuid.uuid4()
    app_name = a_sandbox_name("unchanged")
    await fake_redis.hset(registry_key(user_id), REGISTRY_FIELD_APP_NAME, app_name)
    manager = SessionManager()
    session = _pardoned_session(user_id=user_id)
    sandbox = FakeSandboxClient()

    await manager._pardon_the_container(fake_redis, session, touched=False)

    # Inside the short stay: the background sweep (`honor_stay=True`) spares it. No lock, no
    # heartbeat and no lease are held after a pardon (that is the whole point of the pardon —
    # see `_pardon_the_container`'s docstring), so the stay is the ONLY thing standing between
    # this container and the sweep.
    reaped = await reconcile_user(
        fake_redis, user_id, sandbox, has_live_session=False, honor_stay=True
    )
    assert reaped is False
    assert sandbox.torn_down == []
    assert await fake_redis.exists(registry_key(user_id)) == 1

    # Past it: the same predicate the sweep already trusted for every other writer now reads
    # the deadline as lapsed, and the sweep reaps through it exactly as it would a lapsed
    # `builder_acted` stay — a fact written directly, since waiting out 300 real seconds is not
    # what this test is about.
    lapsed = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    await fake_redis.hset(registry_key(user_id), REGISTRY_FIELD_PREVIEW_STAY_UNTIL, lapsed)

    reaped_after = await reconcile_user(
        fake_redis, user_id, sandbox, has_live_session=False, honor_stay=True
    )
    assert reaped_after is True
    assert sandbox.torn_down == [app_name]
    assert await fake_redis.exists(registry_key(user_id)) == 0
