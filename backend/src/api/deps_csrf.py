"""The shared signed double-submit CSRF gate for mutating data-plane POSTs (ADR-0007).

Lives here — beside `deps_rbac.py`, the other cross-domain gate — rather than inside one
domain: it started as the C3 control surface's own dependency (KTD-4), and the canonical
builder-thread endpoint (`api/v1/conversations`) is the second consumer, which is what
earns it a shared home (ADR-0010: present-tense reuse, never speculative).

It is deliberately NOT universal. The chat relay (`/v1/claude`) carries no CSRF token —
that precedent is frozen by its contract — so a route opts IN by declaring `RequireCsrf`.
Fails closed with the data-plane `{"error":{"message","code"}}` envelope.
"""

from __future__ import annotations

from fastapi import Depends, Request

from src.api.deps import CurrentUser
from src.core.errors import AppApiError
from src.services.auth.cookies import csrf_cookie_name
from src.services.auth.csrf import verify_csrf


async def require_csrf(user: CurrentUser, request: Request) -> None:
    """Signed double-submit CSRF check on a mutating POST (ADR-0007). Fails closed with
    the data-plane envelope (distinct from the chat relay, which uses none)."""
    if not verify_csrf(
        request.cookies.get(csrf_cookie_name(), ""),
        request.headers.get("x-csrf-token", ""),
        user.id,
        user.token_version,
    ):
        raise AppApiError(403, "CSRF check failed.", code="csrf_failed")


RequireCsrf = Depends(require_csrf)
