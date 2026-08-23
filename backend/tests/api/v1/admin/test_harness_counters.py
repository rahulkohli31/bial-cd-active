"""The build-harness counters, read by an operator (U25, R32).

WHAT THIS ROUTE IS FOR, in the words of the plan's success criteria: after a week in production,
"did the verdict block a false claim, how often did we restore, and did any turn fail to reach a
durable copy" has to be answerable. There is no metrics system in this deployment, so if these are
not readable here they are not readable anywhere.

THE GATE IS THE OTHER HALF, and it is not decoration. The gate in `api/v1/admin/` is OPT-IN PER
ROUTE, so a missing dependency ships a citizen-readable endpoint leaking aggregate operational and
usage data across every user in the tenant — silently, because the route works perfectly.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.db.models.harness_counter import HarnessCount, HarnessCounter
from src.services.auth.session_jwt import mint_session_jwt
from tests.factories import UserFactory

_TTL = settings.auth.access_ttl_seconds
_ROUTE = "/v1/admin/harness-counters"


def _cookie(jwt: str) -> dict[str, str]:
    return {"Cookie": f"session={jwt}"}


async def _admin(db: AsyncSession) -> dict[str, str]:
    user = await UserFactory.create(db, email="admin@bial.com")
    return _cookie(mint_session_jwt(user.id, user.token_version, _TTL))


async def _citizen(db: AsyncSession) -> dict[str, str]:
    user = await UserFactory.create(db, email="nobody@rvaiglobal.com")
    return _cookie(mint_session_jwt(user.id, user.token_version, _TTL))


@pytest.fixture(autouse=True)
async def _empty_table(db_session: AsyncSession) -> None:
    await db_session.execute(sa.delete(HarnessCount))
    await db_session.commit()


async def _seed(db: AsyncSession, name: str, value: int, *, age_days: int = 0) -> None:
    db.add(
        HarnessCount(
            name=name,
            value=value,
            occurred_at=datetime.now(UTC) - timedelta(days=age_days),
            app_id=uuid.uuid4(),
        )
    )
    await db.commit()


async def test_a_citizen_cannot_read_the_operational_counters(client, db_session) -> None:
    """★ THE GATE IS OPT-IN PER ROUTE in this package, so its absence is invisible: the route
    works, and it hands aggregate operational and usage data across every user in the tenant to
    anyone signed in."""
    resp = await client.get(_ROUTE, headers=await _citizen(db_session))

    assert resp.status_code == 403
    # LIVENESS: the route exists and answers, so the 403 is the gate rather than a typo'd path.
    assert (await client.get(_ROUTE, headers=await _admin(db_session))).status_code == 200


async def test_each_counter_totals_separately(client, db_session) -> None:
    headers = await _admin(db_session)
    await _seed(db_session, HarnessCounter.CLAIM_BLOCKED.value, 1)
    await _seed(db_session, HarnessCounter.CLAIM_BLOCKED.value, 1)
    await _seed(db_session, HarnessCounter.RESTORE_PERFORMED.value, 1)

    body = (await client.get(_ROUTE, headers=headers)).json()

    totals = {row["name"]: row["total"] for row in body["counters"]}
    assert totals[HarnessCounter.CLAIM_BLOCKED.value] == 2
    assert totals[HarnessCounter.RESTORE_PERFORMED.value] == 1


async def test_a_counter_nobody_declared_still_shows_up(client, db_session) -> None:
    """★ The vocabulary is open by design — the companion plan adds three of its own at the tool
    boundary — so this route reports what has been WRITTEN, not what the enum happens to list.

    Mutation check: iterate `HarnessCounter` instead of the rows and this goes red."""
    headers = await _admin(db_session)
    await _seed(db_session, "a_counter_from_the_other_plan", 5)

    body = (await client.get(_ROUTE, headers=headers)).json()

    assert {row["name"] for row in body["counters"]} == {"a_counter_from_the_other_plan"}


async def test_the_window_bounds_the_query(client, db_session) -> None:
    """The table is append-only, so an unbounded read grows without limit as the history does."""
    headers = await _admin(db_session)
    await _seed(db_session, HarnessCounter.CLAIM_BLOCKED.value, 1, age_days=0)
    await _seed(db_session, HarnessCounter.CLAIM_BLOCKED.value, 99, age_days=30)

    body = (await client.get(f"{_ROUTE}?days=7", headers=headers)).json()

    assert body["counters"][0]["total"] == 1
    # LIVENESS: widen the window and the older row is genuinely there, so the 1 above is the
    # window doing its job rather than a row that never got written.
    wide = (await client.get(f"{_ROUTE}?days=60", headers=headers)).json()
    assert wide["counters"][0]["total"] == 100


async def test_an_absurd_window_is_clamped_rather_than_obeyed(client, db_session) -> None:
    """`days` comes off the query string. Clamped at both ends so neither 0 nor 100000 turns a
    bounded read into an unbounded one."""
    headers = await _admin(db_session)
    await _seed(db_session, HarnessCounter.CLAIM_BLOCKED.value, 1)

    for days in (0, -5, 100000):
        resp = await client.get(f"{_ROUTE}?days={days}", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["counters"][0]["total"] == 1
