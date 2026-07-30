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
from typing import Annotated
from urllib.parse import quote

import httpx
import sqlalchemy as sa
import structlog
from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import CurrentUser, DbSession
from src.api.v1.auth.schemas import (
    LogoutResponse,
    ProfileLimits,
    RefreshResponse,
    UserProfile,
)
from src.config import settings
from src.db.models.refresh_token import RefreshToken
from src.db.models.user import User
from src.schemas import AUTH_401, DetailBody, error_responses
from src.services.auth.cookies import (
    REFRESH_COOKIE_PATH,
    cookie_secure,
    csrf_cookie_name,
    refresh_cookie_name,
    session_cookie_name,
)
from src.services.auth.csrf import issue_csrf_token, verify_csrf
from src.services.auth.errors import (
    REASON_ACCOUNT_SUSPENDED,
    REASON_AUTH_FAILED,
    AuthError,
)
from src.services.auth.oidc import get_oauth, validate_entra_token
from src.services.auth.refresh import (
    hash_refresh_token,
    issue_new_family,
    revoke_all_sessions,
    rotate_refresh_token,
)
from src.services.auth.session_jwt import decode_session_jwt, mint_session_jwt
from src.services.rbac.roles import is_super_duper_admin
from src.services.usage.limits import effective_limits_for

logger = structlog.get_logger()

router = APIRouter(prefix="/auth", tags=["auth"])

# Authlib OAuth registry, injected via Annotated (S8410) rather than a `= Depends`
# default so the signature stays call-safe and matches the DbSession/CurrentUser idiom.
OAuthClient = Annotated[OAuth, Depends(get_oauth)]


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
    response.set_cookie(  # NOSONAR(S3330) CSRF token must be JS-readable (see csrf.py)
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
async def login(request: Request, oauth: OAuthClient) -> Response:
    # Generates state + nonce + PKCE verifier (stored in the oauth_transient
    # session cookie) and 302s to Entra. redirect_uri is the configured external
    # callback, byte-matching the Entra reply URL through the edge (KD-8).
    response: Response = await oauth.entra.authorize_redirect(request, settings.auth.redirect_uri)
    return response


@router.get("/callback", name="auth_callback")
async def callback(request: Request, db: DbSession, oauth: OAuthClient) -> Response:
    try:
        token = await oauth.entra.authorize_access_token(request)
        identity = validate_entra_token(token)
    except (OAuthError, httpx.HTTPError, ValueError) as exc:  # fmt: skip  # py314 paren strip
        # Denied / cancelled consent (error=access_denied, no code) and other
        # provider-side errors (OAuthError), plus a transient httpx transport /
        # HTTP-status failure or malformed-JSON (ValueError, incl. JSONDecodeError)
        # reaching out to Entra's token/userinfo endpoints — all fail CLOSED to the
        # login bounce instead of escaping as a raw 500 (security.md).
        # Log the exception CLASS + provider message (never a credential — Authlib's
        # OAuthError / httpx str carry the AADSTS code or transport error, not the
        # client secret or the token POST body) so an operator can distinguish a
        # network-reach failure (httpx.*Error) from a Microsoft rejection such as
        # invalid_client / AADSTS7000215 (OAuthError) or a lost-state cookie
        # (mismatching_state) — otherwise every callback failure collapses to one
        # indistinguishable `authError=auth_failed` bounce with no root cause.
        logger.warning(
            "auth_callback_failed", error_type=type(exc).__name__, detail=str(exc)[:500]
        )
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
        .returning(User.id, User.token_version, User.suspended_at)
    )
    row = (await db.execute(upsert)).one()
    user_id, token_version = row.id, row.token_version

    if row.suspended_at is not None:
        # Suspension seam 1 of 3 (R11, KD-6): a suspended user authenticates fine at
        # Entra but gets NO local session — bounce to the login banner. Nothing is
        # committed (get_db rolls the profile touch back), so the refused sign-in
        # leaves no trace beyond this log line.
        logger.warning("suspended_user_rejected", user_id=str(user_id), seam="login_callback")
        return _login_error_redirect(REASON_ACCOUNT_SUSPENDED)

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


@router.get(
    "/me",
    # `/me` returns the model directly, so response_model is ENFORCED. The inherited
    # `current_user` 401 is the DetailBody shape (auth is NOT migrated to AppApiError).
    responses=error_responses(AUTH_401),
)
async def me(user: CurrentUser, db: DbSession) -> UserProfile:
    # Authentication + a derived superadmin hint (same allowlist check the admin gate uses, so the
    # SPA and the backend agree on who is an admin) + the user's effective limits via the shared
    # resolver, so an admin's override reaches the client. The actual gates stay server-side.
    daily, soft, hard = await effective_limits_for(db, user.id)
    return UserProfile(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        is_admin=is_super_duper_admin(user, settings.superadmin_emails),
        limits=ProfileLimits(
            daily_token_limit=daily, context_soft_limit=soft, context_hard_limit=hard
        ),
    )


def _csrf_ok(request: Request, user_id: uuid.UUID, token_version: int) -> bool:
    return verify_csrf(
        request.cookies.get(csrf_cookie_name(), ""),
        request.headers.get("x-csrf-token", ""),
        user_id,
        token_version,
    )


@router.post(
    "/refresh",
    # Returns a JSONResponse (for Set-Cookie) so the 200 model is documented-only, NOT
    # enforced. 401/403 keep the hand-built `{"detail"}` shape (R11 — auth carve-out).
    responses=error_responses(
        (200, RefreshResponse, "Session refreshed"),
        AUTH_401,
        (403, DetailBody, "CSRF check failed"),
    ),
)
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
    if user.suspended_at is not None:
        # Suspension seam 3 of 3 (R11, KD-6): deactivation already revoked the
        # family, so this is belt-and-suspenders — but rotation must never re-mint
        # for a suspended user even if a family somehow survived. Same generic 401
        # as every other refresh failure (no state disclosure at this endpoint).
        logger.warning("suspended_user_rejected", user_id=str(user.id), seam="refresh")
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


@router.post(
    "/logout",
    # JSONResponse (for cookie clearing) → documented-only 200. Logout never 401s (it
    # proceeds even without a live session); only the CSRF 403 is a non-2xx path.
    responses=error_responses(
        (200, LogoutResponse, "Logged out"),
        (403, DetailBody, "CSRF check failed"),
    ),
)
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
        # all active refresh families (shared with admin deactivation, U10).
        await revoke_all_sessions(db, user.id)
        await db.commit()

    # ALWAYS clear the client cookies — including the path-scoped refresh cookie via
    # a matching Set-Cookie deletion the browser applies regardless of the request
    # path — so the browser session is terminated and can no longer re-mint, even
    # when the user could not be identified for a server-side revoke.
    response = JSONResponse(LogoutResponse(status="logged_out").model_dump())
    _clear_session_cookies(response)
    return response
