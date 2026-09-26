"""The App Registry listing: every app, each with the status an administrator reads and the
version serving now, loaded in the same number of queries whatever the list holds."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.db.models.app_registry import AppRegistry, AppStatus
from src.db.models.deployment import Deployment, DeploymentStatus
from src.main import create_app
from src.services.auth.session_jwt import mint_session_jwt
from src.services.deploy.service import FAIL_RESTART
from tests.factories import AppRegistryFactory, UserFactory

_TTL = settings.auth.access_ttl_seconds
_LIST = "/v1/admin/apps"
_LIVE_SHA = "7a3c9e0" + "1" * 33
_OLDER_SHA = "1b7d220" + "2" * 33
_WENT_LIVE = datetime(2026, 9, 25, 16, 40, tzinfo=UTC)


def _cookie(user_id: uuid.UUID, token_version: int) -> dict[str, str]:
    return {"Cookie": f"session={mint_session_jwt(user_id, token_version, _TTL)}"}


async def _admin(db: AsyncSession) -> dict[str, str]:
    user = await UserFactory.create(db, email="admin@bial.com")
    return _cookie(user.id, user.token_version)


async def _app(db: AsyncSession, **overrides: Any) -> AppRegistry:
    owner = await UserFactory.create(db)
    return await AppRegistryFactory.create(db, user_id=owner.id, **overrides)


async def _attempt(
    db: AsyncSession,
    app: AppRegistry,
    status: DeploymentStatus = DeploymentStatus.SUCCEEDED,
    **fields: Any,
) -> Deployment:
    """One deploy attempt. A succeeded one published an image at a URL, as the pipeline's does."""
    published = status is DeploymentStatus.SUCCEEDED
    data: dict[str, Any] = {
        "app_id": app.id,
        "user_id": app.user_id,
        "status": status,
        "head_sha": _LIVE_SHA,
        "image_digest": "sha256:" + "ab" * 32 if published else None,
        "url": "https://pub.example/" if published else None,
        "finished_at": None if status is DeploymentStatus.RUNNING else _WENT_LIVE,
    }
    data.update(fields)
    row = Deployment(**data)
    db.add(row)
    await db.flush()
    return row


def _approved_copy(**extra: Any) -> dict[str, Any]:
    return {
        "status": AppStatus.APPROVED,
        "approved_submission_id": uuid.uuid4(),
        "approved_commit_sha": _LIVE_SHA,
        "approved_at": datetime.now(UTC) - timedelta(hours=1),
        **extra,
    }


async def _rows(client, headers: dict[str, str]) -> dict[str, dict[str, Any]]:
    resp = await client.get(_LIST, headers=headers)
    assert resp.status_code == 200, resp.text
    return {row["appId"]: row for row in resp.json()["apps"]}


async def test_the_list_holds_every_app_whatever_its_status(client, db_session) -> None:
    apps = [await _app(db_session, status=status) for status in AppStatus]
    headers = await _admin(db_session)

    rows = await _rows(client, headers)

    assert {str(app.id) for app in apps} <= set(rows)


async def test_a_status_query_narrows_nothing(client, db_session) -> None:
    draft = await _app(db_session)
    pending = await _app(db_session, status=AppStatus.PENDING)
    headers = await _admin(db_session)

    narrowed = await client.get(f"{_LIST}?status=pending", headers=headers)
    bogus = await client.get(f"{_LIST}?status=bogus", headers=headers)

    assert {str(draft.id), str(pending.id)} <= {row["appId"] for row in narrowed.json()["apps"]}
    assert bogus.status_code == 200
    listing = create_app().openapi()["paths"][_LIST]["get"]
    assert "parameters" not in listing
    assert "400" not in listing["responses"]


async def test_each_row_carries_the_status_an_administrator_reads(client, db_session) -> None:
    draft = await _app(db_session)
    waiting = await _app(db_session, status=AppStatus.PENDING)
    rejected = await _app(db_session, status=AppStatus.REJECTED)
    disabled = await _app(db_session, status=AppStatus.DISABLED)
    await _attempt(db_session, disabled)
    not_published = await _app(db_session, **_approved_copy())
    publishing = await _app(db_session, **_approved_copy())
    await _attempt(db_session, publishing, DeploymentStatus.RUNNING)
    live = await _app(db_session)
    await _attempt(db_session, live)
    failed = await _app(db_session)
    await _attempt(db_session, failed, DeploymentStatus.FAILED, failure_code="build_failed")
    offline = await _app(db_session)
    await _attempt(db_session, offline, unpublished_at=_WENT_LIVE + timedelta(days=1))
    headers = await _admin(db_session)

    rows = await _rows(client, headers)

    expected = [
        (draft, "draft"),
        (waiting, "waiting_for_review"),
        (rejected, "rejected"),
        (disabled, "disabled"),
        (not_published, "not_published"),
        (publishing, "publishing"),
        (live, "live"),
        (failed, "publish_failed"),
        (offline, "taken_offline"),
    ]
    assert [rows[str(app.id)]["registryStatus"] for app, _ in expected] == [
        status for _, status in expected
    ]


