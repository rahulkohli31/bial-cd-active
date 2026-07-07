"""Runner-token mint/verify (U3, P1): a fresh short-lived token minted server-side
from the session — the cookie's own JWT/refresh is never handed to JS."""

from __future__ import annotations

import uuid

from src.services.appserving.runner_token import mint_runner_token, verify_runner_token


def test_mint_then_verify_roundtrips_identity() -> None:
    user_id = uuid.uuid7()
    token = mint_runner_token(user_id, token_version=3)
    claims = verify_runner_token(token)
    assert claims.user_id == user_id
    assert claims.token_version == 3


def test_minted_token_is_a_signed_string() -> None:
    token = mint_runner_token(uuid.uuid7(), token_version=0)
    # A compact JWS (header.payload.signature) — a signed token, not the raw cookie.
    assert isinstance(token, str)
    assert token.count(".") == 2
