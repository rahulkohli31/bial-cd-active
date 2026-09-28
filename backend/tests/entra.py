"""A stand-in Entra for sign-in tests.

The real Authlib registry runs against a discovery document seeded so nothing reaches the network,
and ID tokens are signed with a key only the tests hold. So the callback's state check and the
ID token's signature, issuer, audience, expiry and nonce checks all run for real.
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
from joserfc import jwt
from joserfc.jwk import KeySet, RSAKey

from src.config import settings
from src.services.auth.oidc import get_oauth

TENANT = settings.auth.tenant_id
ISSUER = f"https://login.microsoftonline.com/{TENANT}/v2.0"
TOKEN_ENDPOINT = f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token"
# The same key id on both, so a token signed with the foreign key is looked up, found, and fails
# its signature — a different id would send Authlib to the network for fresh keys.
SIGNING_KEY = RSAKey.generate_key(2048, parameters={"kid": "entra-test"}, private=True)
FOREIGN_KEY = RSAKey.generate_key(2048, parameters={"kid": "entra-test"}, private=True)
# Marks a claim to leave out of the token.
ABSENT: Any = object()

# `_loaded_at` marks the document as already fetched, so Authlib's load_server_metadata() and
# fetch_jwk_set() make no network call.
DISCOVERY: dict[str, Any] = {
    "issuer": ISSUER,
    "authorization_endpoint": f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/authorize",
    "token_endpoint": TOKEN_ENDPOINT,
    "jwks_uri": f"https://login.microsoftonline.com/{TENANT}/discovery/v2.0/keys",
    "response_types_supported": ["code"],
    "grant_types_supported": ["authorization_code", "refresh_token"],
    "code_challenge_methods_supported": ["S256"],
    "id_token_signing_alg_values_supported": ["RS256"],
    "jwks": KeySet([SIGNING_KEY]).as_dict(private=False),
    "_loaded_at": 1_700_000_000.0,
}


def seed_discovery() -> None:
    metadata = get_oauth().entra.server_metadata
    metadata.clear()
    metadata.update(DISCOVERY)


def authorize_params(location: str) -> dict[str, str]:
    """The query of an authorize redirect, one value per key."""
    return {key: values[0] for key, values in parse_qs(urlsplit(location).query).items()}


async def start_sign_in(client: httpx.AsyncClient, **params: str) -> tuple[str, str]:
    """Begin a sign-in in this client's browser; returns the state and nonce Entra would echo."""
    seed_discovery()
    resp = await client.get("/v1/auth/login", params=params)
    sent = authorize_params(resp.headers["location"])
    return sent["state"], sent["nonce"]


def id_token(sent_nonce: str, *, key: RSAKey = SIGNING_KEY, **claims: Any) -> str:
    """An ID token for the sign-in that sent `sent_nonce`; any claim can be overridden."""
    now = int(time.time())
    payload: dict[str, Any] = {
        "iss": ISSUER,
        "aud": settings.auth.client_id,
        "iat": now,
        "nbf": now,
        "exp": now + 3600,
        "nonce": sent_nonce,
        "oid": "entra-oid-new",
        "sub": "entra-sub-new",
        "tid": TENANT,
        "email": "citizen@rvaiglobal.com",
        "preferred_username": "citizen@rvaiglobal.com",
        "name": "A Citizen",
    }
    payload.update(claims)
    payload = {name: value for name, value in payload.items() if value is not ABSENT}
    return jwt.encode({"alg": "RS256", "kid": key.kid}, payload, key)


async def complete(client: httpx.AsyncClient, state: str, **form: str) -> httpx.Response:
    """What the callback page posts once Entra's token endpoint has answered the browser."""
    return await client.post("/v1/auth/complete", data={"state": state, **form})


async def sign_in(client: httpx.AsyncClient, **claims: Any) -> httpx.Response:
    """The whole round trip — login, the callback page, and the page's post — for one identity."""
    state, nonce = await start_sign_in(client)
    page = await client.get("/v1/auth/callback", params={"code": "entra-code", "state": state})
    assert page.status_code == 200, page.headers.get("location")
    return await complete(client, state, id_token=id_token(nonce, **claims))
