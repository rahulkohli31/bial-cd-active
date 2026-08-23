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

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.harness_counter import HarnessCount, HarnessCounter
from src.services.build_sessions import counters as counters_module
from src.services.build_sessions.counters import count


@pytest.fixture(autouse=True)
async def _empty_table() -> None:
    """Clear the table in its OWN session, because `count` writes in one too.

    That is not a test smell, it is the feature: a count is a historical fact about something that
    HAPPENED and must not disappear because the surrounding transaction rolled back. The
    consequence is that these rows escape the test transaction, so each test has to start from a
    known-empty table rather than relying on a rollback that cannot reach them."""
    from src.db.base import async_session_factory

    async with async_session_factory() as db:
        await db.execute(sa.delete(HarnessCount))
        await db.commit()


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
