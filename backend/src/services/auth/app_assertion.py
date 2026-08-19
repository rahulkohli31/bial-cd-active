"""Generated-app identity assertion — mint (control-plane) + JWKS publish (issue #92,
R3-R6). The control-plane MINTS ONLY; verification happens INSIDE the generated app
(R11), against the published public key, never here in production. `verify_app_assertion`
exists purely so this repo's own tests can round-trip mint -> verify without a JS runtime.

Signed RS256 with the dedicated `APP_ASSERTION__*` key pair (`services/auth/config.py`'s
`AppAssertionConfig`) — never `AUTH__SESSION_SECRET` (R4). The assertion carries the
Entra object id as `sub` (R5 — never key on email, which can change), is bound to one
app (`aud`) and one plane (a custom `plane` claim, R6), and carries a hard `exp` with no
refresh capability of its own (R16) — a caller mints a fresh one every time.
"""

from __future__ import annotations

import base64
import time
import uuid
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Literal

from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import RSAKey
from joserfc.jwt import JWTClaimsRegistry

from src.config import settings

_ALG = "RS256"

Plane = Literal["preview", "deployed"]

_CLAIMS_REGISTRY = JWTClaimsRegistry(
    sub={"essential": True},
    aud={"essential": True},
    exp={"essential": True},
    iat={"essential": True},
)


@lru_cache(maxsize=1)
def _signing_key() -> RSAKey:
    # The single dedicated keypair (R4). Unwrapped only here, at the JOSE boundary
    # (security.md) — base64 because a raw multi-line PEM is fragile across dotenv
    # parsers/shells (AppAssertionConfig's docstring has the generate command).
    pem = base64.b64decode(settings.app_assertion.private_key_pem_b64.get_secret_value())
    return RSAKey.import_key(pem)


def _key_id() -> str:
    # RFC 7638 JWK thumbprint — deterministic from the key material itself, so it
    # never drifts from what get_jwks() publishes and needs no separate config field.
    return _signing_key().thumbprint()


@dataclass(frozen=True, slots=True)
class AppAssertionClaims:
    """The parsed, type-checked assertion — never a raw claims dict passed inward."""

    entra_oid: str
    email: str
    display_name: str | None
    app_id: uuid.UUID
    plane: Plane


def mint_app_assertion(
    *,
    entra_oid: str,
    email: str,
    display_name: str | None,
    app_id: uuid.UUID,
    plane: Plane,
) -> str:
    """Mint a short-lived signed assertion naming `entra_oid` and binding it to
    exactly one app and one plane (R3, R6). Callers authenticate the human through
    the platform's EXISTING Entra flow first (`oidc.py`, unchanged) — this function
    only ever mints for a caller already proven to be that person; it performs no
    Entra round-trip of its own."""
    now = int(time.time())
    header = {"alg": _ALG, "kid": _key_id(), "typ": "JWT"}
    claims: dict[str, Any] = {
        "sub": entra_oid,
        "email": email,
        "name": display_name,
        "aud": str(app_id),
        "plane": plane,
        "iat": now,
        "exp": now + settings.app_assertion.assertion_ttl_seconds,
    }
    return jwt.encode(header, claims, _signing_key())


_LAUNCH_EXCHANGE_AUDIENCE = "bial-platform-launch-exchange"
_LAUNCH_CODE_TTL_SECONDS = 60


@dataclass(frozen=True, slots=True)
class LaunchExchangeClaims:
    """The verified-Entra identity a launch redirect carries, PRE-assertion (R10)."""

    entra_oid: str
    email: str
    display_name: str | None
    app_id: uuid.UUID
    next_path: str


