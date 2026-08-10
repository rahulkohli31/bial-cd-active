"""A1 — the sandbox login broker: signs a viewer into a generated app's `login_required`
gate through the SAME shared Entra app registration the portal uses, without touching the
generated app's own code.

    GET /auth/sandbox/login?app_id=<id>  -> 302 to Entra (app_id stashed in oauth_transient)
    GET /auth/sandbox/callback           -> validate fail-closed, hand off to the sandbox

Deliberately a SEPARATE router from `auth/router.py`: this flow has no portal session
cookie, no `CurrentUser`, no `User` upsert/suspension check — it only proves "a member of
the org's Entra tenant" (the same fail-closed `validate_entra_token` boundary the portal
uses) and hands that proof to the target sandbox via a short-lived signed token
(`sandbox_handoff.py`), keyed by that sandbox's own `SUPERVISOR_TOKEN`. The generated app
never sees Entra directly; the supervisor's `/auth/complete` (sandbox/supervisor/app.py)
is the far end of this handoff.

Uses the SAME registered Entra app as the portal (`oidc.py`'s `entra` client), on a SECOND
registered redirect URI (`settings.auth.sandbox_redirect_uri`) — one shared app
registration, two reply URLs, both on the same host so the Origin-header workaround in
`build_oauth()` needs no change.
"""

from __future__ import annotations

import uuid
from typing import Annotated
from urllib.parse import quote

import httpx
import structlog
from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import PlainTextResponse, RedirectResponse

from src.api.deps import DbSession
from src.api.v1.build_sessions.deps import OptionalSandbox, SessionManagerDep
from src.config import settings
from src.services.auth.errors import AuthError
from src.services.auth.oidc import get_oauth, validate_entra_token
from src.services.auth.sandbox_handoff import mint_sandbox_handoff_token

logger = structlog.get_logger()

router = APIRouter(prefix="/auth/sandbox", tags=["auth"])

OAuthClient = Annotated[OAuth, Depends(get_oauth)]

_SESSION_KEY = "sandbox_login_app_id"


@router.get("/login", name="auth_sandbox_login")
async def sandbox_login(request: Request, app_id: uuid.UUID, oauth: OAuthClient) -> Response:
    # Stashed in the SAME oauth_transient session cookie Authlib already uses for PKCE
    # state/nonce (main.py's SessionMiddleware) — no new cookie, no new storage.
    request.session[_SESSION_KEY] = str(app_id)
    response: Response = await oauth.entra.authorize_redirect(
        request, settings.auth.sandbox_redirect_uri
    )
    return response


@router.get("/callback", name="auth_sandbox_callback")
async def sandbox_callback(
    request: Request,
    db: DbSession,
    oauth: OAuthClient,
    manager: SessionManagerDep,
    sandbox_client: OptionalSandbox,
) -> Response:
    app_id_raw = request.session.pop(_SESSION_KEY, None)
    if not app_id_raw:
        # Expired/replayed callback (the oauth_transient cookie has a short max-age) — there
        # is no app to hand off to, so this can only ever be a plain error page, not a bounce.
        return PlainTextResponse(
            "Sign-in session expired. Please try again from the app.", status_code=400
        )
    try:
        app_id = uuid.UUID(app_id_raw)
    except ValueError:
        return PlainTextResponse("Invalid sign-in request.", status_code=400)

    try:
        token = await oauth.entra.authorize_access_token(request)
        identity = validate_entra_token(token)
    except (OAuthError, httpx.HTTPError, ValueError) as exc:  # fmt: skip  # py314 paren strip
        # Same fail-closed set `auth/router.py::callback` catches (denied consent, a
        # provider-side error, a transient httpx/JSON failure) — never a raw 500.
        logger.warning(
            "sandbox_auth_callback_failed", error_type=type(exc).__name__, detail=str(exc)[:500]
        )
        return PlainTextResponse("Sign-in failed. Please try again.", status_code=401)
    except AuthError as exc:
        # Wrong tenant / invalid callback — same fail-closed boundary as the portal login,
        # deliberately with no per-user account check beyond it (A1: tenant membership only).
        logger.warning("sandbox_auth_callback_denied", reason=exc.reason)
        return PlainTextResponse(
            "Sign-in was rejected: not a member of this organization.", status_code=403
        )

    if sandbox_client is None:
        return PlainTextResponse("The sandbox runtime is not configured.", status_code=503)

    handle = await manager.live_handle_for_app(db, app_id, sandbox_client)
    if handle is None:
        # The container isn't live (never built, or evicted) — nothing to hand off to.
        return PlainTextResponse(
            "This app isn't running right now. Reopen it from the portal and try again.",
            status_code=409,
        )

    logger.info("sandbox_login_completed", app_id=str(app_id), entra_oid=identity.oid)
    handoff = mint_sandbox_handoff_token(str(app_id), handle.token)
    return RedirectResponse(
        f"{handle.preview_url}_sup/auth/complete?token={quote(handoff, safe='')}",
        status_code=302,
    )
