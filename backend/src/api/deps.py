"""Shared FastAPI dependency aliases.

`DbSession` is the request-scoped async session. `current_user` is the
consumption seam for every protected endpoint: it authenticates a request purely
from the session cookie and returns the live `User`. This is AUTHENTICATION only
(who you are) — no role/permission check (RBAC is a later phase).
"""

from typing import Annotated, Final

import structlog
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.user import User
from src.db.session import get_db
from src.services.analysis import AnalysisRuntime, get_analysis_runtime
from src.services.auth.cookies import session_cookie_name
from src.services.auth.errors import AuthError
from src.services.auth.session_jwt import SessionClaims, decode_session_jwt
from src.services.ratelimit import InProcessRateLimiter, RateLimitExceededError
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
# a super-admin blocked the account. A 403 tells the SPA to stop silently
# refreshing (which a 401 would trigger) and surface the state instead.
_SUSPENDED = HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account suspended")


def _session_claims(request: Request) -> SessionClaims | None:
    """The verified identity in the session cookie, or None for a missing or invalid one."""
    token = request.cookies.get(session_cookie_name())
    if not token:
        return None
    try:
        return decode_session_jwt(token)
    except AuthError:
        return None


async def current_user(request: Request, db: DbSession) -> User:
    """Authenticate from the session cookie; return the live `User` or raise 401.

    Translates `AuthError` into `HTTPException(401)` ITSELF — the composition-root
    catch-all (`add_exception_handler(Exception, ...)`) would otherwise turn an
    uncaught `AuthError` into a generic 500, not a 401 (see core/errors.py)."""
    claims = _session_claims(request)
    if claims is None:
        raise _UNAUTHENTICATED

    user = await db.get(User, claims.user_id)
    # An unknown user can't be suspended and carries no token_version to compare — 401.
    if user is None:
        raise _UNAUTHENTICATED
    if user.suspended_at is not None:
        # Suspension seam 2 of 3: checked BEFORE the token_version gate on
        # purpose. Deactivation bumps token_version AND sets suspended_at, so a suspended
        # user's live JWT is genuinely stale — checking token_version first would 401 them
        # and the SPA would silently refresh instead of surfacing the suspension. This is not
        # a weakening of revocation: a token-stale-but-NOT-suspended user (logout, reactivated
        # old session) still falls through to the 401 below.
        logger.warning("suspended_user_rejected", user_id=str(user.id), seam="current_user")
        raise _SUSPENDED
    # A session revoked by a token_version bump (logout / revocation) with no suspension —
    # the live DB value is the source of truth.
    if user.token_version != claims.token_version:
        raise _UNAUTHENTICATED
    return user


CurrentUser = Annotated[User, Depends(current_user)]


_API_CEILING_PER_MINUTE: Final = 600
"""Requests one signed-in user may make in a minute, per source. The portal's busiest
legitimate rate is about eighty: the start probe running on two surfaces at once."""

_API_CEILING = InProcessRateLimiter(limit=_API_CEILING_PER_MINUTE, window_seconds=60)

# `Sec-Fetch-Site` is set by the browser and page script cannot forge it. A generated app's
# host shares the portal's registrable domain, so its requests carry the viewer's cookie as
# `same-site`; counting them apart keeps an app someone merely opened from spending the budget
# the portal needs to keep them signed in.
_APP_ORIGINATED: Final = frozenset({"same-site", "cross-site"})


async def api_ceiling(request: Request) -> None:
    """Refuse a signed-in user's request over the per-minute ceiling with a 429.

    Verifies the session's signature without a database read. A request with no valid session
    is not counted: there is no safe key for it — every BIAL user shares one egress address and
    the forwarded one is the client's to choose."""
    claims = _session_claims(request)
    if claims is None:
        return
    source = "app" if request.headers.get("sec-fetch-site") in _APP_ORIGINATED else "portal"
    if not _API_CEILING.hit(f"{claims.user_id}:{source}"):
        raise RateLimitExceededError("Too many requests. Please wait a minute and try again.")


def storage_dependency() -> ObjectStorage:
    """The configured object store as a dependency so a test can swap an in-memory fake. The
    attachments router deliberately keeps its OWN `storage_dependency`: the two are overridden
    independently, so merging them would bind a test's fake to a key the route never resolves.

    Take this only where an unconfigured store genuinely IS a deploy bug — it RAISES at
    dependency-solve time, where no `except` of the route's can reach it; a route that documents a
    storage-unavailable status takes `OptionalStorage` below."""
    return get_storage()


Storage = Annotated[ObjectStorage, Depends(storage_dependency)]


def storage_or_none_dependency() -> ObjectStorage | None:
    """The configured object store, or **`None` when it is unconfigured** (dev/test). It resolves
    eagerly like every `Depends`; it just cannot FAIL eagerly, which is what lets the consuming
    route map an unset store onto the status it documents instead of answering an undocumented 500.

    Deliberately still a dependency rather than a bare `get_storage()` inside the route's `try`:
    that naive fix would read the accessor singleton while a test had wired a fake through
    `dependency_overrides`, and the two would diverge with nothing failing."""
    try:
        return get_storage()
    except StorageUnconfiguredError:
        return None


OptionalStorage = Annotated[ObjectStorage | None, Depends(storage_or_none_dependency)]


def container_store_dependency() -> AppContainerStore | None:
    """The per-app container store as a dependency so a test can swap a fake (mirrors
    `storage_dependency`). Returns **`None` when object storage is unconfigured** (dev/test)."""
    return get_app_container_store()


ContainerStore = Annotated[AppContainerStore | None, Depends(container_store_dependency)]


def analysis_runtime_dependency() -> AnalysisRuntime | None:
    """BIAL Chat's analysis runtime, or **`None` when it is not configured** — a supported posture
    in every environment, in which BIAL Chat refuses Office and CSV files at its doors."""
    return get_analysis_runtime()


OptionalAnalysis = Annotated[AnalysisRuntime | None, Depends(analysis_runtime_dependency)]
