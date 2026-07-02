"""Refresh-token crypto — generation + hash-at-rest unit tests (U3).

The DB-backed rotation / reuse-detection cases land in U7 alongside
`rotate_refresh_token`; this file covers the pure primitives.
"""

from __future__ import annotations

import string

from src.services.auth.refresh import generate_refresh_token, hash_refresh_token

_URL_SAFE = set(string.ascii_letters + string.digits + "-_")


def test_hash_is_deterministic_and_64_hex() -> None:
    digest = hash_refresh_token("a-known-token")
    assert digest == hash_refresh_token("a-known-token")
    assert len(digest) == 64
    assert all(c in string.hexdigits for c in digest)


def test_generated_tokens_are_url_safe_high_entropy_and_unique() -> None:
    tokens = {generate_refresh_token() for _ in range(200)}
    assert len(tokens) == 200  # no collisions across 200 draws
    for token in tokens:
        assert len(token) >= 43  # 32 bytes base64url-encoded
        assert set(token) <= _URL_SAFE


def test_raw_token_is_not_recoverable_from_hash() -> None:
    raw = generate_refresh_token()
    digest = hash_refresh_token(raw)
    assert digest != raw
    assert raw not in digest
    assert len(digest) == 64
