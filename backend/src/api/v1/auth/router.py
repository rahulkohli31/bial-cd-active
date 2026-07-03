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
from typing import Literal
from urllib.parse import quote

import httpx
import sqlalchemy as sa
from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import CurrentUser, DbSession
from src.config import settings
from src.db.models.refresh_token import RefreshToken
from src.db.models.user import User
from src.services.auth.cookies import (
    REFRESH_COOKIE_PATH,
    cookie_secure,
    csrf_cookie_name,
    refresh_cookie_name,
    session_cookie_name,
)
from src.services.auth.csrf import issue_csrf_token, verify_csrf
from src.services.auth.errors import REASON_AUTH_FAILED, AuthError
from src.services.auth.oidc import get_oauth, validate_entra_token
from src.services.auth.refresh import hash_refresh_token, issue_new_family, rotate_refresh_token
from src.services.auth.session_jwt import decode_session_jwt, mint_session_jwt

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
async def login(request: Request, oauth: OAuth = Depends(get_oauth)) -> Response:
    # Generates state + nonce + PKCE verifier (stored in the oauth_transient
    # session cookie) and 302s to Entra. redirect_uri is the configured external
    # callback, byte-matching the Entra reply URL through the edge (KD-8).
    response: Response = await oauth.entra.authorize_redirect(request, settings.auth.redirect_uri)
    return response


@router.get("/callback", name="auth_callback")
async def callback(request: Request, db: DbSession, oauth: OAuth = Depends(get_oauth)) -> Response:
    try:
        token = await oauth.entra.authorize_access_token(request)
        identity = validate_entra_token(token)
    except OAuthError, httpx.HTTPError, ValueError:
        # Denied / cancelled consent (error=access_denied, no code) and other
        # provider-side errors (OAuthError), plus a transient httpx transport /
        # HTTP-status failure or malformed-JSON (ValueError, incl. JSONDecodeError)
        # reaching out to Entra's token/userinfo endpoints — all fail CLOSED to the
        # login bounce instead of escaping as a raw 500 (security.md).
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


def _csrf_ok(request: Request, user_id: uuid.UUID, token_version: int) -> bool:
    return verify_csrf(
        request.cookies.get(csrf_cookie_name(), ""),
        request.headers.get("x-csrf-token", ""),
        user_id,
        token_version,
    )


class RefreshResponse(BaseModel):
    """Body returned on a successful silent refresh."""

    status: Literal["refreshed"]


@router.post("/refresh")
async def refresh(request: Request, db: DbSession) -> Response:
    # There is NO valid session JWT at refresh time (it has expired) — the refresh
    # cookie is the credential. Read it from its path-scoped cookie.
    raw = request.cookies.get(refresh_cookie_name())
    if not raw:
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    presented_hash = hash_refresh_token(raw)

    # Resolve the owner FROM the refresh row so CSRF is checked against
    # SERVER-derived values, never a client-supplied user_id.
    user_id = await db.scalar(
        select(RefreshToken.user_id).where(RefreshToken.token_hash == presented_hash)
    )
    if user_id is None:
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    user = await db.get(User, user_id)
    if user is None:
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    if not _csrf_ok(request, user.id, user.token_version):
        return JSONResponse({"detail": "CSRF check failed"}, status_code=403)

    try:
        result = await rotate_refresh_token(db, presented_hash)
    except AuthError:
        # Reuse detection may have revoked the whole family — persist that revoke
        # BEFORE denying (a plain HTTPException would roll it back via get_db).
        await db.commit()
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    await db.commit()

    session_jwt = mint_session_jwt(
        result.user_id, result.token_version, settings.auth.access_ttl_seconds
    )
    csrf_token = issue_csrf_token(result.user_id, result.token_version)
    response = JSONResponse(RefreshResponse(status="refreshed").model_dump())
    _set_session_cookies(
        response,
        session_jwt=session_jwt,
        refresh_token=result.new_refresh_token,
        csrf_token=csrf_token,
    )
    return response


async def _user_from_session_cookie(
    request: Request, db: AsyncSession, *, verify_exp: bool = True
) -> User | None:
    """Best-effort identity from the session cookie (returns None instead of
    raising). Used by logout, which must proceed to CLEAR cookies even when there
    is no live session to identify.

    `verify_exp=False` accepts a validly-signed but EXPIRED session cookie so an
    idle-window logout can still resolve the owner FOR REVOCATION ONLY — never to
    authenticate (KD-6). Signature/alg and token_version checks stay intact."""
    token = request.cookies.get(session_cookie_name())
    if not token:
        return None
    try:
        claims = decode_session_jwt(token, verify_exp=verify_exp)
    except AuthError:
        return None
    user = await db.get(User, claims.user_id)
    if user is None or user.token_version != claims.token_version:
        return None
    return user


class LogoutResponse(BaseModel):
    """Body returned once logout has cleared the client cookies."""

    status: Literal["logged_out"]


@router.post("/logout")
async def logout(request: Request, db: DbSession) -> Response:
    # Logout must NOT hard-require a live session. The short session cookie may
    # already be gone after an idle period (its max-age is the access TTL), while
    # the path-scoped refresh cookie — which isn't even sent to this endpoint —
    # lingers. Requiring current_user here would 401 and skip BOTH the revoke and
    # the cookie clearing, leaving the refresh cookie able to silently re-mint the
    # session (a real logout-defeat / kiosk hazard).
    user = await _user_from_session_cookie(request, db)
    if user is None:
        # Idle past the ~15m access-TTL: the exp-checked lookup fails, yet the
        # session cookie (its max-age == the access TTL) may still ride along as a
        # validly-signed but EXPIRED JWT. Decode it expiry-blind — FOR REVOCATION
        # ONLY, never to authenticate — so a lapsed session still bumps
        # token_version and kills the refresh family instead of leaving a
        # captured refresh token live until the 8h absolute cap (Finding #7).
        user = await _user_from_session_cookie(request, db, verify_exp=False)
    if user is not None:
        # CSRF-gate the server-side revocation (a state change).
        if not _csrf_ok(request, user.id, user.token_version):
            return JSONResponse({"detail": "CSRF check failed"}, status_code=403)
        # Bump token_version (invalidates every live session JWT — KD-6) and revoke
        # all active refresh families. Idempotent: the revoke is a no-op once
        # revoked and the bump is monotonic.
        await db.execute(
            sa.update(User).where(User.id == user.id).values(token_version=User.token_version + 1)
        )
        await db.execute(
            sa.update(RefreshToken)
            .where(RefreshToken.user_id == user.id, RefreshToken.revoked.is_(False))
            .values(revoked=True)
        )
        await db.commit()

    # ALWAYS clear the client cookies — including the path-scoped refresh cookie via
    # a matching Set-Cookie deletion the browser applies regardless of the request
    # path — so the browser session is terminated and can no longer re-mint, even
    # when the user could not be identified for a server-side revoke.
    response = JSONResponse(LogoutResponse(status="logged_out").model_dump())
    _clear_session_cookies(response)
    return response
