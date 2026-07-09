"""Authentication error hierarchy.

One base for every auth failure (bad session token, wrong tenant, invalid
callback, refresh reuse). It carries only NON-SECRET diagnostic context: a
human-readable `message` for server logs and a short stable `reason` code that is
safe to surface to the browser (the `/login?authError=<reason>` redirect, U5).
NEVER put a token, secret, claim value, or credential on an `AuthError` — the
message and reason may be logged and the reason is reflected to the client
(security.md). Security checks fail CLOSED: raise, never return a sentinel. Every
class ends in `Error` (N818).
"""

from __future__ import annotations

# Stable, non-secret reason codes. U5 maps these to redirect query values and U8
# maps those to banner copy — so they are a small closed vocabulary, not free text.
REASON_INVALID_SESSION = "invalid_session"
REASON_WRONG_TENANT = "wrong_tenant"
REASON_INVALID_CALLBACK = "invalid_callback"
REASON_SESSION_EXPIRED = "session_expired"
REASON_INVALID_REFRESH = "invalid_refresh"
REASON_AUTH_FAILED = "auth_failed"
# Local governance suspension (U10, R11): Entra authenticated the user fine, but a
# super-admin has blocked them platform-side — the banner says so explicitly.
REASON_ACCOUNT_SUSPENDED = "account_suspended"


class AuthError(Exception):
    """Base for every authentication failure. `reason` is a safe, stable code
    surfaced to the client; `message` is for server logs only. Neither ever
    carries a secret."""

    def __init__(self, message: str, *, reason: str = REASON_AUTH_FAILED) -> None:
        super().__init__(message)
        self.reason = reason
