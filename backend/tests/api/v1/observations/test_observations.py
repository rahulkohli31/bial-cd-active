"""POST /v1/observations — the browser's one narrow write path (U3; R104, R105, R106).

WHAT THESE PIN, and it is a short list on purpose: the only counter names this route can ever
produce are the three it allows, and a malformed or hostile call writes NOTHING and returns a
refusal rather than a silent success.

The rows escape the test transaction: `count(...)` owns its own session and COMMITS, exactly so a
count survives a rolled-back transaction (`tests/services/build_sessions/test_counters.py` pins
that property). So each test starts from a known-empty table rather than from a rollback that
cannot reach these rows.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from httpx import AsyncClient

from src.api.v1.observations.router import MAX_OBSERVED_MS, OBSERVATION_RATE_LIMIT
from src.config import settings
from src.db.base import async_session_factory
from src.db.models.harness_counter import HarnessCount, HarnessCounter
from src.db.models.user import User
from src.services.auth.csrf import issue_csrf_token
from src.services.auth.session_jwt import mint_session_jwt
from tests.factories import UserFactory

_TTL = settings.auth.access_ttl_seconds


def _headers(user: User, *, with_csrf: bool = True) -> dict[str, str]:
    jwt = mint_session_jwt(user.id, user.token_version, _TTL)
    if not with_csrf:
        return {"Cookie": f"session={jwt}"}
    csrf = issue_csrf_token(user.id, user.token_version)
    return {"Cookie": f"session={jwt}; csrf={csrf}", "X-CSRF-Token": csrf}


@pytest.fixture(autouse=True)
async def _empty_counts(empty_harness_counts: None) -> None:
    """Every test in this file starts from an empty table — see `tests/conftest.py`."""


async def _rows() -> list[tuple[str, int, object, object]]:
    """Every row in the table, as columns — the session that read them is closed by the time the
    assertion runs. `app_id` and `build_id` come back so "no row carries an identity" is
    assertable on the whole table rather than on the one column a test happened to look at."""
    async with async_session_factory() as db:
        found = (
            await db.execute(
                sa.select(
                    HarnessCount.name,
                    HarnessCount.value,
                    HarnessCount.app_id,
                    HarnessCount.build_id,
                )
            )
        ).all()
    return [(n, int(v), a, b) for n, v, a, b in found]


# --- happy path ------------------------------------------------------------------------------


async def test_a_duration_is_recorded_with_the_value_the_browser_measured(
    client: AsyncClient, db_session
) -> None:
    user = await UserFactory.create(db_session, email="obs-ok@rvaiglobal.com")

    resp = await client.post(
        "/v1/observations",
        json={"name": HarnessCounter.PROJECT_TO_APP_VISIBLE_MS.value, "value": 7321},
        headers=_headers(user),
    )

    assert resp.status_code == 201
    assert resp.json() == {"ok": True}
    assert await _rows() == [(HarnessCounter.PROJECT_TO_APP_VISIBLE_MS.value, 7321, None, None)]


async def test_an_occurrence_needs_no_value_and_records_one(
    client: AsyncClient, db_session
) -> None:
    user = await UserFactory.create(db_session, email="obs-occ@rvaiglobal.com")

    resp = await client.post(
        "/v1/observations",
        json={"name": HarnessCounter.PROJECT_OPENED.value},
        headers=_headers(user),
    )

    assert resp.status_code == 201
    assert await _rows() == [(HarnessCounter.PROJECT_OPENED.value, 1, None, None)]


async def test_no_row_this_route_writes_carries_an_identity(
    client: AsyncClient, db_session
) -> None:
    """★ THE TRADE THIS ROUTE MAKES, asserted rather than described. The identity bounds who may
    write and is then discarded; `harness_counts` has never been user-scoped and must not start
    here. There is no user column to check — which IS the property — so this pins the two id
    columns that DO exist staying empty, and the row count staying at what was sent."""
    user = await UserFactory.create(db_session, email="obs-anon@rvaiglobal.com")

    for name in (
        HarnessCounter.PROJECT_OPENED.value,
        HarnessCounter.PROJECT_OPENED_CHAT.value,
        HarnessCounter.PROJECT_TO_APP_VISIBLE_MS.value,
    ):
        assert (
            await client.post(
                "/v1/observations", json={"name": name, "value": 1}, headers=_headers(user)
            )
        ).status_code == 201

    rows = await _rows()
    assert len(rows) == 3
    assert all(app_id is None and build_id is None for _, _, app_id, build_id in rows)


# --- the allowlist ---------------------------------------------------------------------------


async def test_a_counter_that_exists_but_is_not_browser_observable_is_refused(
    client: AsyncClient, db_session
) -> None:
    """★ THE INTERESTING BYPASS, and the reason the allowlist is a mapping on this side rather
    than a rule the browser is asked to follow. `app_start_reached_running` is a real member of
    `HarnessCounter` — a browser that could write it could inflate R103's numerator at will."""
    user = await UserFactory.create(db_session, email="obs-bypass@rvaiglobal.com")

    resp = await client.post(
        "/v1/observations",
        json={"name": HarnessCounter.APP_START_REACHED_RUNNING.value},
        headers=_headers(user),
    )

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "unknown_counter"
    assert await _rows() == []


