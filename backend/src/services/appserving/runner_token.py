"""Runner-token mint (R20, review P1).

The 1.5.0 auth is HttpOnly-cookie-only — there is NO client-JS-readable token for
the same-origin runner shell to hand into the sandboxed opaque-origin frame (the old
Express shell read a `localStorage` token that no longer exists). The fix is a
dedicated, same-origin, **cookie-authenticated** mint endpoint (`POST
/apps/{appId}/runner-token`, `runner.py`): it authenticates via the session cookie
(`current_user`) and issues a **fresh, short-lived** token for iframe injection only.

The raw session JWT and the refresh cookie are NEVER exposed to client JS or written
to `localStorage` — do not "fix" injection by loosening the cookie to JS-readable,
which would undo the HttpOnly hardening and reopen XSS token theft. The minted token
is a freshly-signed access token (not the cookie's value); it is only ever presented
as an `Authorization: Bearer` header, and the cookie-authed endpoints
(`current_user`) never read that header — so it is inherently scoped to the
Bearer-accepting data-service. App-bound audience scoping is the documented deferred
hardening (parity with the Express POC tradeoff).
"""

from __future__ import annotations

import uuid

from src.config import settings
from src.services.auth.session_jwt import SessionClaims, decode_session_jwt, mint_session_jwt


def mint_runner_token(user_id: uuid.UUID, token_version: int) -> str:
    """Mint a fresh short-lived token for injection into the runner frame. Same
    signed-JWT verification path as the session cookie (so the data-service verifies
    it), but minted server-side per request — the cookie's own value never leaves
    the server."""
    return mint_session_jwt(user_id, token_version, settings.auth.access_ttl_seconds)


def verify_runner_token(token: str) -> SessionClaims:
    """Verify a Bearer runner token (HS256 pinned) and return typed identity. Raises
    `AuthError` on any failure (fail closed). Used by the X-App-Key chain's
    login-required re-check (U4)."""
    return decode_session_jwt(token)
