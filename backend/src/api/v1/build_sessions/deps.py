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

from typing import Annotated

import redis.asyncio as aioredis
from fastapi import Depends, Request

from src.api.deps import CurrentUser
from src.api.v1.build_sessions.schemas import RunBuild
from src.core.errors import AppApiError
from src.services.auth.cookies import csrf_cookie_name
from src.services.auth.csrf import verify_csrf
from src.services.build_sessions import SessionManager, get_session_manager
from src.services.redis import get_redis
from src.services.sandbox import SandboxClient, get_sandbox


def sandbox_dependency() -> SandboxClient:
    return get_sandbox()


def redis_dependency() -> aioredis.Redis:
    return get_redis()


def session_manager_dependency() -> SessionManager:
    return get_session_manager()


def run_build_dependency() -> RunBuild | None:
    """The BRAIN entry point. `None` until Track BRAIN plugs its real orchestrator in at
    the C7 join — the router maps `None` → 503 BEFORE touching Redis or the lock (KTD-9),
    so a misconfigured brain never leaks a lock. Tests override this with the mock brain."""
    return None


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
