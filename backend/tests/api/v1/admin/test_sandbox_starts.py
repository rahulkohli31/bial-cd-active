"""Sandbox start times, read by an operator: per kind of start, how many, how they ended, and the
median of each stage.

A start row names who started which app and when, so what this route must never do is hand one
back. The gate is opt-in per route in this package, which makes a citizen-readable version of it
work perfectly and silently.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.db.models.sandbox_start import (
    SandboxProjectType,
    SandboxStart,
    SandboxStartKind,
    SandboxStartMiss,
    SandboxStartOutcome,
)
from src.services.auth.session_jwt import mint_session_jwt
from tests.factories import AppRegistryFactory, UserFactory

_TTL = settings.auth.access_ttl_seconds
_ROUTE = "/v1/admin/sandbox-starts"


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
    """Every row in the window is counted, so the tests start from none."""
    await db_session.execute(sa.delete(SandboxStart))


@pytest.fixture
async def starter(db_session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    """A citizen and their app, for the rows to name."""
    user = await UserFactory.create(db_session, email="starter@rvaiglobal.com")
    app = await AppRegistryFactory.create(db_session, user_id=user.id)
    return user.id, app.id


async def _seed(
    db: AsyncSession,
    starter: tuple[uuid.UUID, uuid.UUID],
    kind: SandboxStartKind,
    *,
    age: timedelta = timedelta(hours=1),
    took_ms: int | None = None,
    **columns: Any,
) -> None:
    user_id, app_id = starter
    started_at = datetime.now(UTC) - age
    ended_at = None if took_ms is None else started_at + timedelta(milliseconds=took_ms)
    db.add(
        SandboxStart(
            user_id=user_id,
            app_id=app_id,
            kind=kind,
            project_type=SandboxProjectType.PLAIN,
            started_at=started_at,
            ended_at=ended_at,
            **columns,
        )
    )
    await db.flush()


async def test_a_citizen_cannot_read_the_start_times(client, db_session) -> None:
    resp = await client.get(_ROUTE, headers=await _citizen(db_session))

    assert resp.status_code == 403
    # LIVENESS: the route exists and answers, so the 403 is the gate rather than a typo'd path.
    assert (await client.get(_ROUTE, headers=await _admin(db_session))).status_code == 200


async def test_each_kind_reports_its_counts_and_the_median_of_each_stage(
    client, db_session, starter
) -> None:
    headers = await _admin(db_session)
    served = SandboxStartOutcome.SERVED
    reopen = SandboxStartKind.REOPEN
    await _seed(
        db_session,
        starter,
        reopen,
        took_ms=1000,
        outcome=served,
        admission_ms=10,
        create_ms=400,
        browser_visible_ms=1500,
    )
    await _seed(
        db_session, starter, reopen, took_ms=3000, outcome=served, admission_ms=30, claimed=True
    )
    await _seed(
        db_session,
        starter,
        reopen,
        took_ms=500,
        outcome=SandboxStartOutcome.FAILED,
        admission_ms=50,
        miss_reason=SandboxStartMiss.NO_READY,
    )
    await _seed(db_session, starter, SandboxStartKind.CHAT, miss_reason=SandboxStartMiss.SIZE_ZERO)

    body = (await client.get(_ROUTE, headers=headers)).json()

    reopens, chats = body["kinds"]
    assert reopens["kind"] == "reopen"
    counts = (reopens["starts"], reopens["served"], reopens["failed"], reopens["claimed"])
    assert counts == (3, 2, 1, 1)
    assert reopens["misses"] == {"no_ready": 1}
    assert reopens["medians"] == {
        "admissionMs": 30,
        "settingsMs": None,
        "createMs": 400,
        "devStartMs": None,
        "firstPageMs": None,
        "browserVisibleMs": 1500,
        # Over the served starts only: the failed one's 500 ms is not a time to a page.
        "totalMs": 2000,
    }
    assert chats["kind"] == "chat"
    assert (chats["starts"], chats["served"], chats["failed"]) == (1, 0, 0)
    assert chats["misses"] == {"size_zero": 1}


async def test_no_start_comes_back_with_who_started_it(client, db_session, starter) -> None:
    """Mutation check: add the row's `user_id` to the summary and this goes red."""
    headers = await _admin(db_session)
    await _seed(db_session, starter, SandboxStartKind.REOPEN)

    resp = await client.get(_ROUTE, headers=headers)

    user_id, app_id = starter
    assert resp.status_code == 200
    assert str(user_id) not in resp.text
    assert str(app_id) not in resp.text
    assert set(resp.json()["kinds"][0]) == {
        "kind",
        "starts",
        "served",
        "failed",
        "claimed",
        "misses",
        "medians",
    }


async def test_the_window_bounds_the_query(client, db_session, starter) -> None:
    headers = await _admin(db_session)
    await _seed(db_session, starter, SandboxStartKind.REOPEN, age=timedelta(hours=1))
    await _seed(db_session, starter, SandboxStartKind.REOPEN, age=timedelta(days=30))

    narrow = (await client.get(f"{_ROUTE}?days=7", headers=headers)).json()
    wide = (await client.get(f"{_ROUTE}?days=60", headers=headers)).json()

    assert narrow["kinds"][0]["starts"] == 1
    # LIVENESS: the older row is genuinely there, so the 1 above is the window at work.
    assert wide["kinds"][0]["starts"] == 2


async def test_an_absurd_window_is_clamped_rather_than_obeyed(client, db_session) -> None:
    headers = await _admin(db_session)

    for days in (0, -5, 100000):
        resp = await client.get(f"{_ROUTE}?days={days}", headers=headers)
        assert resp.status_code == 200
        since = datetime.fromisoformat(resp.json()["since"])
        assert timedelta(days=1) <= datetime.now(UTC) - since <= timedelta(days=90, minutes=1)