async def test_the_live_version_is_the_commit_serving_and_when_it_went_live(
    client, db_session
) -> None:
    app = await _app(db_session)
    await _attempt(
        db_session, app, head_sha=_OLDER_SHA, finished_at=_WENT_LIVE - timedelta(days=6)
    )
    await _attempt(db_session, app, head_sha=_LIVE_SHA, finished_at=_WENT_LIVE)
    headers = await _admin(db_session)

    row = (await _rows(client, headers))[str(app.id)]

    assert row["registryStatus"] == "live"
    assert row["liveVersion"] == {"commitSha": _LIVE_SHA, "since": "2026-09-25T16:40:00Z"}


async def test_an_app_waiting_for_review_shows_the_version_still_serving(
    client, db_session
) -> None:
    app = await _app(db_session, status=AppStatus.PENDING)
    await _attempt(db_session, app, head_sha=_OLDER_SHA)
    headers = await _admin(db_session)

    row = (await _rows(client, headers))[str(app.id)]

    assert row["registryStatus"] == "waiting_for_review"
    assert row["liveVersion"]["commitSha"] == _OLDER_SHA


async def test_a_failed_restart_keeps_the_version_it_restarted_live(client, db_session) -> None:
    app = await _app(db_session)
    await _attempt(db_session, app, head_sha=_LIVE_SHA)
    await _attempt(
        db_session,
        app,
        DeploymentStatus.FAILED,
        failure_code=FAIL_RESTART,
        head_sha=_LIVE_SHA,
        finished_at=_WENT_LIVE + timedelta(days=1),
    )
    headers = await _admin(db_session)

    row = (await _rows(client, headers))[str(app.id)]

    assert row["registryStatus"] == "live"
    assert row["liveVersion"] == {"commitSha": _LIVE_SHA, "since": "2026-09-25T16:40:00Z"}


async def test_a_live_row_always_names_the_version_it_serves(client, db_session) -> None:
    """A withdrawn re-submission keeps its standing rejection, which hides it from the
    marketplace, while its older version keeps serving: the row still names that version."""
    app = await _app(db_session, rejection_standing=True)
    await _attempt(db_session, app, head_sha=_OLDER_SHA)
    headers = await _admin(db_session)

    row = (await _rows(client, headers))[str(app.id)]

    assert row["registryStatus"] == "live"
    assert row["liveVersion"]["commitSha"] == _OLDER_SHA


async def test_an_app_with_nothing_serving_has_no_live_version(client, db_session) -> None:
    never = await _app(db_session)
    failed_first = await _app(db_session)
    await _attempt(db_session, failed_first, DeploymentStatus.FAILED, failure_code="build_failed")
    offline = await _app(db_session)
    await _attempt(db_session, offline, unpublished_at=_WENT_LIVE + timedelta(days=1))
    disabled = await _app(db_session, status=AppStatus.DISABLED)
    await _attempt(db_session, disabled)
    serving = await _app(db_session)
    await _attempt(db_session, serving)
    headers = await _admin(db_session)

    rows = await _rows(client, headers)

    assert rows[str(serving.id)]["liveVersion"] is not None
    for app in (never, failed_first, offline, disabled):
        assert rows[str(app.id)]["liveVersion"] is None, rows[str(app.id)]["registryStatus"]


async def test_the_most_recently_active_app_comes_first(client, db_session) -> None:
    now = datetime.now(UTC)
    busy = await _app(db_session, created_at=now - timedelta(days=9), updated_at=now)
    quiet = await _app(
        db_session, created_at=now - timedelta(days=4), updated_at=now - timedelta(days=3)
    )
    headers = await _admin(db_session)

    order = list(await _rows(client, headers))

    assert order.index(str(busy.id)) < order.index(str(quiet.id))


async def test_the_listing_costs_the_same_queries_for_two_apps_as_for_twelve(
    client, db_session, test_engine
) -> None:
    """Mutation check: load either deployment read per row and the second count grows."""
    headers = await _admin(db_session)

    async def _statements_for(extra_apps: int) -> int:
        for _ in range(extra_apps):
            app = await _app(db_session)
            await _attempt(db_session, app)
        statements: list[str] = []

        def _record(conn, cursor, statement, parameters, context, executemany) -> None:
            statements.append(statement)

        event.listen(test_engine.sync_engine, "before_cursor_execute", _record)
        try:
            await _rows(client, headers)
        finally:
            event.remove(test_engine.sync_engine, "before_cursor_execute", _record)
        return len(statements)

    assert await _statements_for(2) == await _statements_for(10)


async def test_a_citizen_is_refused_the_list(client, db_session) -> None:
    citizen = await UserFactory.create(db_session, email="nobody@rvaiglobal.com")

    resp = await client.get(_LIST, headers=_cookie(citizen.id, citizen.token_version))

    assert resp.status_code == 403
