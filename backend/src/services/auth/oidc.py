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
from urllib.parse import urlsplit

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
    """Register the `entra` provider (tenant discovery + PKCE, public SPA client)."""
    oauth = OAuth()
    # The Entra app registration is a SINGLE-PAGE-APPLICATION (SPA) platform client, whose /token
    # endpoint only redeems a code from a CROSS-ORIGIN request — it demands an `Origin` header that
    # matches a registered SPA redirect URI's origin, else AADSTS9002327 ("may only be redeemed via
    # cross-origin requests"). CORS is enforced by browsers, not by Entra, so a server that simply
    # presents the header is accepted. We redeem server-side, so we present it explicitly. The
    # origin is the scheme+host of the configured redirect URI (which IS the registered SPA reply
    # URL), so it always matches — no separate config to drift out of sync.
    redirect = urlsplit(settings.auth.redirect_uri)
    spa_origin = f"{redirect.scheme}://{redirect.netloc}"
    oauth.register(
        name="entra",
        server_metadata_url=settings.auth.server_metadata_url,
        client_id=settings.auth.client_id,
        # PUBLIC-CLIENT — no client secret. The app registration is flagged "Allow public client
        # flows" / SPA, so Entra rejects any secret at the token endpoint (AADSTS700025), and a
        # secret presented ALONGSIDE the Origin header below is itself rejected (Entra forbids
        # credentials in the presence of an Origin). We authenticate public-client style:
        # `token_endpoint_auth_method="none"` sends `client_id` in the token-request body and NO
        # secret; PKCE (S256) is the sole proof of the code exchange. Deliberate, temporary
        # reduction in defense-in-depth (loses client authentication), tracked as a backlog
        # hardening item — revert to a confidential Web-platform client (restore the secret, drop
        # the Origin header) once the app registration is switched. alg=none is still impossible:
        # the id_token is decoded with the discovery doc's signing algs (RS256), not our choice.
        #
        # `headers` is siphoned by Authlib into httpx.AsyncClient as a DEFAULT header (its
        # HTTPX_CLIENT_KWARGS allowlist), so httpx merges the Origin with the token POST's own
        # Content-Type — nothing is clobbered. It also rides the public discovery/JWKS GETs, which
        # is harmless (those endpoints ignore a stray Origin).
        client_kwargs={
            "scope": _SCOPES,
            "code_challenge_method": "S256",
            "token_endpoint_auth_method": "none",
            "headers": {"Origin": spa_origin},
        },
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
