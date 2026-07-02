"""Shared FastAPI dependency aliases.

`DbSession` is the request-scoped async session. `current_user` is the
consumption seam for every protected endpoint: it authenticates a request purely
from the session cookie and returns the live `User`. This is AUTHENTICATION only
(who you are) — no role/permission check (RBAC is a later phase).
"""

from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.user import User
from src.db.session import get_db
from src.services.auth.cookies import session_cookie_name
from src.services.auth.errors import AuthError
from src.services.auth.session_jwt import decode_session_jwt

DbSession = Annotated[AsyncSession, Depends(get_db)]

# One generic 401 for every failure mode — a missing, malformed, expired, or
# revoked session are indistinguishable to the client (fail closed, no detail).
_UNAUTHENTICATED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Not authenticated",
    headers={"WWW-Authenticate": "Cookie"},
)


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
    # Unknown user, or a session revoked by a token_version bump (logout /
    # revocation) — the live DB value is the source of truth (KD-6).
    if user is None or user.token_version != claims.token_version:
        raise _UNAUTHENTICATED
    return user


CurrentUser = Annotated[User, Depends(current_user)]
