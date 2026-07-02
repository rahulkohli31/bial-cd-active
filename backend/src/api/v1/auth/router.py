"""Auth HTTP endpoints — interactive sign-in (this unit), plus /me, /refresh,
/logout (U6/U7). The browser round-trip:

    GET /auth/login    -> 302 to Entra (PKCE state in the oauth_transient cookie)
    GET /auth/callback -> validate fail-closed, provision by oid, mint session,
                          set cookies, 302 to the SPA (or /login?authError=... )

The session/refresh/csrf cookies follow the KD-4 matrix and are ENVIRONMENT-AWARE:
`__Host-`/`__Secure-` prefixes + `Secure` in production, relaxed over plain http in
dev. `SameSite=Lax` (never Strict) so the top-level redirect back from Microsoft
carries them. The callback redirect_uri is the configured `AUTH__REDIRECT_URI`
(byte-matched through the /api-stripping edge), never `request.url_for` (KD-8).
"""

from __future__ import annotations

import uuid
from typing import Any
from urllib.parse import quote

import sqlalchemy as sa
from authlib.integrations.starlette_client import OAuthError
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.api.deps import CurrentUser, DbSession
from src.config import settings
from src.db.models.user import User
from src.services.auth.cookies import (
    REFRESH_COOKIE_PATH,
    cookie_secure,
    csrf_cookie_name,
    refresh_cookie_name,
    session_cookie_name,
)
from src.services.auth.csrf import issue_csrf_token
from src.services.auth.errors import REASON_AUTH_FAILED, AuthError
from src.services.auth.oidc import get_oauth, validate_entra_token
from src.services.auth.refresh import issue_new_family
from src.services.auth.session_jwt import mint_session_jwt

router = APIRouter(prefix="/auth", tags=["auth"])


# --- cookie helpers (private; the names/Secure decision live in services/auth/
# cookies.py, shared with the current_user dependency — ADR-0010) --------------


def _set_session_cookies(
    response: Response, *, session_jwt: str, refresh_token: str, csrf_token: str
) -> None:
    secure = cookie_secure()
    # session JWT — HttpOnly, root path (__Host- eligible), session-lived.
    response.set_cookie(
        session_cookie_name(),
        session_jwt,
        max_age=settings.auth.access_ttl_seconds,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )
    # refresh — HttpOnly, PATH-SCOPED to the refresh endpoint, and NO Domain
    # (host-only; __Secure- does not browser-enforce that, so the omission is the
    # guarantee — KD-4). Lives as long as the refresh token itself.
    response.set_cookie(
        refresh_cookie_name(),
        refresh_token,
        max_age=settings.auth.refresh_ttl_seconds,
        httponly=True,
        secure=secure,
        samesite="lax",
        path=REFRESH_COOKIE_PATH,
    )
    # csrf — readable by JS (NOT HttpOnly) so the SPA echoes it in X-CSRF-Token.
    # Lives as long as the refresh cookie so it is present for a silent refresh.
    response.set_cookie(
        csrf_cookie_name(),
        csrf_token,
        max_age=settings.auth.refresh_ttl_seconds,
        httponly=False,
        secure=secure,
        samesite="lax",
        path="/",
    )


def _clear_session_cookies(response: Response) -> None:
    secure = cookie_secure()
    response.delete_cookie(
        session_cookie_name(), path="/", secure=secure, httponly=True, samesite="lax"
    )
    response.delete_cookie(
        refresh_cookie_name(),
        path=REFRESH_COOKIE_PATH,
        secure=secure,
        httponly=True,
        samesite="lax",
    )
    response.delete_cookie(
        csrf_cookie_name(), path="/", secure=secure, httponly=False, samesite="lax"
    )


def _login_error_redirect(reason: str) -> RedirectResponse:
    # Fail closed: no session, no user row — bounce to the SPA login with a stable,
    # non-secret reason code (U8 maps it to banner copy).
    return RedirectResponse(
        f"{settings.FRONTEND_URL}/login?authError={quote(reason, safe='')}", status_code=302
    )


# --- endpoints -----------------------------------------------------------------


@router.get("/login")
async def login(request: Request, oauth: Any = Depends(get_oauth)) -> Response:
    # Generates state + nonce + PKCE verifier (stored in the oauth_transient
    # session cookie) and 302s to Entra. redirect_uri is the configured external
    # callback, byte-matching the Entra reply URL through the edge (KD-8).
    response: Response = await oauth.entra.authorize_redirect(request, settings.auth.redirect_uri)
    return response


@router.get("/callback", name="auth_callback")
async def callback(request: Request, db: DbSession, oauth: Any = Depends(get_oauth)) -> Response:
    try:
        token = await oauth.entra.authorize_access_token(request)
        identity = validate_entra_token(token)
    except OAuthError:
        # Denied / cancelled consent (error=access_denied, no code) and other
        # provider-side errors — no 500, just a fail-closed bounce.
        return _login_error_redirect(REASON_AUTH_FAILED)
    except AuthError as exc:
        # Wrong tenant (AE1) / invalid callback (AE4) — reason drives the banner.
        return _login_error_redirect(exc.reason)

    # Provision by the stable Entra oid (never email). Inlined upsert (ADR-0010):
    # a returning sign-in updates the mutable profile fields but PRESERVES
    # token_version (revocation state), and a brand-new row defaults it to 0.
    upsert = (
        pg_insert(User)
        .values(
            azure_oid=identity.oid,
            email=identity.email,
            upn=identity.upn,
            display_name=identity.display_name,
        )
        .on_conflict_do_update(
            index_elements=["azure_oid"],
            set_={
                "email": identity.email,
                "upn": identity.upn,
                "display_name": identity.display_name,
                "updated_at": sa.func.now(),
            },
        )
        .returning(User.id, User.token_version)
    )
    row = (await db.execute(upsert)).one()
    user_id, token_version = row.id, row.token_version

    raw_refresh = await issue_new_family(db, user_id)
    await db.commit()

    # The Entra tokens are discarded here — never stored, never reused as the app
    # session (R5). Our own short-lived session JWT is the session.
    session_jwt = mint_session_jwt(user_id, token_version, settings.auth.access_ttl_seconds)
    csrf_token = issue_csrf_token(user_id, token_version)

    response = RedirectResponse(settings.FRONTEND_URL, status_code=302)
    _set_session_cookies(
        response, session_jwt=session_jwt, refresh_token=raw_refresh, csrf_token=csrf_token
    )
    return response


class UserProfile(BaseModel):
    """The current user's public profile — no secrets, no upn/token_version."""

    id: uuid.UUID
    email: str
    display_name: str | None


@router.get("/me")
async def me(user: CurrentUser) -> UserProfile:
    # Authentication only (current_user) — no role/permission gate (RBAC deferred).
    return UserProfile(id=user.id, email=user.email, display_name=user.display_name)
