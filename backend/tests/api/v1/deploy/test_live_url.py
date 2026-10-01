"""`liveUrl` on the deployment read: the address the app is serving at now.

The latest attempt's `url` is empty while a republish runs and after one fails, so the live
address comes from the same collapse that decides `isServing` on the projects list. The last
test reads both routes over a mix of states and requires them to agree."""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi import FastAPI

from src.api.deps import storage_or_none_dependency
from src.db.models.app_registry import AppRegistry, AppStatus
from src.db.models.deployment import Deployment, DeploymentStatus
from tests.api.v1.build_sessions.conftest import auth_headers
from tests.factories import AppRegistryFactory, ProjectFactory, UserFactory

_STATUS = "/v1/projects/{pid}/deployment"
_PROJECTS = "/v1/projects"
_OLD_URL = "https://app-old.example.azurecontainerapps.io/"
_NEW_URL = "https://app-new.example.azurecontainerapps.io/"


@pytest.fixture(autouse=True)
def no_store(app: FastAPI) -> None:
    # The saved-version half of the response is not under test; an unconfigured store is a
    # supported state for this route.
    app.dependency_overrides[storage_or_none_dependency] = lambda: None


async def _deploy(
    db,
    app: AppRegistry,
    *,
    status: DeploymentStatus = DeploymentStatus.SUCCEEDED,
    url: str | None = _OLD_URL,
    unpublished_at: dt.datetime | None = None,
) -> Deployment:
    """One deploy attempt. Rows are append-only and ordered by their UUIDv7 ids."""
    row = Deployment(
        app_id=app.id,
        user_id=app.user_id,
        status=status,
        image_digest="sha256:" + "cd" * 32 if status is DeploymentStatus.SUCCEEDED else None,
        url=url,
        unpublished_at=unpublished_at,
    )
    db.add(row)
    await db.flush()
    return row


async def _live_url(client, user, app: AppRegistry) -> str | None:
    resp = await client.get(_STATUS.format(pid=app.project_id), headers=auth_headers(user))
    assert resp.status_code == 200, resp.text
    live_url: str | None = resp.json()["liveUrl"]
    return live_url


async def test_the_latest_successful_deploy_is_the_live_address(client, db_session) -> None:
    user = await UserFactory.create(db_session)
    app = await AppRegistryFactory.create(db_session, user_id=user.id)
    await _deploy(db_session, app, url=_OLD_URL)
    await _deploy(db_session, app, url=_NEW_URL)

    assert await _live_url(client, user, app) == _NEW_URL


@pytest.mark.parametrize(
    "republish",
    [
        pytest.param(DeploymentStatus.RUNNING, id="republish running"),
        pytest.param(DeploymentStatus.FAILED, id="republish failed"),
    ],
)
async def test_a_republish_without_an_address_keeps_the_older_live_address(
    client, db_session, republish: DeploymentStatus
) -> None:
    """The latest attempt has no URL here, which is why the latest-attempt `url` cannot be the
    source of the address the menu opens."""
    user = await UserFactory.create(db_session)
    app = await AppRegistryFactory.create(db_session, user_id=user.id)
    await _deploy(db_session, app, url=_OLD_URL)
    await _deploy(db_session, app, status=republish, url=None)

    resp = await client.get(_STATUS.format(pid=app.project_id), headers=auth_headers(user))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == republish.value
    assert body["url"] is None
    assert body["liveUrl"] == _OLD_URL


async def test_a_takedown_clears_the_address_until_a_later_deploy_succeeds(
    client, db_session
) -> None:
    user = await UserFactory.create(db_session)
    app = await AppRegistryFactory.create(db_session, user_id=user.id)
    await _deploy(db_session, app, url=_OLD_URL, unpublished_at=dt.datetime.now(dt.UTC))

    assert await _live_url(client, user, app) is None

    await _deploy(db_session, app, url=_NEW_URL)

    assert await _live_url(client, user, app) == _NEW_URL


async def test_a_never_deployed_app_has_no_live_address(client, db_session) -> None:
    user = await UserFactory.create(db_session)
    app = await AppRegistryFactory.create(db_session, user_id=user.id)

    resp = await client.get(_STATUS.format(pid=app.project_id), headers=auth_headers(user))

    assert resp.status_code == 200, resp.text
    assert resp.json()["deploymentId"] is None
    assert resp.json()["liveUrl"] is None


async def test_the_live_address_agrees_with_is_serving_on_the_projects_list(
    client, db_session
) -> None:
    """Two reads of one definition. A disabled app is the case a deployment-only rule gets
    wrong: its newest attempt succeeded with a URL and was never unpublished."""
    user = await UserFactory.create(db_session)
    apps: dict[str, AppRegistry] = {}

    async def make(name: str, *, status: AppStatus = AppStatus.DRAFT) -> AppRegistry:
        project = await ProjectFactory.create(db_session, user.id, name=name)
        apps[name] = await AppRegistryFactory.create(
            db_session, user_id=user.id, project_id=project.id, status=status
        )
        return apps[name]

    await _deploy(db_session, await make("Live"))
    running = await make("Republishing")
    await _deploy(db_session, running)
    await _deploy(db_session, running, status=DeploymentStatus.RUNNING, url=None)
    failed = await make("Republish Failed")
    await _deploy(db_session, failed)
    await _deploy(db_session, failed, status=DeploymentStatus.FAILED, url=None)
    await _deploy(db_session, await make("Taken Down"), unpublished_at=dt.datetime.now(dt.UTC))
    await _deploy(db_session, await make("Switched Off", status=AppStatus.DISABLED))
    await make("Never Deployed")

    listed = await client.get(_PROJECTS, headers=auth_headers(user))
    assert listed.status_code == 200, listed.text
    serving = {item["id"]: item["isServing"] for item in listed.json()["items"]}
    has_live_url = {
        str(app.project_id): await _live_url(client, user, app) is not None
        for app in apps.values()
    }

    assert has_live_url == serving
    assert set(serving.values()) == {True, False}


async def test_another_users_app_is_not_found(client, db_session) -> None:
    owner = await UserFactory.create(db_session)
    stranger = await UserFactory.create(db_session)
    app = await AppRegistryFactory.create(db_session, user_id=owner.id)
    await _deploy(db_session, app, url=_OLD_URL)

    theirs = await client.get(_STATUS.format(pid=app.project_id), headers=auth_headers(stranger))
    mine = await client.get(_STATUS.format(pid=app.project_id), headers=auth_headers(owner))

    assert theirs.status_code == 404
    assert _OLD_URL not in theirs.text
    assert mine.status_code == 200
    assert mine.json()["liveUrl"] == _OLD_URL
