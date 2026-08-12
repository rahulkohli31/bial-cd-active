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
)
from src.services.build_sessions import locks
from src.services.build_sessions.locks import DeadlineWriter, grant_stay_of_execution
from src.services.redis import registry_key
from src.services.redis.keys import (
    REGISTRY_FIELD_APP_NAME,
    REGISTRY_FIELD_PREVIEW_STAY_UNTIL,
    REGISTRY_FIELD_STAY_WRITER,
)

USER = uuid.uuid4()


async def _register(redis: aioredis.Redis) -> None:
    await redis.hset(registry_key(USER), mapping={REGISTRY_FIELD_APP_NAME: "sbx-x"})


def _text(value: bytes | str | None) -> str | None:
    """`decode_responses=True` hands back `str`, but the stub's union still admits `bytes`."""
    if value is None:
        return None
    return value.decode() if isinstance(value, bytes) else str(value)


async def _stay(redis: aioredis.Redis) -> tuple[datetime | None, str | None]:
    reg = await redis.hgetall(registry_key(USER))
    raw = _text(reg.get(REGISTRY_FIELD_PREVIEW_STAY_UNTIL))
    return (
        datetime.fromisoformat(raw) if raw else None,
        _text(reg.get(REGISTRY_FIELD_STAY_WRITER)),
    )


# --- the writer set is closed, and named ------------------------------------------


def test_the_writer_set_is_exactly_three() -> None:
    """A CLOSED SET is the requirement, not a side effect. Adding a fourth way to keep a container
    alive should be a deliberate act with a review attached — the failure this unit exists to
    prevent is a deadline nobody can attribute."""
    assert {w.value for w in DeadlineWriter} == {
        "turn_in_flight",
        "app_served_traffic",
        "builder_acted",
    }


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
