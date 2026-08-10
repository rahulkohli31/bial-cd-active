"""A1 — the sandbox login broker's short-lived handoff token.

Hands a verified Entra identity off from the backend broker (one fixed, registered
redirect URI) to a sandbox's own dynamic-FQDN origin, with no new shared-secret
infrastructure: the token is signed with the TARGET sandbox's own per-container
`SUPERVISOR_TOKEN` (already minted at provision, `services/sandbox/client.py`, and
already present in the supervisor's own env) — so only the correct sandbox can ever
verify a token minted for it, and revoking/expiring the container revokes the key too.

Deliberately a hand-rolled HMAC token (mirrors `csrf.py`'s `{body}.{hmac}` shape), NOT
a joserfc JWT (`session_jwt.py`'s pattern): the supervisor that verifies this token runs
stdlib + FastAPI only (`sandbox/supervisor/app.py`, the frozen minimal C1 image) — no
JOSE library is a runtime dependency there, and this format needs none either. The
supervisor's own `/auth/complete` mints its longer-lived session-cookie token with the
same wire format, independently implemented (no shared import is possible across the
backend/sandbox process boundary) — keep the two in sync if this format ever changes.
"""

from __future__ import annotations

import base64
import hmac
import json
import time
from hashlib import sha256

_DEFAULT_TTL_SECONDS = 30


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _sign(body: str, key: str) -> str:
    return hmac.new(key.encode(), body.encode(), sha256).hexdigest()


def mint_sandbox_handoff_token(
    app_id: str, supervisor_token: str, *, ttl_seconds: int = _DEFAULT_TTL_SECONDS
) -> str:
    """Sign a `{app_id, exp}` payload with the TARGET sandbox's own `SUPERVISOR_TOKEN`.

    Short TTL (~30s) by design: this only ever rides the browser's own redirect chain
    over HTTPS, immediately consumed. Not tracked for true single-use (no server-side
    replay store) — an accepted Day-1 gap given the short window, noted as a follow-up.
    """
    payload = {"app_id": app_id, "exp": int(time.time()) + ttl_seconds}
    body = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    return f"{body}.{_sign(body, supervisor_token)}"


def verify_sandbox_handoff_token(token: str, supervisor_token: str) -> str:
    """Verify signature + expiry; return the embedded `app_id`.

    Raises `ValueError` on ANY failure (malformed, bad signature, expired) — fail
    closed, no detail leaked to the caller.
    """
    body, _, signature = token.partition(".")
    if not body or not signature:
        raise ValueError("malformed handoff token")
    if not hmac.compare_digest(signature, _sign(body, supervisor_token)):
        raise ValueError("bad handoff token signature")
    try:
        payload = json.loads(_b64url_decode(body))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("malformed handoff token payload") from exc
    app_id = payload.get("app_id")
    exp = payload.get("exp")
    if not isinstance(app_id, str) or not isinstance(exp, int):
        raise ValueError("malformed handoff token payload")
    if exp < int(time.time()):
        raise ValueError("expired handoff token")
    return app_id