async def test_an_invented_counter_name_is_refused(client: AsyncClient, db_session) -> None:
    user = await UserFactory.create(db_session, email="obs-invent@rvaiglobal.com")

    resp = await client.post(
        "/v1/observations",
        json={"name": "everything_is_fine", "value": 1},
        headers=_headers(user),
    )

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "unknown_counter"
    assert await _rows() == []


# --- the ceiling -----------------------------------------------------------------------------


async def test_a_duration_above_the_ceiling_is_refused_and_not_clamped(
    client: AsyncClient, db_session
) -> None:
    """★ REFUSES RATHER THAN CLAMPS. A clamped duration silently reads as exactly the ceiling and
    would quietly flatter the mean — losing the row is the better failure.

    Mutation check: clamp instead of refuse and this goes red twice over, on the status and on
    the empty table."""
    user = await UserFactory.create(db_session, email="obs-ceiling@rvaiglobal.com")

    resp = await client.post(
        "/v1/observations",
        json={
            "name": HarnessCounter.PROJECT_TO_APP_VISIBLE_MS.value,
            "value": MAX_OBSERVED_MS + 1,
        },
        headers=_headers(user),
    )

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "value_out_of_range"
    assert await _rows() == []


async def test_the_ceiling_itself_is_accepted(client: AsyncClient, db_session) -> None:
    # The boundary belongs to the honest side: a journey that took exactly the budget happened.
    user = await UserFactory.create(db_session, email="obs-edge@rvaiglobal.com")

    resp = await client.post(
        "/v1/observations",
        json={"name": HarnessCounter.PROJECT_TO_APP_VISIBLE_MS.value, "value": MAX_OBSERVED_MS},
        headers=_headers(user),
    )

    assert resp.status_code == 201
    assert await _rows() == [
        (HarnessCounter.PROJECT_TO_APP_VISIBLE_MS.value, MAX_OBSERVED_MS, None, None)
    ]


async def test_a_duration_with_no_value_is_refused_rather_than_recorded_as_one_millisecond(
    client: AsyncClient, db_session
) -> None:
    """★ A missing value means something for an OCCURRENCE and nothing for a DURATION.

    Defaulting a duration to 1 files a one-millisecond first view — the same silent corruption of
    the mean this route refuses a too-LARGE value to avoid, arriving from the other end, and with
    no user id on the row it is exactly as impossible to exclude afterwards.

    Mutation check: return 1 for any missing value and this goes red."""
    user = await UserFactory.create(db_session, email="obs-noval@rvaiglobal.com")

    resp = await client.post(
        "/v1/observations",
        json={"name": HarnessCounter.PROJECT_TO_APP_VISIBLE_MS.value},
        headers=_headers(user),
    )

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_value"
    assert await _rows() == []


async def test_a_body_that_is_not_json_at_all_is_refused(client: AsyncClient, db_session) -> None:
    """The hand-parsed-body branch, reachable only with a RAW payload.

    Every other test here posts through httpx's `json=`, which always serialises valid JSON — so
    the `except (ValueError, TypeError)` arm that is the whole reason this route parses its own
    body was, until this test, unreachable by the suite that covers it."""
    user = await UserFactory.create(db_session, email="obs-rawbody@rvaiglobal.com")

    resp = await client.post(
        "/v1/observations",
        content=b"{not json at all",
        headers={**_headers(user), "Content-Type": "application/json"},
    )

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_body"
    assert await _rows() == []


async def test_the_route_does_not_relate_one_counter_to_another(
    client: AsyncClient, db_session
) -> None:
    """The gap this route deliberately does NOT close, pinned so it cannot close by accident.

    Each name is bounded alone; nothing here knows that R105's numerator should never outrun its
    denominator. That invariant lives in the browser, which means it holds for the portal and not
    for a hand-made request. Enforcing it server-side needs a visit token this plan does not
    build — so the behaviour is documented, and the reading rule (a ratio outside [0,1] is
    poisoned data, not a surprising result) lives beside the number."""
    user = await UserFactory.create(db_session, email="obs-invariant@rvaiglobal.com")

    resp = await client.post(
        "/v1/observations",
        json={"name": HarnessCounter.PROJECT_OPENED_CHAT.value},
        headers=_headers(user),
    )

    assert resp.status_code == 201  # accepted with no `project_opened` behind it
    assert await _rows() == [(HarnessCounter.PROJECT_OPENED_CHAT.value, 1, None, None)]


