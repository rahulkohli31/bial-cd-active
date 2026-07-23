"""DI seams for the C3 control surface.

The brain + sandbox client + session manager are resolved through FastAPI `Depends` (KTD-9) so
`app.dependency_overrides` reach them in tests — the router threads the resolved objects into
the SessionManager rather than letting the manager call the deps inline (a plain in-service call
would bypass the overrides).

Redis is the ONE exception and there is no `Depends` seam for it: the lock/heartbeat routes call
`get_redis()` LAZILY inside `build_coordination_or_503()`, because `get_redis()` raises on a
Redis-off deployment and an eagerly-solved dependency would raise before that seam — or the
route's own 404 — ever ran, turning the documented 503 into an undocumented 500. The
`redis_dependency` / `RedisDep` pair that used to live here was deleted once its last consumer
moved into the seam; nothing binds Redis through DI, and `fake_redis` binds the accessor
singleton instead. See
`docs/solutions/design-patterns/eager-fastapi-depends-bypasses-in-body-error-seam-2026-07-21.md`.

C3 §3 mandates signed double-submit CSRF on the mutating POSTs (`start` / `stop` / all
lock ops / `internal/reap`), a deliberate divergence from the chat-relay precedent that
the frozen contract requires; the `status` GET and the SSE GET are exempt. That gate now
lives in `src/api/deps_csrf.py` — the conversations domain is its second consumer — and is
re-exported here so this module stays the C3 router's single dependency import.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends
from pydantic_ai.models import Model

from src.api.deps_csrf import RequireCsrf as RequireCsrf
from src.api.v1.build_sessions.schemas import RunBuild
from src.config import FoundryConfig, settings
from src.db.base import async_session_factory
from src.services.agent.model import build_foundry_model
from src.services.build_sessions import SessionManager, get_session_manager
from src.services.orchestrator import BuildOrchestrator, BuildSpec
from src.services.sandbox import SandboxClient, SandboxNotConfiguredError, get_sandbox


def sandbox_dependency() -> SandboxClient:
    """RAISES `SandboxNotConfiguredError` on a sandbox-off deployment — and because every
    `Depends` is solved BEFORE the route body's first statement, it raises where no `except` of
    the route's can reach it. Take this only where a missing sandbox genuinely IS a 500 (a deploy
    bug); a route that documents a sandbox-unavailable 503 takes `OptionalSandbox` below."""
    return get_sandbox()


def sandbox_or_none_dependency() -> SandboxClient | None:
    """The configured sandbox client, or **`None` when it is unconfigured** (dev/test) — the
    None-tolerant twin of `sandbox_dependency`, mirroring `OptionalStorage` in `src/api/deps.py`.

    It still resolves eagerly; it just cannot FAIL eagerly. `SandboxNotConfiguredError` subclasses
    `SandboxError`, so `relaunch_preview`'s `except (..., SandboxError) -> 503` *would* have caught
    it — one frame later. Resolved eagerly it escaped to the catch-all instead, and the route that
    advertises "The sandbox or build coordination is temporarily unavailable" answered an
    undocumented 500 with the wrong envelope. Sandbox-off is supported outside production
    (`_require_sandbox_in_production` only gates prod), so the break was live exactly where nobody
    watches. See
    `docs/solutions/design-patterns/eager-fastapi-depends-bypasses-in-body-error-seam-2026-07-21.md`."""
    try:
        return get_sandbox()
    except SandboxNotConfiguredError:
        return None


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
    if not session.attachments:
        # No attachments → a bare `str` prompt, byte-identical to the pre-R3 path.
        return BuildSpec(
            prompt=session.prompt,
            app_id=session.app_id,
            conversation_id=session.conversation_id,
        )
    # R3 — the multimodal prompt: each attachment's content FIRST (fenced office/csv text, or
    # `BinaryContent` for image/PDF vision), then the instruction text. Attachments-before-text
    # is Anthropic's documented vision ordering and matches the portal's own `buildContent`
    # ("text after files"), so the build path and the chat relay ground a model the same way —
    # the instruction reads as a question ABOUT the material above it, which is exactly what a
    # "build me an app from this spreadsheet" prompt means. The attachments were materialized
    # at start (`build_sessions/attachments.py`), so this is pure assembly — no I/O, nothing
    # that can fail here, and nothing that could silently drop a file this late.
    return BuildSpec(
        prompt=[*session.attachments, session.prompt],
        app_id=session.app_id,
        conversation_id=session.conversation_id,
    )


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
# `| None`-tolerant, unlike `SandboxDep`: the consuming route maps an unset sandbox onto its
# own documented 503 instead of dying at dependency-solve time.
OptionalSandbox = Annotated[SandboxClient | None, Depends(sandbox_or_none_dependency)]
SessionManagerDep = Annotated[SessionManager, Depends(session_manager_dependency)]
RunBuildDep = Annotated[RunBuild | None, Depends(run_build_dependency)]
