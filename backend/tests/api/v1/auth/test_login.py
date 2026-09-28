"""GET /auth/login — the redirect to Entra with PKCE.

The tenant discovery doc is pre-seeded onto the real OAuth registry (`tests/entra.py`) so
`authorize_redirect` builds the authorize URL locally with NO network fetch.
"""

from __future__ import annotations

from urllib.parse import quote

import pytest

from src.config import settings
from tests.entra import seed_discovery

_TID = settings.auth.tenant_id


async def test_login_redirects_to_entra_with_pkce(client) -> None:
    seed_discovery()
    resp = await client.get("/v1/auth/login")

    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith(f"https://login.microsoftonline.com/{_TID}/oauth2/v2.0/authorize")
    assert "code_challenge=" in location
    assert "code_challenge_method=S256" in location
    assert "state=" in location
    assert f"redirect_uri={quote(settings.auth.redirect_uri, safe='')}" in location


async def test_login_sets_transient_state_cookie(client) -> None:
    seed_discovery()
    resp = await client.get("/v1/auth/login")
    # The PKCE verifier / nonce / state live in the oauth_transient cookie (its own
    # SessionMiddleware name — never colliding with the app session cookie).
    set_cookie = "\n".join(resp.headers.get_list("set-cookie"))
    assert "oauth_transient=" in set_cookie
    assert "session=" not in set_cookie  # no app session cookie minted yet


async def test_login_passes_no_prompt_by_default(client) -> None:
    """An ordinary sign-in lets Entra reuse the browser session — the fast path most users take."""
    seed_discovery()
    resp = await client.get("/v1/auth/login")
    assert "prompt=" not in resp.headers["location"]


async def test_login_with_prompt_login_forces_a_fresh_sign_in(client) -> None:
    """The login page's button after `reauth_required` asks Entra to authenticate again instead of
    re-minting a code from the browser session whose MFA has expired."""
    seed_discovery()
    resp = await client.get("/v1/auth/login", params={"prompt": "login"})
    assert resp.status_code == 302
    assert "prompt=login" in resp.headers["location"]


@pytest.mark.parametrize("value", ["consent", "none", "select_account", "login consent"])
async def test_login_forwards_no_other_prompt_value(client, value: str) -> None:
    """Only the one value the step-up needs is honoured; the query string cannot steer Entra into
    anything else (`none` would turn a sign-in into a silent failure, `consent` into a prompt)."""
    seed_discovery()
    resp = await client.get("/v1/auth/login", params={"prompt": value})
    assert "prompt=" not in resp.headers["location"]
