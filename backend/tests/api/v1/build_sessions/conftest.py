"""Shared fixtures for the build-session router tests: cookie/CSRF auth, the
dep-override wiring, and `a_live_session` — the one door left into a live in-process session."""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.v1.build_sessions.deps import (
    sandbox_dependency,
    sandbox_or_none_dependency,
    session_manager_dependency,
)
from src.config import settings
from src.db.models.user import User
from src.services.auth.csrf import issue_csrf_token
from src.services.auth.session_jwt import mint_session_jwt
from src.services.build_sessions import BuildSession, SessionManager, write_heartbeat
from src.services.redis import REGISTRY_STATE_READY, lock_key, registry_key
from src.services.redis.keys import (
    REGISTRY_FIELD_APP_NAME,
    REGISTRY_FIELD_CREATED_AT,
    REGISTRY_FIELD_FQDN,
    REGISTRY_FIELD_STATE,
    REGISTRY_FIELD_TOKEN_REF,
)
from src.services.sandbox.config import SandboxConfig
from tests.fakes import FakeSandboxClient

_TTL = settings.auth.access_ttl_seconds


def _sandbox_config() -> SandboxConfig:
    return SandboxConfig(
        subscription_id="s",
        resource_group="r",
        region="westeurope",
        managed_environment_name="aca-env",
        acr_server="acr.azurecr.io",
        acr_username="acr-user",
        acr_password=SecretStr("acr-pass"),
        image_ref="acr/img:latest",
    )


def auth_headers(user: User, *, with_csrf: bool = True) -> dict[str, str]:
    jwt = mint_session_jwt(user.id, user.token_version, _TTL)
    if not with_csrf:
        return {"Cookie": f"session={jwt}"}
    csrf = issue_csrf_token(user.id, user.token_version)
    return {"Cookie": f"session={jwt}; csrf={csrf}", "X-CSRF-Token": csrf}


async def a_live_session(
    wire: SimpleNamespace,
    db: AsyncSession,
    user: User,
    project_id: uuid.UUID,
    *,
    may_write: bool = True,
) -> BuildSession:
    """A live in-process session holding this project's container — THE door, packaged once.

    `ensure_sandbox` is the allocator production actually uses, and the ONLY one: nothing else
    claims the one-per-user slot or registers in `_active_by_user`.

    `may_write=True` by default because that is what the guards this package tests actually
    branch on — `_writing_session_holds` is what makes Save refuse — and a read-only default
    would silently turn those refusals into passes."""
    return await wire.manager.ensure_sandbox(
        db, user, project_id, sandbox_client=wire.sbx, may_write=may_write
    )


@pytest.fixture
def wire(app: FastAPI, db_session, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Configure the sandbox + override the manager/sandbox deps with fakes.

    The manager it binds is the one `a_live_session` above allocates against, so a test drives
    the same instance the routes resolve. (This fixture used to say the test sets its own
    `run_build_dependency` override; that seam is deleted along with the start route.)

    The manager's session factory is bound to the ROLLED-BACK test session: the manager opens
    its OWN session for work that outlives a request, so an unbound one would commit real rows
    into the test database and leak them across tests.
    """
    monkeypatch.setattr(settings, "sandbox", _sandbox_config())

    @contextlib.asynccontextmanager
    async def _session():
        yield db_session

    manager = SessionManager(session_factory=lambda: _session())
    sbx = FakeSandboxClient()
    app.dependency_overrides[session_manager_dependency] = lambda: manager
    # Both sandbox seams bound to one fake: routes documenting a sandbox 503 take the
    # None-tolerant `sandbox_or_none_dependency`, the rest the raising one — leaving either
    # unbound 503s a test that meant to reach the route.
    app.dependency_overrides[sandbox_dependency] = lambda: sbx
    app.dependency_overrides[sandbox_or_none_dependency] = lambda: sbx
    return SimpleNamespace(app=app, manager=manager, sbx=sbx)


class DeadRedis:
    """A Redis client where EVERY command raises `redis.exceptions.ConnectionError` — the
    shape a real outage takes at the call site once the client's bounded retry has spent its
    attempts. Bound in place of the client singleton (below) so `get_redis()` hands it to the
    manager exactly as it would a live client: the routes under test never learn they are
    talking to a stub, which is the point — the 503 has to come from the seam, not from the
    fixture.

    `__getattr__` rather than a list of methods, deliberately: a total outage does not pick
    and choose commands, and enumerating them would quietly stop covering any new one."""

    def __getattr__(self, name: str):
        async def the_store_is_gone(*args: object, **kwargs: object) -> object:
            raise RedisConnectionError(f"connection refused ({name})")

        return the_store_is_gone


@pytest.fixture
def dead_redis(monkeypatch: pytest.MonkeyPatch) -> DeadRedis:
    """`get_redis()` returns a client that answers nothing. Mutually exclusive with
    `fake_redis`: both bind the same client singleton, so a test taking both gets whichever
    bound last."""
    from src.services.redis import client as redis_client

    stub = DeadRedis()
    monkeypatch.setattr(redis_client, "_redis_singleton", stub)
    return stub


async def seed_live_sandbox_state(redis, user_id: uuid.UUID) -> None:
    """Make a user look like they already hold a live sandbox from ANOTHER process:
    registry + lock + heartbeat, which is the exact conjunction `reconcile_user` spares
    (its guard is an AND). Without all three the reconcile reaps the lock on the way in and
    the acquire succeeds, so a contention test seeded with the lock alone proves nothing."""
    await redis.hset(
        registry_key(user_id),
        mapping={
            REGISTRY_FIELD_APP_NAME: "sbx-someone-elses",
            REGISTRY_FIELD_FQDN: "live.example",
            REGISTRY_FIELD_TOKEN_REF: "ref",
            REGISTRY_FIELD_CREATED_AT: datetime.now(UTC).isoformat(),
            REGISTRY_FIELD_STATE: REGISTRY_STATE_READY,
        },
    )
    await redis.set(lock_key(user_id), "another-processes-token", ex=900)
    await write_heartbeat(redis, user_id)