async def test_an_occurrence_cannot_be_sent_as_a_batch(client: AsyncClient, db_session) -> None:
    """An occurrence IS one. A browser reporting `project_opened` with a value of 40 is not
    reporting an occurrence, it is inflating R105's denominator — and a denominator a client can
    inflate is not a measurement."""
    user = await UserFactory.create(db_session, email="obs-batch@rvaiglobal.com")

    resp = await client.post(
        "/v1/observations",
        json={"name": HarnessCounter.PROJECT_OPENED.value, "value": 40},
        headers=_headers(user),
    )

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "value_out_of_range"
    assert await _rows() == []


@pytest.mark.parametrize(
    "value",
    [-1, 0, "1200", 12.5, True, [1]],
    ids=["negative", "zero", "string", "float", "bool", "list"],
)
async def test_a_value_that_is_not_a_positive_whole_number_is_refused(
    client: AsyncClient, db_session, value: object
) -> None:
    # `True` is in here because `bool` IS an `int` in Python and would otherwise sail through as
    # 1. A boolean is not a measurement.
    email = f"obs-bad-{type(value).__name__}@rvaiglobal.com"
    user = await UserFactory.create(db_session, email=email)

    resp = await client.post(
        "/v1/observations",
        json={"name": HarnessCounter.PROJECT_TO_APP_VISIBLE_MS.value, "value": value},
        headers=_headers(user),
    )

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] in {"invalid_value", "value_out_of_range"}
    assert await _rows() == []


async def test_a_body_that_is_not_an_object_is_refused(client: AsyncClient, db_session) -> None:
    user = await UserFactory.create(db_session, email="obs-body@rvaiglobal.com")

    resp = await client.post("/v1/observations", json=[1, 2, 3], headers=_headers(user))

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_body"
    assert await _rows() == []


# --- the gates -------------------------------------------------------------------------------


async def test_unauthenticated_writes_nothing(client: AsyncClient) -> None:
    resp = await client.post(
        "/v1/observations", json={"name": HarnessCounter.PROJECT_OPENED.value}
    )

    assert resp.status_code == 401
    assert await _rows() == []


async def test_a_missing_csrf_header_is_refused_with_the_data_plane_envelope(
    client: AsyncClient, db_session
) -> None:
    """`RequireCsrf` is opt-in per route in this codebase, so this is the assertion that it was
    actually opted into: a forged write pollutes the only measurement the platform has."""
    user = await UserFactory.create(db_session, email="obs-csrf@rvaiglobal.com")

    resp = await client.post(
        "/v1/observations",
        json={"name": HarnessCounter.PROJECT_OPENED.value},
        headers=_headers(user, with_csrf=False),
    )

    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "csrf_failed"
    assert await _rows() == []


async def test_the_per_user_limit_stops_the_next_one_and_keeps_the_ones_before_it(
    client: AsyncClient, db_session
) -> None:
    user = await UserFactory.create(db_session, email="obs-limit@rvaiglobal.com")
    headers = _headers(user)

    for _ in range(OBSERVATION_RATE_LIMIT):
        resp = await client.post(
            "/v1/observations",
            json={"name": HarnessCounter.PROJECT_OPENED.value},
            headers=headers,
        )
        assert resp.status_code == 201

    blocked = await client.post(
        "/v1/observations", json={"name": HarnessCounter.PROJECT_OPENED.value}, headers=headers
    )

    assert blocked.status_code == 429
    # The limit refuses the NEXT one; it does not retract what was already observed.
    assert len(await _rows()) == OBSERVATION_RATE_LIMIT

    # ★ AND IT IS PER USER. Without this, a limiter keyed on a constant passes every assertion
    # above while letting any one citizen silence everybody else's measurements for five minutes.
    # Mutation check: return a constant from `_observation_rate_key` and this goes red.
    neighbour = await UserFactory.create(db_session, email="obs-limit-neighbour@rvaiglobal.com")
    ok = await client.post(
        "/v1/observations",
        json={"name": HarnessCounter.PROJECT_OPENED.value},
        headers=_headers(neighbour),
    )
    assert ok.status_code == 201


def test_observations_openapi_documents_its_refusals() -> None:
    from src.main import create_app

    op = create_app().openapi()["paths"]["/v1/observations"]["post"]
    assert {"400", "401", "403", "429"} <= set(op["responses"])
    assert "201" in op["responses"]
    props = op["requestBody"]["content"]["application/json"]["schema"]["properties"]
    assert "name" in props and "value" in props
