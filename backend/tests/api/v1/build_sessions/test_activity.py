"""`GET /v1/build-sessions/activity` — one user-scoped read answering which of a citizen's
projects are starting, open right now, or closing down, and when one is near its ceiling.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from httpx import AsyncClient
from pydantic import SecretStr
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.ext.asyncio import AsyncSession

import src.api.v1.build_sessions.router as router_module
from src.config import settings
from src.db.models.pending_teardown import PendingTeardown
from src.db.models.project import Project
from src.services.build_sessions.appdata import resolve_app_for_project
from src.services.build_sessions.manager import app_name_for
from src.services.redis import REGISTRY_STATE_READY, registry_key
from src.services.redis.keys import (
    REGISTRY_FIELD_APP_NAME,
    REGISTRY_FIELD_CREATED_AT,
    REGISTRY_FIELD_FQDN,
    REGISTRY_FIELD_SERVING_SINCE,
    REGISTRY_FIELD_STATE,
    REGISTRY_FIELD_TOKEN_REF,
    starting_key,
)
from src.services.sandbox.config import SandboxConfig
from tests.api.v1.build_sessions.conftest import auth_headers
from tests.factories import ProjectFactory, UserFactory

_UNPROVEN = ""  # the registry's own sentinel for "exists, never served"


def _sandbox_config(**overrides: Any) -> SandboxConfig:
    data: dict[str, Any] = {
        "subscription_id": "s",
        "resource_group": "r",
        "region": "westeurope",
        "managed_environment_name": "aca-env",
        "acr_server": "acr.azurecr.io",
        "acr_username": "acr-user",
        "acr_password": SecretStr("acr-pass"),
        "image_ref": "acr/img:latest",
    }
    data.update(overrides)
    return SandboxConfig(**data)


async def _user_project_app(db: AsyncSession, email: str):
    user = await UserFactory.create(db, email=email)
    project = await ProjectFactory.create(db, user.id)
    app_id = await resolve_app_for_project(db, user.id, project.id)
    await db.commit()
    return user, project, app_id


async def _register(
    redis,
    user_id: uuid.UUID,
    app_name: str,
    *,
    serving_since: str,
    created_at: datetime | None = None,
) -> None:
    await redis.hset(
        registry_key(user_id),
        mapping={
            REGISTRY_FIELD_APP_NAME: app_name,
            REGISTRY_FIELD_FQDN: f"{app_name}.example.azurecontainerapps.io",
            REGISTRY_FIELD_TOKEN_REF: f"ref-{app_name}",
            REGISTRY_FIELD_CREATED_AT: (created_at or datetime.now(UTC)).isoformat(),
            REGISTRY_FIELD_STATE: REGISTRY_STATE_READY,
            REGISTRY_FIELD_SERVING_SINCE: serving_since,
        },
    )


async def _write_owed_row(
    db: AsyncSession, *, user_id: uuid.UUID, app_id: uuid.UUID, project_id: uuid.UUID
) -> PendingTeardown:
    row = PendingTeardown(
        user_id=user_id,
        app_id=app_id,
        app_name=app_name_for(app_id),
        project_id=project_id,
        instance_ref=datetime.now(UTC) - timedelta(minutes=5),
        claimed_until=datetime.now(UTC) + timedelta(minutes=1),
    )
    db.add(row)
    await db.flush()
    await db.commit()
    return row


async def _activity(client: AsyncClient, user) -> dict[str, Any]:
    resp = await client.get("/v1/build-sessions/activity", headers=auth_headers(user))
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


def _by_project(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {entry["projectId"]: entry for entry in body["projects"]}


# --- the shape: not shadowed by /{session_id} ---------------------------------


async def test_activity_is_not_shadowed_by_the_session_id_route(
    client: AsyncClient, db_session: AsyncSession, fake_redis
) -> None:
    """`/activity` must be declared above `GET /{session_id}` in the router, or `session_id`
    parses "activity" as a UUID and 422s. A real request against the mounted app is the only
    thing that actually proves the registration order, not a read of the source."""
    user = await UserFactory.create(db_session, email="shadow@bial.test")
    await db_session.commit()

    resp = await client.get("/v1/build-sessions/activity", headers=auth_headers(user))

    assert resp.status_code == 200, resp.text


# --- the happy paths -----------------------------------------------------------


async def test_a_starting_container_reads_starting(
    client: AsyncClient, db_session: AsyncSession, fake_redis
) -> None:
    """A start in flight, named only by the marker — the container itself does not exist yet,
    so there is no registry hash and no age to bound."""
    user, project, _ = await _user_project_app(db_session, "starting@bial.test")
    await fake_redis.set(starting_key(user.id), str(project.id), ex=300)

    body = await _activity(client, user)

    entries = _by_project(body)
    assert entries[str(project.id)]["phase"] == "starting"
    assert entries[str(project.id)]["drainingAt"] is None


async def test_a_registered_but_unproven_container_also_reads_starting(
    client: AsyncClient, db_session: AsyncSession, fake_redis
) -> None:
    """The container exists (the registry names it, `state=ready`) but nothing has watched it
    answer a request yet — the same arm `PreviewLifeState.STARTING` covers for one project."""
    user, project, app_id = await _user_project_app(db_session, "unproven@bial.test")
    await _register(fake_redis, user.id, app_name_for(app_id), serving_since=_UNPROVEN)

    body = await _activity(client, user)

    entries = _by_project(body)
    assert entries[str(project.id)]["phase"] == "starting"


async def test_a_serving_container_and_an_owed_row_read_open_and_closing(
    client: AsyncClient, db_session: AsyncSession, fake_redis
) -> None:
    """The whole point of the read in one scenario: the project holding the workspace right
    now reads `open`, and a DIFFERENT project the platform still owes a deletion for reads
    `closing` — from the one response, with no container call for either."""
    user, open_project, open_app_id = await _user_project_app(db_session, "open@bial.test")
    closing_project = await ProjectFactory.create(db_session, user.id)
    closing_app_id = await resolve_app_for_project(db_session, user.id, closing_project.id)
    await db_session.commit()

    await _register(
        fake_redis,
        user.id,
        app_name_for(open_app_id),
        serving_since=datetime.now(UTC).isoformat(),
    )
    await _write_owed_row(
        db_session, user_id=user.id, app_id=closing_app_id, project_id=closing_project.id
    )

    body = await _activity(client, user)

    entries = _by_project(body)
    assert entries[str(open_project.id)]["phase"] == "open"
    assert entries[str(closing_project.id)]["phase"] == "closing"
    assert entries[str(closing_project.id)]["drainingAt"] is None


async def test_an_open_container_carries_its_drain_mark_when_the_ceiling_is_on(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_redis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`drainingAt` comes from the registry's own `created_at`, the same age source the read's
    fixed budget allows (no ARM tag call) — and is null the moment the flag is off."""
    monkeypatch.setattr(
        settings, "sandbox", _sandbox_config(drain_enabled=True, drain_after_hours=2)
    )
    user, project, app_id = await _user_project_app(db_session, "ceiling@bial.test")
    created_at = datetime.now(UTC) - timedelta(minutes=10)
    await _register(
        fake_redis,
        user.id,
        app_name_for(app_id),
        serving_since=datetime.now(UTC).isoformat(),
        created_at=created_at,
    )

    body = await _activity(client, user)

    draining_at = _by_project(body)[str(project.id)]["drainingAt"]
    assert draining_at is not None
    expected = created_at + timedelta(hours=2)
    assert abs((datetime.fromisoformat(draining_at) - expected).total_seconds()) < 5


