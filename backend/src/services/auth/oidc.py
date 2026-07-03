"""Entra ID OIDC client + fail-closed identity validator.

`build_oauth()` registers the single `entra` provider against the TENANT-SPECIFIC
discovery document (`{tenant_id}/v2.0`, never `common`/`organizations` — a
templated issuer defeats the exact `iss` match, R17), with PKCE (`S256`) and the
`openid profile email` scopes. `get_oauth()` is the injectable seam (KD-9): the
endpoints depend on it, and tests override it (or pre-seed `.entra.server_metadata`)
so no live tenant or forged JWKS is needed.

`validate_entra_token` is the fail-closed gate (KD-3). Authlib's
`authorize_access_token` does NOT raise when the token response lacks an
`id_token` — it returns a dict with no `userinfo` and performs ZERO OIDC
validation. `userinfo` is populated only AFTER Authlib fully validates the
signature / `iss` / `aud` / `exp` / `nonce`, so its presence is the proof of
validation. We then hard-assert `oid` + `sub` present and `tid == tenant_id`, and
derive a non-null email (the `email` claim is optional even with the `email`
scope; `preferred_username` is the reliable fallback for work accounts) — else
`AuthError`. Any failure denies: no session, no user row (AE1, AE4).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from authlib.integrations.starlette_client import OAuth

from src.config import settings
from src.services.auth.errors import (
    REASON_INVALID_CALLBACK,
    REASON_WRONG_TENANT,
    AuthError,
)

_SCOPES = "openid profile email"


@dataclass(frozen=True, slots=True)
class EntraIdentity:
    """The validated identity extracted from the Entra token — a typed object
    parsed at the boundary, never a raw claims dict passed inward."""

    oid: str
    email: str
    upn: str | None
    display_name: str | None


def build_oauth() -> OAuth:
    """Register the `entra` provider (tenant discovery + PKCE)."""
    oauth = OAuth()
    oauth.register(
        name="entra",
        server_metadata_url=settings.auth.server_metadata_url,
        client_id=settings.auth.client_id,
        client_secret=settings.auth.client_secret.get_secret_value(),
        # S256 PKCE + the OIDC scopes. alg=none is impossible here: the id_token is
        # decoded with the discovery doc's signing algs (RS256), not our choice.
        client_kwargs={"scope": _SCOPES, "code_challenge_method": "S256"},
    )
    return oauth


@lru_cache(maxsize=1)
def get_oauth() -> OAuth:
    """Process-wide OAuth registry — the seam the endpoints depend on. Tests
    override this dependency (or pre-seed the returned registry's
    `.entra.server_metadata`) to run the flow without a live tenant (KD-9)."""
    return build_oauth()


def validate_entra_token(token: Mapping[str, Any]) -> EntraIdentity:
    """Fail-closed identity extraction from a validated token response (KD-3).

    Raises `AuthError` on missing `userinfo` (unvalidated token), missing
    `oid`/`sub`, a foreign `tid`, or a missing email+UPN. Never returns on doubt."""
    userinfo = token.get("userinfo")
    # `userinfo` present AND a mapping == Authlib fully validated the id_token.
    if not isinstance(userinfo, Mapping) or not userinfo:
        raise AuthError(
            "callback token carries no validated userinfo", reason=REASON_INVALID_CALLBACK
        )

    oid = userinfo.get("oid")
    sub = userinfo.get("sub")
    if not oid or not sub:
        raise AuthError("callback identity missing oid/sub", reason=REASON_INVALID_CALLBACK)

    # Hard tenant boundary — a foreign tenant or personal account is rejected
    # fail-closed (AE1, R13).
    if userinfo.get("tid") != settings.auth.tenant_id:
        raise AuthError("callback tenant does not match", reason=REASON_WRONG_TENANT)

    # Entra's `email` claim is optional even with the `email` scope;
    # `preferred_username` (the UPN) is reliably present for work accounts. A
    # missing email is a DEFINED fail-closed outcome, never a NOT NULL crash on
    # upsert.
    email = userinfo.get("email") or userinfo.get("preferred_username")
    if not email:
        raise AuthError(
            "callback identity has neither email nor UPN", reason=REASON_INVALID_CALLBACK
        )

    # Capture the UPN unconditionally (separate from email) as the deterministic
    # join key for the deferred POC->Postgres migration (KD-3).
    upn = userinfo.get("preferred_username")
    display_name = userinfo.get("name")
    return EntraIdentity(
        oid=str(oid),
        email=str(email),
        upn=str(upn) if upn else None,
        display_name=str(display_name) if display_name else None,
    )
