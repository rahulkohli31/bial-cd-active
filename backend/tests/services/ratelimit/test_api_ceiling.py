"""One ceiling across the API, per signed-in user.

The production ceiling is far above any test's request count, so each test swaps in a tiny
limiter with an injectable clock. The decision under test is WHO is counted and in WHICH
bucket, not the number.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.api import deps
from src.config import settings
from src.services.auth.session_jwt import mint_session_jwt
from src.services.ratelimit import InProcessRateLimiter
from tests.factories import UserFactory

_LIMIT = 3
_PROBE = "/v1/projects/counts"


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    tick = _Clock()
    monkeypatch.setattr(
        deps, "_API_CEILING", InProcessRateLimiter(limit=_LIMIT, window_seconds=60, clock=tick)
    )
    return tick


async def _session(db: AsyncSession) -> dict[str, str]:
    person = await UserFactory.create(db)
    jwt = mint_session_jwt(person.id, person.token_version, settings.auth.access_ttl_seconds)
    return {"Cookie": f"session={jwt}"}


def _from(site: str | None, cookie: dict[str, str]) -> dict[str, str]:
    return cookie if site is None else {**cookie, "Sec-Fetch-Site": site}


async def test_the_portal_bucket_refuses_the_request_over_the_ceiling(
    client: AsyncClient, db_session: AsyncSession, clock: _Clock
) -> None:
    me = await _session(db_session)
    for _ in range(_LIMIT):
        allowed = await client.get(_PROBE, headers=_from("same-origin", me))
        assert allowed.status_code == 200

    refused = await client.get(_PROBE, headers=_from("same-origin", me))

    assert refused.status_code == 429
    assert refused.json()["error"]["message"]


async def test_a_generated_apps_traffic_cannot_spend_the_portals_budget(
    client: AsyncClient, db_session: AsyncSession, clock: _Clock
) -> None:
    me = await _session(db_session)
    for _ in range(_LIMIT):
        await client.get(_PROBE, headers=_from("same-site", me))
    assert (await client.get(_PROBE, headers=_from("same-site", me))).status_code == 429
    assert (await client.get(_PROBE, headers=_from("cross-site", me))).status_code == 429

    assert (await client.get(_PROBE, headers=_from("same-origin", me))).status_code == 200


async def test_a_request_without_fetch_metadata_counts_as_the_portal(
    client: AsyncClient, db_session: AsyncSession, clock: _Clock
) -> None:
    me = await _session(db_session)
    for _ in range(_LIMIT):
        await client.get(_PROBE, headers=_from(None, me))

    assert (await client.get(_PROBE, headers=_from("same-origin", me))).status_code == 429


async def test_one_users_ceiling_is_not_anothers(
    client: AsyncClient, db_session: AsyncSession, clock: _Clock
) -> None:
    me, colleague = await _session(db_session), await _session(db_session)
    for _ in range(_LIMIT + 1):
        await client.get(_PROBE, headers=me)

    assert (await client.get(_PROBE, headers=colleague)).status_code == 200


async def test_the_window_rolls_over(
    client: AsyncClient, db_session: AsyncSession, clock: _Clock
) -> None:
    me = await _session(db_session)
    for _ in range(_LIMIT + 1):
        await client.get(_PROBE, headers=me)

    clock.now += 60

    assert (await client.get(_PROBE, headers=me)).status_code == 200


@pytest.mark.parametrize(
    "cookie",
    [
        {},
        {"Cookie": "session=not-a-jwt"},
        {"Cookie": "session=eyJhbGciOiJub25lIn0.e30."},
        {"Cookie": f"session={mint_session_jwt(uuid.uuid4(), 0, -60)}"},
    ],
    ids=["none", "garbage", "unsigned", "expired"],
)
async def test_a_request_without_a_valid_session_is_never_counted(
    client: AsyncClient, clock: _Clock, cookie: dict[str, str]
) -> None:
    statuses = {(await client.get(_PROBE, headers=cookie)).status_code for _ in range(5)}

    assert statuses == {401}


async def test_the_ceiling_runs_before_the_route_does(
    client: AsyncClient, db_session: AsyncSession, clock: _Clock
) -> None:
    """A refused request never reaches the route: a citizen over the ceiling gets 429 from an
    admin route, not that route's own 403."""
    me = await _session(db_session)
    for _ in range(_LIMIT):
        await client.get(_PROBE, headers=me)

    assert (await client.get("/v1/admin/apps", headers=me)).status_code == 429


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/v1/auth/me"),
        ("GET", "/v1/health"),
        ("POST", "/v1/auth/logout"),
        ("POST", "/v1/auth/refresh"),
    ],
)
async def test_the_sessions_own_routes_stay_outside_the_ceiling(
    client: AsyncClient, db_session: AsyncSession, clock: _Clock, method: str, path: str
) -> None:
    """A refused who-am-I reads as signed out in the portal, and a refused sign-out leaves the
    session alive, so a person over the ceiling must still reach both."""
    me = await _session(db_session)
    for _ in range(_LIMIT):
        await client.get(_PROBE, headers=me)
    assert (await client.get(_PROBE, headers=me)).status_code == 429

    assert (await client.request(method, path, headers=me)).status_code != 429


def test_only_the_sessions_own_routes_are_mounted_outside_the_ceiling(app) -> None:
    """The ceiling and its documented 429 come from one router, so a router mounted beside it
    instead of inside it shows here as an operation without the 429."""
    outside = ("/v1/auth/", "/v1/health")
    for path, operations in app.openapi()["paths"].items():
        for method, operation in operations.items():
            ceilinged = "429" in operation["responses"]
            assert ceilinged != path.startswith(outside), f"{method.upper()} {path}"
