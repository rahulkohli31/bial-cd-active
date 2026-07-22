"""The C7 join — the REAL `run_build_dependency` (finding #1).

Unlike every other router test, `run_build_dependency` is NOT overridden here: with
`settings.foundry` configured the real dependency builds the real `BuildOrchestrator`
(session factory + KD-13 run-context provider included) and hands its bound `run_build` to
the router — POST /v1/build-sessions must NOT 503. Only the `_build_model` seam is swapped
for a scripted `FunctionModel` (no live Foundry call) and the sandbox for the in-memory
`FakeSandbox`, so the session deterministically drives to a terminal `ended`.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.v1.build_sessions import deps as deps_module
from src.api.v1.build_sessions.deps import (
    reset_run_build_for_tests,
    run_build_dependency,
    sandbox_dependency,
    sandbox_or_none_dependency,
)
from src.config import FoundryConfig, settings
from src.services.build_sessions import SessionManager, set_session_manager_for_tests
from src.services.sandbox.config import SandboxConfig
from tests.api.v1.build_sessions.conftest import auth_headers
from tests.factories import ProjectFactory, UserFactory
from tests.services.orchestrator.fake_sandbox import FakeSandbox
from tests.services.orchestrator.model_harness import scripted_model, text_turn, tool_turn


@pytest.fixture
def brain_wire(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    """Wire the REAL C7 join: Foundry configured, `_build_model` seam scripted, sandbox
    faked — and the PROCESS-singleton SessionManager (not a dep-override one), because the
    real dependency's run-context provider resolves the live session via
    `get_session_manager()`."""
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
    monkeypatch.setattr(
        settings,
        "foundry",
        FoundryConfig(resource="res", deployment="claude", api_key=SecretStr("k")),
    )
    manager = SessionManager()
    set_session_manager_for_tests(manager)
    sbx = FakeSandbox()
    sbx.dev_ready = True  # attach returns ready=True; `tsc` defaults green -> completed
    # Bind BOTH sandbox seams to one fake: routes documenting a sandbox 503 take the
    # None-tolerant `sandbox_or_none_dependency`; the rest keep the raising one.
    app.dependency_overrides[sandbox_dependency] = lambda: sbx
    app.dependency_overrides[sandbox_or_none_dependency] = lambda: sbx
    # THE SEAM (deps._build_model): only the Foundry model is swapped for a scripted
    # FunctionModel — orchestrator construction and the dependency itself stay real.
    monkeypatch.setattr(
        deps_module,
        "_build_model",
        lambda config: scripted_model(
            [tool_turn("declare_done", {"summary": "records app"}), text_turn()]
        ),
    )
    reset_run_build_for_tests()  # drop any engine cached under a different config
    yield SimpleNamespace(app=app, manager=manager, sbx=sbx)
    reset_run_build_for_tests()
    set_session_manager_for_tests(None)


async def test_real_run_build_dependency_drives_a_session_to_terminal_ended(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_redis: object,
    fake_storage: object,
    brain_wire: SimpleNamespace,
) -> None:
    assert run_build_dependency not in brain_wire.app.dependency_overrides  # the REAL dep
    user = await UserFactory.create(db_session, email="c7join@rvaiglobal.com")
    project = await ProjectFactory.create(db_session, user.id)

    resp = await client.post(
        "/v1/build-sessions",
        json={"projectId": str(project.id), "prompt": "build me a records app"},
        headers=auth_headers(user),
    )
    assert resp.status_code == 201  # NOT 503: the real dep returned a bound run_build
    sid = resp.json()["sessionId"]

    session = brain_wire.manager.get(uuid.UUID(sid))
    assert session is not None and session.task is not None
    await asyncio.wait_for(session.task, timeout=10)  # the scripted build runs to its end

    status = await client.get(f"/v1/build-sessions/{sid}", headers=auth_headers(user))
    assert status.status_code == 200
    body = status.json()
    assert body["status"] == "ended"  # terminal: the full BRAIN->SESSION-API loop closed
    assert body["previewUrl"] is not None  # preview_ready flowed through the real engine
    assert brain_wire.sbx.teardown_calls == 1  # SESSION-API (not BRAIN) tore down


async def test_real_dependency_is_none_without_foundry_and_caches_the_engine(
    brain_wire: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One engine per process (the model wraps a long-lived client): two resolutions hand
    # out the same bound run_build; unconfigured Foundry stays the router's 503 `None`.
    # The dependency is async (no awaits) so it resolves ON the event loop — the lazy
    # check-then-set is atomic, never threadpool-raced into a double construction.
    first = await deps_module.run_build_dependency()
    second = await deps_module.run_build_dependency()
    # Bound-method equality == same underlying orchestrator instance (`is` would compare
    # two fresh bound-method wrappers and always fail).
    assert first is not None and first == second
    reset_run_build_for_tests()
    monkeypatch.setattr(settings, "foundry", None)
    assert await deps_module.run_build_dependency() is None
