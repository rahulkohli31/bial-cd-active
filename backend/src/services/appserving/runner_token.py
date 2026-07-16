"""Runner-token verify (R20, review P1).

The dedicated `mint_runner_token` wrapper + its `POST /apps/{appId}/runner-token` endpoint
(`runner.py`) were retired with the open-sandbox pivot — a deployed app is served from the
sandbox's own Caddy, not this control plane, so there is no runner shell here to mint a token for.
What remains is the VERIFY half: the X-App-Key data chain's login-required re-check
(`appkey/chain.py`) accepts a signed session/runner JWT presented as `Authorization: Bearer` and
decodes it through the SAME pinned-HS256 path as the session cookie.

The raw session JWT and the refresh cookie are NEVER exposed to client JS or written to
`localStorage` — do not loosen the cookie to JS-readable, which would undo the HttpOnly hardening
and reopen XSS token theft. A Bearer token is only ever presented as an `Authorization` header, and
the cookie-authed endpoints (`current_user`) never read that header — so it is inherently scoped to
the Bearer-accepting data-service. App-bound audience scoping is the documented deferred hardening.
"""

from __future__ import annotations

from src.services.auth.session_jwt import SessionClaims, decode_session_jwt


def verify_runner_token(token: str) -> SessionClaims:
    """Verify a Bearer runner token (HS256 pinned) and return typed identity. Raises
    `AuthError` on any failure (fail closed). Used by the X-App-Key chain's
    login-required re-check (U4)."""
    return decode_session_jwt(token)
