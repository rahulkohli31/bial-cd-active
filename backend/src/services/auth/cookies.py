"""Cookie names + the environment-aware Secure/prefix decision (KD-4).

Shared by the auth router (which SETS/CLEARS the cookies on a Response) and the
`current_user` dependency (which READS the session cookie off the Request) — the
second consumer that earns this its own module (ADR-0010). The
`__Host-`/`__Secure-` prefixes require Secure + https, so a name carries its prefix
only when Secure applies: prefixed in production, bare over plain http in dev.
"""

from __future__ import annotations

from src.config import settings

# External, browser-visible path the refresh cookie is scoped to. It carries the
# /api prefix the edge adds (KD-8) and MUST match the path the SPA calls, so the
# cookie is sent ONLY on the refresh request — never on every API call.
REFRESH_COOKIE_PATH = "/api/v1/auth/refresh"


def cookie_secure() -> bool:
    override = settings.auth.cookie_secure
    return override if override is not None else settings.is_production


def session_cookie_name() -> str:
    return "__Host-session" if cookie_secure() else "session"


def refresh_cookie_name() -> str:
    return "__Secure-refresh" if cookie_secure() else "refresh"


def csrf_cookie_name() -> str:
    return "__Host-csrf" if cookie_secure() else "csrf"
