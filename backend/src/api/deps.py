"""Shared FastAPI dependency aliases.

`DbSession` is the request-scoped async session. `current_user` is the
consumption seam for every protected endpoint: it authenticates a request purely
from the session cookie and returns the live `User`. This is AUTHENTICATION only
(who you are) — no role/permission check (RBAC is a later phase).
"""

from typing import Annotated

import structlog
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.user import User
from src.db.session import get_db
from src.services.auth.cookies import session_cookie_name
from src.services.auth.errors import AuthError
from src.services.auth.session_jwt import decode_session_jwt

logger = structlog.get_logger()

DbSession = Annotated[AsyncSession, Depends(get_db)]

# One generic 401 for every failure mode — a missing, malformed, expired, or
# revoked session are indistinguishable to the client (fail closed, no detail).
_UNAUTHENTICATED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Not authenticated",
    headers={"WWW-Authenticate": "Cookie"},
)

# Suspension is the ONE distinguishable failure: the caller proved who they are but
# a super-admin blocked the account (R11). A 403 tells the SPA to stop silently
# refreshing (which a 401 would trigger) and surface the state instead.
_SUSPENDED = HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account suspended")

# A never-approved user is authenticated but not yet authorized — distinguishable
# from suspension so the SPA can render an awaiting-approval screen instead of the
# suspension banner. /auth/me is exempted so the SPA can actually LEARN it's
# pending (this seam would otherwise 403 every request including its own status
# check). /auth/logout is NOT exempted here — it never calls current_user at all
# (see its own docstring), so there is nothing to exempt it from.
_PENDING_EXEMPT_PATHS = {"/v1/auth/me"}
_PENDING = HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Pending approval")


async def current_user(request: Request, db: DbSession) -> User:
    """Authenticate from the session cookie; return the live `User` or raise 401.

    Translates `AuthError` into `HTTPException(401)` ITSELF — the composition-root
    catch-all (`add_exception_handler(Exception, ...)`) would otherwise turn an
    uncaught `AuthError` into a generic 500, not a 401 (see core/errors.py)."""
    token = request.cookies.get(session_cookie_name())
    if not token:
        raise _UNAUTHENTICATED
    try:
        claims = decode_session_jwt(token)
    except AuthError as exc:
        raise _UNAUTHENTICATED from exc

    user = await db.get(User, claims.user_id)
    # An unknown user can't be suspended and carries no token_version to compare — 401.
    if user is None:
        raise _UNAUTHENTICATED
    if user.suspended_at is not None:
        # Suspension seam 2 of 3 (R11, KD-6): checked BEFORE the token_version gate on
        # purpose. Deactivation bumps token_version AND sets suspended_at, so a suspended
        # user's live JWT is genuinely stale — checking token_version first would 401 them
        # and the SPA would silently refresh instead of surfacing the suspension. This is not
        # a weakening of revocation: a token-stale-but-NOT-suspended user (logout, reactivated
        # old session) still falls through to the 401 below.
        logger.warning("suspended_user_rejected", user_id=str(user.id), seam="current_user")
        raise _SUSPENDED
    # A session revoked by a token_version bump (logout / revocation) with no suspension —
    # the live DB value is the source of truth (KD-6).
    if user.approved_at is None and request.url.path not in _PENDING_EXEMPT_PATHS:
        logger.warning("pending_user_rejected", user_id=str(user.id), seam="current_user")
        raise _PENDING
    if user.token_version != claims.token_version:
        raise _UNAUTHENTICATED
    return user


CurrentUser = Annotated[User, Depends(current_user)]
