"""GET /auth/login — the redirect to Entra with PKCE (U5).

The tenant discovery doc is pre-seeded onto the real OAuth registry (KD-9) so
`authorize_redirect` builds the authorize URL locally with NO network fetch.
"""

from __future__ import annotations

from urllib.parse import quote

from src.config import settings
from src.services.auth.oidc import get_oauth

_TID = settings.auth.tenant_id

# A minimal Entra discovery doc. The `_loaded_at` key marks it as already fetched,
# so Authlib's load_server_metadata() skips the network call (KD-9).
_DISCOVERY = {
    "issuer": f"https://login.microsoftonline.com/{_TID}/v2.0",
    "authorization_endpoint": f"https://login.microsoftonline.com/{_TID}/oauth2/v2.0/authorize",
    "token_endpoint": f"https://login.microsoftonline.com/{_TID}/oauth2/v2.0/token",
    "jwks_uri": f"https://login.microsoftonline.com/{_TID}/discovery/v2.0/keys",
    "response_types_supported": ["code"],
    "grant_types_supported": ["authorization_code", "refresh_token"],
    "code_challenge_methods_supported": ["S256"],
    "_loaded_at": 1_700_000_000.0,
}


def _seed_discovery() -> None:
    metadata = get_oauth().entra.server_metadata
    metadata.clear()
    metadata.update(_DISCOVERY)


async def test_login_redirects_to_entra_with_pkce(client) -> None:
    _seed_discovery()
    resp = await client.get("/v1/auth/login")

    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith(f"https://login.microsoftonline.com/{_TID}/oauth2/v2.0/authorize")
    # PKCE (S256) + state present.
    assert "code_challenge=" in location
    assert "code_challenge_method=S256" in location
    assert "state=" in location
    # redirect_uri equals the configured AUTH__REDIRECT_URI, byte-for-byte (KD-8).
    assert f"redirect_uri={quote(settings.auth.redirect_uri, safe='')}" in location


async def test_login_sets_transient_state_cookie(client) -> None:
    _seed_discovery()
    resp = await client.get("/v1/auth/login")
    # The PKCE verifier / nonce / state live in the oauth_transient cookie (its own
    # SessionMiddleware name — never colliding with the app session cookie, KD-4).
    set_cookie = "\n".join(resp.headers.get_list("set-cookie"))
    assert "oauth_transient=" in set_cookie
    assert "session=" not in set_cookie  # no app session cookie minted yet