def mint_launch_code(
    *, entra_oid: str, email: str, display_name: str | None, app_id: uuid.UUID, next_path: str
) -> str:
    """R10's launch-redirect payload — deliberately NOT an app assertion. A short-lived
    (60s) exchange code the deployed app's OWN server trades server-to-server for the
    real assertion (`verify_launch_code` + `mint_app_assertion`) — this is what rides
    the redirect URL, never the assertion itself (R10: "never in the URL, browser
    history, referrer headers, or server access logs").

    Signed with the SAME dedicated key as `mint_app_assertion` (no new secret to hold),
    but with a fixed sentinel `aud` that no real app id can ever equal — so even if a
    generated app tried to verify this against the published JWKS (it never receives
    it through any channel that would let it), the audience check fails naturally; it
    is not a capability a generated app could ever present as a usable identity."""
    now = int(time.time())
    header = {"alg": _ALG, "kid": _key_id(), "typ": "JWT"}
    claims: dict[str, Any] = {
        "sub": entra_oid,
        "email": email,
        "name": display_name,
        "aud": _LAUNCH_EXCHANGE_AUDIENCE,
        "app_id": str(app_id),
        "next": next_path,
        "iat": now,
        "exp": now + _LAUNCH_CODE_TTL_SECONDS,
    }
    return jwt.encode(header, claims, _signing_key())


def verify_launch_code(code: str) -> LaunchExchangeClaims:
    """Verify a launch-exchange code and return the identity it carries. Raises
    `ValueError` on ANY failure (bad signature, expired, wrong audience, malformed) —
    fail closed. NOT tracked for true single-use (no server-side replay store) — an
    accepted gap given the 60s window and HTTPS-only transit (mirrors R16's own
    documented posture on the final assertion), noted as a follow-up.
    """
    try:
        decoded = jwt.decode(code, _signing_key(), algorithms=[_ALG])
        _CLAIMS_REGISTRY.validate(decoded.claims)
    except JoseError as exc:
        raise ValueError("launch code rejected") from exc

    claims = decoded.claims
    if claims.get("aud") != _LAUNCH_EXCHANGE_AUDIENCE:
        raise ValueError("not a launch-exchange code")

    entra_oid = claims.get("sub")
    email = claims.get("email")
    app_id_raw = claims.get("app_id")
    next_path = claims.get("next")
    if (
        not isinstance(entra_oid, str)
        or not isinstance(email, str)
        or not isinstance(app_id_raw, str)
        or not isinstance(next_path, str)
    ):
        raise ValueError("launch code missing required claims")
    try:
        app_id = uuid.UUID(app_id_raw)
    except ValueError as exc:
        raise ValueError("launch code has a malformed app_id") from exc

    name = claims.get("name")
    return LaunchExchangeClaims(
        entra_oid=entra_oid,
        email=email,
        display_name=str(name) if name else None,
        app_id=app_id,
        next_path=next_path,
    )


def get_jwks() -> dict[str, list[dict[str, Any]]]:
    """The public JWKS document (R3, R11) a generated app fetches to verify an
    assertion's signature itself. The PRIVATE half never leaves this process —
    `as_dict(private=False)` is the public numbers only (n, e, kty)."""
    public = _signing_key().as_dict(private=False)
    return {"keys": [{**public, "kid": _key_id(), "use": "sig", "alg": _ALG}]}


def verify_app_assertion(token: str, *, app_id: uuid.UUID, plane: Plane) -> AppAssertionClaims:
    """Verify signature + claims and return typed identity. FOR THIS REPO'S OWN TEST
    SUITE ONLY — production verification happens inside the generated app (R11)
    against `get_jwks()`'s published key, never here. Raises `ValueError` on ANY
    failure (bad signature, expired, wrong app/plane, missing/malformed claim) —
    fail closed, no partial trust."""
    try:
        decoded = jwt.decode(token, _signing_key(), algorithms=[_ALG])
        _CLAIMS_REGISTRY.validate(decoded.claims)
    except JoseError as exc:
        raise ValueError("app assertion rejected") from exc

    claims = decoded.claims
    if claims.get("aud") != str(app_id):
        raise ValueError("app assertion is bound to a different app")
    if claims.get("plane") != plane:
        raise ValueError("app assertion is bound to a different plane")

    entra_oid = claims.get("sub")
    email = claims.get("email")
    if not isinstance(entra_oid, str) or not isinstance(email, str):
        raise ValueError("app assertion missing sub/email")
    name = claims.get("name")

    return AppAssertionClaims(
        entra_oid=entra_oid,
        email=email,
        display_name=str(name) if name else None,
        app_id=app_id,
        plane=plane,
    )
