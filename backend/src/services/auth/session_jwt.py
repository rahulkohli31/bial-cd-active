"""Session JWT — mint + decode with the algorithm PINNED (joserfc, HS256).

The backend's OWN session token (distinct from the Entra id_token, which is
validated and discarded — R5). It carries the minimum needed to authenticate a
request and support instant revocation (KD-6): `sub` (user id), `token_version`,
and `iat`/`exp`. No email/role claim — that would go stale; `current_user`
re-reads live user state instead.

joserfc is the single JOSE stack (Authlib's own OIDC dependency, non-deprecated —
KD-1). The algorithm is pinned to HS256 on BOTH encode and decode: `jwt.decode`
is given `algorithms=["HS256"]`, so a token forged with `alg=none` (or any other
algorithm) is rejected before its claims are read (R18). Every JOSE failure —
bad signature, expiry, missing essential claim — maps to a typed `AuthError`
that leaks no detail (fail closed).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import OctKey
from joserfc.jwt import JWTClaimsRegistry

from src.config import settings
from src.services.auth.errors import REASON_INVALID_SESSION, AuthError

_ALG = "HS256"

# `exp`/`iat`/`sub` are essential: absence (or an expired `exp`) fails validation.
# `now=None` -> joserfc evaluates the current time at each validate() call, so a
# module-level registry still checks expiry against real now.
_CLAIMS_REGISTRY = JWTClaimsRegistry(
    sub={"essential": True},
    exp={"essential": True},
    iat={"essential": True},
)


@dataclass(frozen=True, slots=True)
class SessionClaims:
    """The parsed, type-checked session identity — never a raw claims dict."""

    user_id: uuid.UUID
    token_version: int


def _session_key() -> OctKey:
    # The single session secret (>= 32 chars, enforced by AuthConfig) signs the
    # HS256 session JWT. Unwrapped only here, at the JOSE boundary (security.md).
    return OctKey.import_key(settings.auth.session_secret.get_secret_value())


def mint_session_jwt(user_id: uuid.UUID, token_version: int, ttl_seconds: int) -> str:
    """Mint a signed session JWT valid for `ttl_seconds`."""
    now = int(time.time())
    header = {"alg": _ALG, "typ": "JWT"}
    claims = {
        "sub": str(user_id),
        "token_version": token_version,
        "iat": now,
        "exp": now + ttl_seconds,
    }
    return jwt.encode(header, claims, _session_key())


def decode_session_jwt(token: str) -> SessionClaims:
    """Verify signature (HS256 pinned) + claims and return typed identity.

    Raises `AuthError` on ANY failure — bad/none algorithm, bad signature, expiry,
    missing/ malformed claim — with no leaked detail (fail closed)."""
    try:
        decoded = jwt.decode(token, _session_key(), algorithms=[_ALG])
        _CLAIMS_REGISTRY.validate(decoded.claims)
    except JoseError as exc:
        # Covers BadSignatureError, ExpiredTokenError, MissingClaimError,
        # InvalidClaimError, and an alg outside the allow-list.
        raise AuthError("session token rejected", reason=REASON_INVALID_SESSION) from exc
    except (ValueError, TypeError) as exc:
        # Structurally malformed token (bad base64 / not a JWT) surfaces here.
        raise AuthError("session token malformed", reason=REASON_INVALID_SESSION) from exc

    claims = decoded.claims
    # `sub` is registry-guaranteed present; `token_version` is our own claim, so
    # validate it explicitly. Parse at the boundary into typed values.
    raw_version = claims.get("token_version")
    if not isinstance(raw_version, int) or isinstance(raw_version, bool):
        raise AuthError("session token token_version invalid", reason=REASON_INVALID_SESSION)
    try:
        user_id = uuid.UUID(str(claims["sub"]))
    except (ValueError, TypeError, AttributeError) as exc:
        raise AuthError("session token sub invalid", reason=REASON_INVALID_SESSION) from exc

    return SessionClaims(user_id=user_id, token_version=raw_version)