async def test_the_drain_mark_is_absent_with_the_ceiling_off(
    client: AsyncClient, db_session: AsyncSession, fake_redis
) -> None:
    """No sandbox config is bound by this file's fixtures — `settings.sandbox` is `None`, the
    ordinary reading outside `wire` — so the ceiling must read as off, never as a guess."""
    user, project, app_id = await _user_project_app(db_session, "noceiling@bial.test")
    await _register(
        fake_redis, user.id, app_name_for(app_id), serving_since=datetime.now(UTC).isoformat()
    )

    body = await _activity(client, user)

    assert _by_project(body)[str(project.id)]["drainingAt"] is None


# --- edge cases ------------------------------------------------------------------


async def test_nothing_running_and_nothing_owed_is_an_empty_list(
    client: AsyncClient, db_session: AsyncSession, fake_redis
) -> None:
    """A 200 with nothing in it, not a 404 — the citizen has projects, none of them are doing
    anything right now."""
    user = await UserFactory.create(db_session, email="quiet@bial.test")
    await ProjectFactory.create(db_session, user.id)
    await db_session.commit()

    body = await _activity(client, user)

    assert body["projects"] == []


async def test_an_owed_row_for_a_deleted_project_is_omitted(
    client: AsyncClient, db_session: AsyncSession, fake_redis
) -> None:
    """`PendingTeardown` carries no `ForeignKey` on `project_id` by design — it must outlive a
    citizen deleting the outgoing project mid-wait — so a reader has to tolerate the id
    resolving to nothing, and this read's answer is to leave that project off the list."""
    user, project, app_id = await _user_project_app(db_session, "deleted@bial.test")
    await _write_owed_row(db_session, user_id=user.id, app_id=app_id, project_id=project.id)
    await db_session.execute(sa.delete(Project).where(Project.id == project.id))
    await db_session.commit()

    body = await _activity(client, user)

    assert body["projects"] == []


