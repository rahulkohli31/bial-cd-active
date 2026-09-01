"""The shared signed double-submit CSRF gate for mutating data-plane POSTs (ADR-0007).

Lives here — beside `deps_rbac.py`, the other cross-domain gate — rather than inside one
domain: it started as the C3 control surface's own dependency (KTD-4), and the canonical
builder-thread endpoint (`api/v1/conversations`) is the second consumer, which is what
earns it a shared home (ADR-0010: present-tense reuse, never speculative).

It is deliberately NOT universal: a route opts IN by declaring `RequireCsrf`. That used to
be justified by the legacy chat relay, which carried no CSRF token and whose contract was
frozen — and the relay is now retired, so the exception it stood for is gone. Opt-in survives it
on its own merits: it keeps the gate a visible line at each mutating route rather than a
blanket a new GET-shaped endpoint silently inherits. If every mutating route is meant to carry
it, make that a positive decision and audit the list; do not let this comment imply the old
exception still exists.
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
    the data-plane `{"error":{"message","code"}}` envelope."""
    if not verify_csrf(
        request.cookies.get(csrf_cookie_name(), ""),
        request.headers.get("x-csrf-token", ""),
        user.id,
        user.token_version,
    ):
        raise AppApiError(403, "CSRF check failed.", code="csrf_failed")


RequireCsrf = Depends(require_csrf)
