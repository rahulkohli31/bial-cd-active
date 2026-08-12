"""Shared FastAPI dependency aliases.

`DbSession` is the request-scoped async session. `current_user` is the
consumption seam for every protected endpoint: it authenticates a request purely
from the session cookie and returns the live `User`. This is AUTHENTICATION only
(who you are) — no role/permission check (RBAC is a later phase).

Object storage comes in TWO flavours, and which one a route takes is a contract decision, not
a style choice. `Storage` raises `StorageUnconfiguredError` when the store is off;
`OptionalStorage` hands back `None`. A route whose body maps a storage outage onto a status MUST
take `OptionalStorage` — see that dependency's docstring. The attachments router keeps its OWN
`storage_dependency` (overridden independently in tests): none of its routes documents a
storage-unavailable status, so it stays on the raising flavour.
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
from src.services.storage import (
    AppContainerStore,
    ObjectStorage,
    StorageUnconfiguredError,
    get_app_container_store,
    get_storage,
)

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
    if user.token_version != claims.token_version:
        raise _UNAUTHENTICATED
    return user


CurrentUser = Annotated[User, Depends(current_user)]


def storage_dependency() -> ObjectStorage:
    """The configured object store as a dependency so a test can swap an in-memory fake. The
    attachments router deliberately keeps its OWN `storage_dependency` — the two are overridden
    independently in tests, so they must stay distinct symbols.

    RAISES `StorageUnconfiguredError` on a storage-off deployment, and — because every `Depends`
    is solved BEFORE the route body's first statement — it raises where no `except` of the route's
    can see it. Only take this where an unconfigured store genuinely IS a 500 (a deploy bug, not a
    runtime condition); anything that documents a storage-unavailable status takes
    `OptionalStorage` below."""
    return get_storage()


Storage = Annotated[ObjectStorage, Depends(storage_dependency)]


def storage_or_none_dependency() -> ObjectStorage | None:
    """The configured object store, or **`None` when it is unconfigured** (dev/test) — the
    None-tolerant twin of `storage_dependency`, and the same idiom as `container_store_dependency`
    below.

    It still resolves EAGERLY (every `Depends` does); it just cannot FAIL eagerly. That is the
    whole point: a route that advertises `503` and maps `StorageError → 503` in its body cannot
    honour either promise if the store is resolved by a provider that raises during dependency
    solving — the client gets an undocumented 500 with a DIFFERENT envelope (`{"detail": ...}`
    from the catch-all, not `{"error": {"message": ...}}`). Storage-off is a supported posture
    outside production (`_require_storage_in_production` only gates prod), so that contract break
    is live in exactly the environments nobody watches. Full write-up:
    `docs/solutions/design-patterns/eager-fastapi-depends-bypasses-in-body-error-seam-2026-07-21.md`.

    Deliberately still a dependency rather than a bare `get_storage()` inside the route's `try`:
    that naive fix would read the accessor singleton while a test had wired a fake through
    `dependency_overrides`, and the two would diverge with nothing failing."""
    try:
        return get_storage()
    except StorageUnconfiguredError:
        return None


# `| None`-tolerant, unlike `Storage`: the consuming route maps an unset store onto its own
# documented storage-unavailable answer instead of dying at dependency-solve time.
OptionalStorage = Annotated[ObjectStorage | None, Depends(storage_or_none_dependency)]


def container_store_dependency() -> AppContainerStore | None:
    """The per-app container store as a dependency so a test can swap a fake (mirrors
    `storage_dependency`). Returns **`None` when object storage is unconfigured** (dev/test) —
    `get_app_container_store` diverges from `get_storage` there on purpose, and the sweep helpers
    branch on `None` (`storage off → skip`) rather than raising. Injected into the admin
    hard-delete so `nuke_app` receives the store instead of resolving the singleton inline."""
    return get_app_container_store()


ContainerStore = Annotated[AppContainerStore | None, Depends(container_store_dependency)]
