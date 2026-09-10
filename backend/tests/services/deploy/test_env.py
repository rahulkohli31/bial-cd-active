"""What a PUBLISHED app is given — and, for the connector coordinates, who it is given to.

WHY THIS FILE EXISTS NOW. `build_published_env` had no tests of its own: the deploy-service suite
monkeypatches it away (correctly — it must not reach Azure there), and the envelope suite starts
from an env dict handed to it. That was survivable while the function only assembled values every
app gets. It stopped being survivable the moment one of those values became a grant: the
connector coordinates are per person and per project, and an app published by somebody who never
had access to a connector must not carry a credential to it.

THE ARGUMENT THAT MAKES THE GATE REAL IS `user_id`. It is threaded in rather than looked up here,
because the caller already holds it and a second lookup is a second chance to scope it wrongly —
so the test that matters most below is the one where a stranger publishes somebody else's
project and gets nothing.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.db.models.connector_access import ConnectorRequestStatus
from src.db.models.project_connector import ConnectorWindowKind, ProjectConnector
from src.services.deploy.env import build_published_env
from src.services.lake.config import LakeConfig
from src.services.lake.env import connector_env_names
from src.services.sandbox.config import SandboxConfig
from tests.api.v1.connectors.conftest import KEY, seed_decision
from tests.factories import AppRegistryFactory, ProjectFactory, UserFactory

_LAKE_URL = "https://alakeaccount.blob.core.windows.net/acontainer/AOS/reports/"
_LAKE_CLIENT_ID = "52b74947-0621-46e2-a523-a6b466f47c33"


@pytest.fixture(autouse=True)
def _sandbox_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """`build_app_env` refuses without a configured sandbox — the router's 503 gate runs first in
    production, so this is the configured path every publish actually takes."""
    from pydantic import SecretStr

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


@pytest.fixture
def lake_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings,
        "connector_lake",
        LakeConfig(
            url=_LAKE_URL,
            identity_client_id=_LAKE_CLIENT_ID,
            identity_resource_id="/subscriptions/s/resourcegroups/r/providers/p/id/an-identity",
        ),
    )


async def _publishable(
    db: AsyncSession,
    *,
    access: ConnectorRequestStatus | None = ConnectorRequestStatus.APPROVED,
    enabled: bool = True,
    email: str = "citizen@rvaiglobal.com",
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """`(user_id, project_id, app_id)` for a project with the connector in a chosen state."""
    user = await UserFactory.create(db, email=email)
    project = await ProjectFactory.create(db, user_id=user.id)
    app = await AppRegistryFactory.create(db, user_id=user.id, project_id=project.id)
    if access is not None:
        await seed_decision(db, user.id, access, None)
    db.add(
        ProjectConnector(
            project_id=project.id,
            connector_key=KEY,
            enabled=enabled,
            window_kind=ConnectorWindowKind.RELATIVE,
            window_days=7,
        )
    )
    await db.flush()
    return user.id, project.id, app.id


async def test_an_approved_project_publishes_with_the_coordinates(
    db_session: AsyncSession, lake_configured: None
) -> None:
    """★ THE SAME TWO VALUES THE BUILD GOT, so the app the citizen tested is the app that ships.
    Leaving them out would ship a pass where an app can read flight data while it is being built
    and not after it is published — which reads as a bug, not as a boundary."""
    user_id, project_id, app_id = await _publishable(db_session)

    env, _url = await build_published_env(
        db_session, app_id=app_id, project_id=project_id, user_id=user_id
    )

    url_name, client_id_name = connector_env_names(KEY)
    assert env[url_name] == _LAKE_URL
    assert env[client_id_name] == _LAKE_CLIENT_ID


async def test_no_window_dates_reach_a_published_app(
    db_session: AsyncSession, lake_configured: None
) -> None:
    """★ A deployed app is UNCAPPED by ruling, so a build-time window injected into it would be a
    limit that means nothing. It still inherits the connector's day-late ceiling, which is a fact
    about the data rather than a grant — and which the app derives from the lake's own listing."""
    user_id, project_id, app_id = await _publishable(db_session)

    env, _url = await build_published_env(
        db_session, app_id=app_id, project_id=project_id, user_id=user_id
    )

    assert not [name for name in env if "WINDOW" in name or "DAYS" in name]


@pytest.mark.parametrize(
    ("access", "enabled"),
    [
        (ConnectorRequestStatus.APPROVED, False),
        (ConnectorRequestStatus.PENDING, True),
        (ConnectorRequestStatus.DECLINED, True),
        (None, True),
    ],
    ids=["switched-off", "pending", "declined", "never-asked"],
)
async def test_an_ungranted_project_publishes_with_nothing_extra(
    db_session: AsyncSession,
    lake_configured: None,
    access: ConnectorRequestStatus | None,
    enabled: bool,
) -> None:
    """The gate, at the env-building level. Its twin — that the SPEC then carries no identity
    block — lives in `test_aca_publish.py`, because a test that only covered this half would
    pass while every published app on the platform carried the credential."""
    user_id, project_id, app_id = await _publishable(db_session, access=access, enabled=enabled)

    env, _url = await build_published_env(
        db_session, app_id=app_id, project_id=project_id, user_id=user_id
    )

    url_name, client_id_name = connector_env_names(KEY)
    assert url_name not in env
    assert client_id_name not in env


async def test_publishing_someone_elses_project_carries_no_coordinates(
    db_session: AsyncSession, lake_configured: None
) -> None:
    """★ `user_id` IS THE OWNERSHIP CLAIM. An approved stranger publishing this project — which
    the pipeline could only reach through a bug, and which is exactly why the predicate is here
    and not merely at the route — gets nothing."""
    _owner, project_id, app_id = await _publishable(db_session)
    stranger = await UserFactory.create(db_session, email="stranger@rvaiglobal.com")
    await seed_decision(db_session, stranger.id, ConnectorRequestStatus.APPROVED, None)
    await db_session.flush()

    env, _url = await build_published_env(
        db_session, app_id=app_id, project_id=project_id, user_id=stranger.id
    )

    url_name, _client_id_name = connector_env_names(KEY)
    assert url_name not in env


async def test_an_unconfigured_lake_publishes_normally(db_session: AsyncSession) -> None:
    """No lake is a supported deployment, and it must not fail a deploy. Binds no lake fixture:
    with one bound this branch is unreachable by construction."""
    user_id, project_id, app_id = await _publishable(db_session)

    assert settings.connector_lake is None
    env, _url = await build_published_env(
        db_session, app_id=app_id, project_id=project_id, user_id=user_id
    )

    assert env["BIAL_APP_ID"] == str(app_id)
    url_name, _client_id_name = connector_env_names(KEY)
    assert url_name not in env
