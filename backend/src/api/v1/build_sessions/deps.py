"""DI seams + the reusable CSRF dependency for the C3 control surface.

The brain + sandbox client + redis + session manager are resolved through FastAPI
`Depends` (KTD-9) so `app.dependency_overrides` reach them in tests — the router threads
the resolved objects into the SessionManager rather than letting the manager call the
deps inline (a plain in-service call would bypass the overrides).

`RequireCsrf` wraps the existing `verify_csrf` primitive (KTD-4): C3 §3 mandates signed
double-submit CSRF on the mutating POSTs (`start` / `stop` / all lock ops / `internal/reap`),
a deliberate divergence from the chat-relay precedent that the frozen contract requires.
The `status` GET and the SSE GET are exempt.
"""

from __future__ import annotations

import uuid
from typing import Annotated

import redis.asyncio as aioredis
from fastapi import Depends, Request
from pydantic_ai.models import Model

from src.api.deps import CurrentUser
from src.api.v1.build_sessions.schemas import RunBuild
from src.config import FoundryConfig, settings
from src.core.errors import AppApiError
from src.db.base import async_session_factory
from src.services.agent.model import build_foundry_model
from src.services.auth.cookies import csrf_cookie_name
from src.services.auth.csrf import verify_csrf
from src.services.build_sessions import SessionManager, get_session_manager
from src.services.orchestrator import BuildOrchestrator, BuildSpec
from src.services.redis import get_redis
from src.services.sandbox import SandboxClient, get_sandbox


def sandbox_dependency() -> SandboxClient:
    return get_sandbox()


def redis_dependency() -> aioredis.Redis:
    return get_redis()


def session_manager_dependency() -> SessionManager:
    return get_session_manager()


# The BRAIN engine is built once per process (the Foundry model wraps a long-lived httpx
# client); `run_build_dependency` hands out its bound `run_build`.
_orchestrator: BuildOrchestrator | None = None


def _build_model(config: FoundryConfig) -> Model:
    """The Foundry-backed Pydantic AI model — the exact chat-relay wiring
    (`claude/router.py::chat_model`). A named seam so the C7 integration test can swap in
    a scripted `FunctionModel` while still exercising the REAL dependency below."""
    return build_foundry_model(config)


async def _live_session_spec(session_id: uuid.UUID) -> BuildSpec:
    """The KD-13 run-context provider: resolve the LIVE session's prompt + app_id from the
    in-process SessionManager — the same singleton the router registered the session in,
    keyed by `session_id` from the manager's own store (already user-validated at start),
    so no DB lookup and no new scoping surface. FAILS CLOSED on a missing session; the
    harness funnels the raise to an `internal_error` escalation (KD-12)."""
    session = get_session_manager().get(session_id)
    if session is None:
        raise LookupError(f"no live build session {session_id} for run-context resolution")
    return BuildSpec(prompt=session.prompt, app_id=session.app_id)


async def run_build_dependency() -> RunBuild | None:
    """The BRAIN entry point (the C7 join). Foundry configured → the real
    `BuildOrchestrator`'s bound `run_build`; unconfigured → `None`, which the router maps
    to 503 BEFORE touching Redis or the lock (KTD-9), so a misconfigured brain never leaks
    a lock. Tests override this dependency with the mock brain.

    Deliberately `async` despite having no awaits: FastAPI runs async deps on the event
    loop (a sync `def` goes to the threadpool), so the check-then-set below is atomic and
    "built once per process" holds — two concurrent first requests can never
    double-construct the orchestrator (orphaning an unclosed httpx client)."""
    global _orchestrator
    if settings.foundry is None:
        return None
    if _orchestrator is None:
        _orchestrator = BuildOrchestrator(
            model=_build_model(settings.foundry),
            session_factory=async_session_factory,
            run_context_provider=_live_session_spec,
        )
    return _orchestrator.run_build


def reset_run_build_for_tests() -> None:
    """Drop the cached orchestrator so a test that reconfigures Foundry (or swaps the
    model seam) never reuses a stale engine."""
    global _orchestrator
    _orchestrator = None


SandboxDep = Annotated[SandboxClient, Depends(sandbox_dependency)]
RedisDep = Annotated[aioredis.Redis, Depends(redis_dependency)]
SessionManagerDep = Annotated[SessionManager, Depends(session_manager_dependency)]
RunBuildDep = Annotated[RunBuild | None, Depends(run_build_dependency)]


async def require_csrf(user: CurrentUser, request: Request) -> None:
    """Signed double-submit CSRF check on a mutating POST (ADR-0007). Fails closed with
    the data-plane envelope (distinct from the chat relay, which uses none)."""
    if not verify_csrf(
        request.cookies.get(csrf_cookie_name(), ""),
        request.headers.get("x-csrf-token", ""),
        user.id,
        user.token_version,
    ):
        raise AppApiError(403, "CSRF check failed.", code="csrf_failed")


RequireCsrf = Depends(require_csrf)