# --- the error path --------------------------------------------------------------


async def test_an_unreadable_coordination_store_answers_503_never_an_empty_list(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_redis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A store that will not answer is a 503, not the `[]` a client would read as "nothing is
    happening" and use to clear every marker it is currently showing."""
    user, _project, app_id = await _user_project_app(db_session, "outage@bial.test")
    await _register(
        fake_redis, user.id, app_name_for(app_id), serving_since=datetime.now(UTC).isoformat()
    )

    async def _refuse(*_args: object, **_kwargs: object) -> None:
        raise RedisConnectionError("coordination store is gone")

    # The route reads through the pipelined `read_registry_and_starting_marker`, not a bare
    # `hgetall`: patching a plain command would leave the pipeline it never calls perfectly
    # able to answer.
    monkeypatch.setattr(router_module, "read_registry_and_starting_marker", _refuse)

    resp = await client.get("/v1/build-sessions/activity", headers=auth_headers(user))

    assert resp.status_code == 503


# --- cross-user isolation ---------------------------------------------------------


async def test_a_citizen_never_sees_another_citizens_activity(
    client: AsyncClient, db_session: AsyncSession, fake_redis
) -> None:
    owner, owner_project, owner_app_id = await _user_project_app(db_session, "mine@bial.test")
    stranger, stranger_project, stranger_app_id = await _user_project_app(
        db_session, "theirs@bial.test"
    )
    await _register(
        fake_redis,
        owner.id,
        app_name_for(owner_app_id),
        serving_since=datetime.now(UTC).isoformat(),
    )
    await _register(
        fake_redis,
        stranger.id,
        app_name_for(stranger_app_id),
        serving_since=datetime.now(UTC).isoformat(),
    )
    await fake_redis.set(starting_key(stranger.id), str(stranger_project.id), ex=300)
    await _write_owed_row(
        db_session,
        user_id=stranger.id,
        app_id=stranger_app_id,
        project_id=stranger_project.id,
    )

    body = await _activity(client, owner)

    entries = _by_project(body)
    assert str(owner_project.id) in entries
    assert str(stranger_project.id) not in entries
