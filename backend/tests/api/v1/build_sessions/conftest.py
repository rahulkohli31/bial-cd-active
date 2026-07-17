"""Shared fixtures for the C3 router + SSE tests: cookie/CSRF auth, the dep-override
wiring (KTD-9), and a blocking brain that keeps a session live for the HTTP boundary."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import suppress
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from src.api.v1.build_sessions.deps import (
    sandbox_dependency,
    session_manager_dependency,
)
from src.api.v1.build_sessions.schemas import (
    BuildResult,
    BuildSessionStatus,
    StepEvent,
)
from src.config import settings
from src.db.models.user import User
from src.services.auth.csrf import issue_csrf_token
from src.services.auth.session_jwt import mint_session_jwt
from src.services.build_sessions import SessionManager
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
        app_data_base_url="https://platform.example/v1",
    )


def auth_headers(user: User, *, with_csrf: bool = True) -> dict[str, str]:
    jwt = mint_session_jwt(user.id, user.token_version, _TTL)
    if not with_csrf:
        return {"Cookie": f"session={jwt}"}
    csrf = issue_csrf_token(user.id, user.token_version)
    return {"Cookie": f"session={jwt}; csrf={csrf}", "X-CSRF-Token": csrf}


class BlockingBrain:
    """Emits one step then blocks until `release()` — keeps a session live across the
    HTTP request boundary so status / lock / stop tests aren't racing a fast completion.
    Emits no terminal `ended`: that frame is SESSION-API's alone (R7)."""

    def __init__(self) -> None:
        self._gate = asyncio.Event()

    def release(self) -> None:
        self._gate.set()

    async def __call__(self, session_id, user_id, sandbox_client, on_progress) -> BuildResult:
        await on_progress(StepEvent(seq=1, name="scaffold", label="Scaffolding", state="started"))
        await self._gate.wait()
        return BuildResult(
            status=BuildSessionStatus.ENDED,
            reason="completed",
            app_id=uuid.uuid4(),
            preview_url=None,
            last_seq=1,
            snapshot_committed=False,
        )


@pytest.fixture
def wire(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Configure the sandbox + override the manager/sandbox deps with fakes. The test
    sets its own `run_build_dependency` override (FakeBrain / BlockingBrain / None)."""
    monkeypatch.setattr(settings, "sandbox", _sandbox_config())
    manager = SessionManager()
    sbx = FakeSandboxClient()
    app.dependency_overrides[session_manager_dependency] = lambda: manager
    app.dependency_overrides[sandbox_dependency] = lambda: sbx
    return SimpleNamespace(app=app, manager=manager, sbx=sbx)


async def drain(manager: SessionManager, session_id: str) -> None:
    """Await a session's background task to a clean finish (test teardown helper). A
    stopped/force-ended task ends cancelled (CancelledError is BaseException, not
    Exception), so suppress both."""
    session = manager.get(uuid.UUID(session_id))
    if session is not None and session.task is not None:
        with suppress(asyncio.CancelledError, Exception):
            await session.task
