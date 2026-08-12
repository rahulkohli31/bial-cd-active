"""Fail-closed Entra token validation + OIDC registration (U4).

Entra is mocked at the boundary (KD-9): `validate_entra_token` is a pure function
exercised with crafted token dicts, so there is no live tenant or forged JWKS.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import pytest

from src.config import settings
from src.services.auth.errors import REASON_INVALID_CALLBACK, REASON_WRONG_TENANT, AuthError
from src.services.auth.oidc import build_oauth, validate_entra_token

_TENANT = settings.auth.tenant_id

# Sentinel meaning "remove this key entirely" (vs. setting it to None/empty).
_ABSENT = object()


def _token(**userinfo_overrides: Any) -> dict[str, Any]:
    userinfo: dict[str, Any] = {
        "oid": "entra-oid-123",
        "sub": "entra-sub-123",
        "tid": _TENANT,
        "email": "citizen@rvaiglobal.com",
        "preferred_username": "citizen@rvaiglobal.com",
        "name": "A Citizen",
    }
    userinfo.update(userinfo_overrides)
    # Drop any key explicitly set to the _ABSENT sentinel.
    userinfo = {k: v for k, v in userinfo.items() if v is not _ABSENT}
    return {"userinfo": userinfo}


def test_valid_token_returns_identity() -> None:
    identity = validate_entra_token(_token())
    assert identity.oid == "entra-oid-123"
    assert identity.email == "citizen@rvaiglobal.com"
    assert identity.upn == "citizen@rvaiglobal.com"
    assert identity.display_name == "A Citizen"


@pytest.mark.parametrize("token", [{}, {"userinfo": None}, {"userinfo": {}}])
def test_missing_userinfo_rejected(token: dict[str, Any]) -> None:
    # AE4: a token with no validated userinfo is fail-closed.
    with pytest.raises(AuthError) as exc:
        validate_entra_token(token)
    assert exc.value.reason == REASON_INVALID_CALLBACK


@pytest.mark.parametrize("field", ["oid", "sub"])
def test_missing_oid_or_sub_rejected(field: str) -> None:
    with pytest.raises(AuthError) as exc:
        validate_entra_token(_token(**{field: _ABSENT}))
    assert exc.value.reason == REASON_INVALID_CALLBACK


def test_foreign_tenant_rejected() -> None:
    # AE1: a token minted for a different tenant (or a personal account) is denied.
    with pytest.raises(AuthError) as exc:
        validate_entra_token(_token(tid="ffffffff-ffff-ffff-ffff-ffffffffffff"))
    assert exc.value.reason == REASON_WRONG_TENANT


def test_email_falls_back_to_preferred_username() -> None:
    # Entra's email claim is optional; the UPN is the reliable fallback.
    identity = validate_entra_token(_token(email=_ABSENT))
    assert identity.email == "citizen@rvaiglobal.com"  # from preferred_username
    assert identity.upn == "citizen@rvaiglobal.com"


def test_missing_email_and_upn_rejected() -> None:
    # Neither email nor UPN -> defined fail-closed outcome, not a NOT NULL crash.
    with pytest.raises(AuthError) as exc:
        validate_entra_token(_token(email=_ABSENT, preferred_username=_ABSENT))
    assert exc.value.reason == REASON_INVALID_CALLBACK


def test_upn_captured_even_when_email_present() -> None:
    # upn is captured unconditionally (the deferred-migration join key), distinct
    # from email even when both are present.
    identity = validate_entra_token(_token(email="alias@rvaiglobal.com"))
    assert identity.email == "alias@rvaiglobal.com"
    assert identity.upn == "citizen@rvaiglobal.com"


def test_registration_uses_tenant_discovery_and_s256() -> None:
    oauth = build_oauth()
    assert oauth.entra.client_kwargs["code_challenge_method"] == "S256"
    assert oauth.entra.client_kwargs["scope"] == "openid profile email"
    # PUBLIC-CLIENT hotfix: no secret is presented at the token endpoint; PKCE is the sole
    # proof of the code exchange (AADSTS700025). Revert with the confidential-client hardening.
    assert oauth.entra.client_kwargs["token_endpoint_auth_method"] == "none"
    # SPA-platform redemption is cross-origin: the token request carries an Origin header matching
    # the registered SPA redirect URI's origin (AADSTS9002327). Derived from redirect_uri as
    # scheme+host (no path/trailing slash), so it always matches the registered reply URL.
    redirect = urlsplit(settings.auth.redirect_uri)
    assert (
        oauth.entra.client_kwargs["headers"]["Origin"] == f"{redirect.scheme}://{redirect.netloc}"
    )
    # Tenant-specific discovery doc — the concrete issuer, never common/organizations.
    metadata_url = oauth.entra._server_metadata_url
    assert metadata_url == settings.auth.server_metadata_url
    assert _TENANT in metadata_url
    assert "/v2.0/.well-known/openid-configuration" in metadata_url
    assert "common" not in metadata_url and "organizations" not in metadata_url
