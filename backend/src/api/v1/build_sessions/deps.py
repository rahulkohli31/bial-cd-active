"""DI seams for the control surface.

WHY THIS EXISTS
The sandbox client + session manager are resolved through FastAPI `Depends` so
`app.dependency_overrides` reach them in tests — the router threads the resolved objects into the
SessionManager rather than letting the manager call the deps inline (a plain in-service call would
bypass the overrides).

THE BRAIN SEAM IS GONE. `run_build_dependency` / `RunBuildDep` built a process-singleton
`BuildOrchestrator` and handed the router its bound `run_build`; its one consumer was
`start_build`, which is deleted — the bare `POST` on the build-sessions collection lost its
browser client. `_build_model` went with it and was never shared:
`conversations/_shared.py::chat_model` calls
`agent/model.py::build_foundry_model` directly, so the live chat path never routed through here.

Redis is the ONE exception and there is no `Depends` seam for it: the lock/heartbeat routes call
`get_redis()` LAZILY inside `build_coordination_or_503()`, because `get_redis()` raises on a
Redis-off deployment and an eagerly-solved dependency would raise before that seam — or the
route's own 404 — ever ran, turning the documented 503 into an undocumented 500. Nothing binds
Redis through DI; `fake_redis` binds the accessor singleton directly instead.

The frozen contract mandates signed double-submit CSRF on the mutating POSTs, a deliberate
divergence from the chat-relay precedent; the GETs are exempt. That gate now lives in
`src/api/deps_csrf.py` — the conversations domain is its second consumer — and is re-exported
here so this module stays the single dependency import for this domain's router.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from src.api.deps_csrf import RequireCsrf as RequireCsrf
from src.services.build_sessions import SessionManager, get_session_manager
from src.services.sandbox import SandboxClient, SandboxNotConfiguredError, get_sandbox


def sandbox_dependency() -> SandboxClient:
    """RAISES `SandboxNotConfiguredError` on a sandbox-off deployment — and because every
    `Depends` is solved BEFORE the route body's first statement, it raises where no `except` of
    the route's can reach it. Take this only where a missing sandbox genuinely IS a 500 (a deploy
    bug); a route that documents a sandbox-unavailable 503 takes `OptionalSandbox` below."""
    return get_sandbox()


def sandbox_or_none_dependency() -> SandboxClient | None:
    """The configured sandbox client, or `None` when unconfigured (dev/test) — the None-tolerant
    twin of `sandbox_dependency`, mirroring `OptionalStorage` in `src/api/deps.py`. It still
    resolves eagerly; it just cannot FAIL eagerly. `SandboxNotConfiguredError` subclasses
    `SandboxError`, so `relaunch_preview`'s `except (..., SandboxError) -> 503` would have
    caught it a frame later — instead it escaped eager resolution to the catch-all, answering an
    undocumented 500 with the wrong envelope. Sandbox-off is supported outside production
    (`_require_sandbox_in_production` only gates prod), so the break was live exactly where
    nobody watches."""
    try:
        return get_sandbox()
    except SandboxNotConfiguredError:
        return None


def session_manager_dependency() -> SessionManager:
    return get_session_manager()


SandboxDep = Annotated[SandboxClient, Depends(sandbox_dependency)]
# `| None`-tolerant, unlike `SandboxDep`: the consuming route maps an unset sandbox onto its
# own documented 503 instead of dying at dependency-solve time.
OptionalSandbox = Annotated[SandboxClient | None, Depends(sandbox_or_none_dependency)]
SessionManagerDep = Annotated[SessionManager, Depends(session_manager_dependency)]
