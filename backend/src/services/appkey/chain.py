"""The `X-App-Key` auth chain (R22, R23; ADR-0004) — the security spine of the
deployed-app data plane. Reproduces the Express `app-context.js` chain EXACTLY:

    require_app_key            resolve X-App-Key → verified app context
      · missing/unknown key                       → 401
      · status ∉ {draft,pending,approved}         → 403   (checked BEFORE the 404)
      · URL appId ≠ key's appId                    → 404   (fail-closed cross-check)
    require_login_if_required  live loginRequired re-read; verify the injected user
      · loginRequired & bad/absent Bearer          → 401
      · loginRequired false                        → allow, actor = None
    per_app_limiter            keyed on the VERIFIED appId (never IP)

Everything is validated against the verified `AppContext`, NEVER the request body.
The 403-before-404 ordering is load-bearing (a disabled app's key on a mismatched
URL is a 403, not a 404) — do not reorder. A dropped predicate here is a cross-app
leak, so every failure fails CLOSED with a stable `{"error": {"message"}}` body.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated

import sqlalchemy as sa
from fastapi import Depends, Request

from src.api.deps import DbSession
from src.core.errors import AppApiError
from src.db.models.app_registry import ACTIVE_STATUSES, AppRegistry, AppStatus
from src.db.models.user import User
from src.services.auth.errors import AuthError
from src.services.auth.session_jwt import decode_session_jwt
from src.services.ratelimit.limiter import rate_limit

_APP_KEY_HEADER = "x-app-key"
_BEARER_RE = re.compile(r"^Bearer (.+)$")


@dataclass(frozen=True, slots=True)
class AppContext:
    """The verified app the request is scoped to — the ONLY source of `app_id`
    downstream (never the request body). `login_required` and `status` are the LIVE
    values read this request from the registry row."""

    app_id: uuid.UUID
    login_required: bool
    status: AppStatus


async def require_app_key(app_id: uuid.UUID, request: Request, db: DbSession) -> AppContext:
    """Resolve `X-App-Key` → verified `AppContext`, fail-closed (401/403/404).

    `app_id` is the URL path segment; the header key resolves to its own app, and
    the two must match. Order is a security invariant: unknown key → 401; inactive
    status → 403; URL/key mismatch → 404 (403 BEFORE 404)."""
    app_key = request.headers.get(_APP_KEY_HEADER)
    if not app_key:
        raise AppApiError(401, "Missing or invalid app key.")

    app = (
        await db.execute(sa.select(AppRegistry).where(AppRegistry.app_key == app_key))
    ).scalar_one_or_none()
    if app is None:
        # Same body as a missing key — a valid-but-unregistered key leaks nothing.
        raise AppApiError(401, "Missing or invalid app key.")
    if app.status not in ACTIVE_STATUSES:
        # Disabled (kill-switch) / rejected — refused before the URL cross-check.
        raise AppApiError(403, "This app is not available.")
    if app_id != app.id:
        # A valid key used on another app's URL 404s (indistinguishable from a
        # non-existent app — no cross-app enumeration).
        raise AppApiError(404, "App not found.")

    return AppContext(app_id=app.id, login_required=app.login_required, status=app.status)


RequireAppKey = Annotated[AppContext, Depends(require_app_key)]


async def require_login_if_required(
    ctx: RequireAppKey, request: Request, db: DbSession
) -> User | None:
    """Enforce the app's LIVE `loginRequired` gate. When true, verify the injected
    Bearer token and return the live user (401 on any failure); when false, allow the
    request with no user (actor = None). Fail-closed — any error denies."""
    if not ctx.login_required:
        return None

    match = _BEARER_RE.match(request.headers.get("authorization", ""))
    if match is None:
        raise AppApiError(401, "Missing or malformed Authorization header")
    try:
        claims = decode_session_jwt(match.group(1).strip())
    except AuthError as exc:
        raise AppApiError(401, "Invalid or expired token") from exc

    user = await db.get(User, claims.user_id)
    # Unknown user or a revoked session (token_version bumped) → denied.
    if user is None or user.token_version != claims.token_version:
        raise AppApiError(401, "Invalid or expired token")
    return user


InjectedUser = Annotated[User | None, Depends(require_login_if_required)]


async def _app_limiter_key(ctx: RequireAppKey) -> str:
    """The per-app rate-limit bucket key — the VERIFIED appId (never the IP; all of
    BIAL shares one egress IP, so IP keying would collapse every app into one
    bucket). No null-safety by design: it depends on `require_app_key`, so a
    misordered mount fails loud rather than silently sharing one bucket."""
    return str(ctx.app_id)


def make_per_app_limiter(
    *, limit: int, window_seconds: float, message: str
) -> Callable[[str], Awaitable[None]]:
    """Build a per-app fixed-window rate-limit dependency keyed on the verified appId.
    Call ONCE at router import so its counter persists; the router wraps the result in
    `Depends(...)`. Each call owns its own counter (records/files vs parse do not share
    a bucket). Ordered after key resolution because its key depends on
    `require_app_key`."""
    return rate_limit(
        _app_limiter_key, limit=limit, window_seconds=window_seconds, message=message
    )
