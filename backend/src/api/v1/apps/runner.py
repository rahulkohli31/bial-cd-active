"""Runner HTML serving (R20, R21) — mounted OUTSIDE the `/v1` API prefix (at `/apps`)
to match the current hosted-app URLs deployed apps already use. (The old single-file
`/preview` builder shell is retired — the Phase-2 live preview is a per-session
cross-origin sandbox frame, C8.) Serving is gated purely by app status (no owner auth
on the shell/frame): an
`approved` app, or a `pending` app still carrying its prior approved snapshot, is
served; everything else 404s.

Auth for a login-required app is minted, not read: the shell calls the cookie-authed
`POST /apps/{id}/runner-token` (P1) — the session cookie is HttpOnly, so the shell
cannot read a token; it fetches a fresh short-lived one and injects it into the frame.

Per-route CSP is set on each response; `X-Frame-Options: SAMEORIGIN` is applied for
these paths by the security-headers middleware (main.py) so the shell can same-origin
frame the sandboxed frame.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from src.api.deps import CurrentUser, DbSession
from src.api.v1.apps.runner_schemas import RunnerTokenResponse, RunnerUser
from src.core.errors import AppApiError
from src.db.models.app_registry import AppRegistry, AppStatus
from src.schemas import AUTH_401, DetailBody, ErrorEnvelope, error_responses
from src.services.appserving.csp import (
    build_frame_csp,
    build_shell_csp,
    origin_of,
)
from src.services.appserving.runner_token import mint_runner_token
from src.services.appserving.shells import (
    DATA_SERVICE_BASE_URL,
    render_frame,
    render_shell,
)

# runner mounts at the APP level (main.py), OUTSIDE `v1_router`, so the v1 500 default
# never reaches it — give it its own router-level 500 (`DetailBody`, the unhandled
# `{"detail"}` shape) so its S8415/500 coverage matches the v1 routes.
router = APIRouter(
    tags=["runner"],
    responses=error_responses((500, DetailBody, "Internal server error")),
)

# The "not available" bodies (Express verbatim): served for any non-serveable app.
_NOT_AVAILABLE_SHELL = (
    "<!doctype html><title>Not available</title><p>This app is not available.</p>"
)
_NOT_AVAILABLE_FRAME = "<!doctype html><title>Not available</title>"

# Statuses whose app is served (Express `isServeable`): approved, or pending still
# carrying a prior approved snapshot (keeps serving until re-approval).
_SERVEABLE_STATUSES = frozenset({AppStatus.APPROVED, AppStatus.PENDING})


def _approved_compiled(app: AppRegistry | None) -> str | None:
    """The serveable compiled artifact, or None if the app isn't serveable
    (Express `isServeable`: status ∈ {approved, pending} AND approvedSnapshot.compiled
    is a string)."""
    if app is None or app.status not in _SERVEABLE_STATUSES:
        return None
    snapshot = app.approved_snapshot or {}
    compiled = snapshot.get("compiled")
    return compiled if isinstance(compiled, str) else None


# The shell/frame 404s are RETURNED HTML (not raised JSON), so they are documented
# with a description only — no JSON body model would honestly describe the HTML body.
_HTML_404: dict[int | str, dict[str, Any]] = {404: {"description": "App not available (HTML)"}}


@router.get("/apps/{app_id}", response_class=HTMLResponse, responses=_HTML_404)
async def serve_shell(app_id: uuid.UUID, db: DbSession) -> HTMLResponse:
    """The same-origin host shell for an approved/pending app (404 otherwise)."""
    app = await db.get(AppRegistry, app_id)
    if _approved_compiled(app) is None or app is None:
        return HTMLResponse(_NOT_AVAILABLE_SHELL, status_code=404)
    config = {
        "appId": str(app.id),
        "appKey": app.app_key,
        "baseUrl": DATA_SERVICE_BASE_URL,
        "loginRequired": bool(app.login_required),
    }
    return HTMLResponse(
        render_shell(app.id, config),
        headers={"Content-Security-Policy": build_shell_csp()},
    )


@router.get("/apps/{app_id}/frame", response_class=HTMLResponse, responses=_HTML_404)
async def serve_frame(app_id: uuid.UUID, request: Request, db: DbSession) -> HTMLResponse:
    """The opaque-origin sandboxed frame serving the pre-compiled app JS."""
    app = await db.get(AppRegistry, app_id)
    compiled = _approved_compiled(app)
    if compiled is None:
        return HTMLResponse(_NOT_AVAILABLE_FRAME, status_code=404)
    return HTMLResponse(
        render_frame(compiled),
        headers={"Content-Security-Policy": build_frame_csp(origin_of(request))},
    )


@router.post(
    "/apps/{app_id}/runner-token",
    # Cookie-authed via `current_user` (401 DetailBody); raises its own 404 envelope.
    responses=error_responses(
        AUTH_401,
        (404, ErrorEnvelope, "App not found"),
    ),
)
async def runner_token(app_id: uuid.UUID, user: CurrentUser, db: DbSession) -> RunnerTokenResponse:
    """Mint a short-lived token for frame injection from the session cookie (P1).

    Cookie-authed (`current_user`); returns a FRESH token (never the cookie's own
    JWT/refresh) plus the display user. A missing/invalid cookie 401s (the shell then
    shows the sign-in prompt)."""
    app = await db.get(AppRegistry, app_id)
    if app is None:
        raise AppApiError(404, "App not found.")
    return RunnerTokenResponse(
        access_token=mint_runner_token(user.id, user.token_version),
        user=RunnerUser(id=user.id, email=user.email, display_name=user.display_name),
    )
