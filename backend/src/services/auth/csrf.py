"""CSRF — signed, session-bound double-submit token.

The token lives in a NON-HttpOnly cookie (JS reads it) and must be echoed back in
the `X-CSRF-Token` header on every state-changing request (refresh, logout). Two
independent guarantees, both required to pass:

1. Double-submit: the cookie value and the header value must be byte-equal — an
   attacker on another origin can force a request but cannot read our cookie to
   populate the header (SameSite is a second line, not the only one).
2. Session binding: the token is `{nonce}.{hmac}` where the HMAC (SHA-256, keyed
   by the session secret) covers `user_id:token_version:nonce`. So a token cannot
   be forged without the secret, and one issued before a `token_version` bump
   (logout / revocation) no longer verifies — CSRF tokens die with the session.

`verify_csrf` is constant-time and fails CLOSED: any missing, malformed, or
mismatched input returns False, never an exception the caller might miss.
"""

from __future__ import annotations

import hmac
import secrets
import uuid
from hashlib import sha256

from src.config import settings

_NONCE_BYTES = 16


def _sign(user_id: uuid.UUID, token_version: int, nonce: str) -> str:
    secret = settings.auth.session_secret.get_secret_value().encode()
    message = f"{user_id}:{token_version}:{nonce}".encode()
    return hmac.new(secret, message, sha256).hexdigest()


def issue_csrf_token(user_id: uuid.UUID, token_version: int) -> str:
    """Issue a fresh CSRF token bound to this session's user + token_version."""
    nonce = secrets.token_urlsafe(_NONCE_BYTES)
    return f"{nonce}.{_sign(user_id, token_version, nonce)}"


def verify_csrf(
    cookie_value: str, header_value: str, user_id: uuid.UUID, token_version: int
) -> bool:
    """True only if the cookie/header double-submit matches AND the token is a
    valid, current-token_version signature for this user. Fails closed."""
    if not cookie_value or not header_value:
        return False
    # Double-submit equality first (constant-time). Compare as bytes, not str:
    # hmac.compare_digest raises TypeError on a non-ASCII str, which would turn a
    # malformed X-CSRF-Token header into a 500 instead of a fail-closed False.
    if not hmac.compare_digest(cookie_value.encode(), header_value.encode()):
        return False
    nonce, _, signature = cookie_value.partition(".")
    if not nonce or not signature:
        return False
    return hmac.compare_digest(signature, _sign(user_id, token_version, nonce))
