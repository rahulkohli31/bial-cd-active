"""Session JWT mint/decode — pinned-algorithm, fail-closed unit tests (U3)."""

from __future__ import annotations

import base64
import json
import time
import uuid
from typing import Any

import pytest
from joserfc import jwt
from joserfc.jwk import OctKey

from src.config import settings
from src.services.auth.errors import AuthError
from src.services.auth.session_jwt import decode_session_jwt, mint_session_jwt


def _key(secret: str | None = None) -> OctKey:
    return OctKey.import_key(secret or settings.auth.session_secret.get_secret_value())


def _valid_claims(uid: uuid.UUID, token_version: int = 0) -> dict[str, Any]:
    now = int(time.time())
    return {"sub": str(uid), "token_version": token_version, "iat": now, "exp": now + 900}


def _b64(payload: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()


def test_mint_decode_roundtrips_sub_and_version() -> None:
    uid = uuid.uuid7()
    token = mint_session_jwt(uid, token_version=3, ttl_seconds=900)
    claims = decode_session_jwt(token)
    assert claims.user_id == uid
    assert claims.token_version == 3


def test_expired_token_rejected() -> None:
    uid = uuid.uuid7()
    token = mint_session_jwt(uid, token_version=0, ttl_seconds=-3600)  # already expired
    with pytest.raises(AuthError):
        decode_session_jwt(token)


def test_wrong_key_rejected() -> None:
    uid = uuid.uuid7()
    forged = jwt.encode({"alg": "HS256", "typ": "JWT"}, _valid_claims(uid), _key("x" * 40))
    with pytest.raises(AuthError):
        decode_session_jwt(forged)


def test_tampered_payload_rejected() -> None:
    uid = uuid.uuid7()
    token = mint_session_jwt(uid, token_version=0, ttl_seconds=900)
    header, payload, sig = token.split(".")
    # Flip a character in the payload segment — signature no longer matches.
    tampered_payload = payload[:-1] + ("A" if payload[-1] != "A" else "B")
    with pytest.raises(AuthError):
        decode_session_jwt(f"{header}.{tampered_payload}.{sig}")


def test_alg_none_forgery_rejected() -> None:
    uid = uuid.uuid7()
    forged = f"{_b64({'alg': 'none', 'typ': 'JWT'})}.{_b64(_valid_claims(uid))}."
    with pytest.raises(AuthError):
        decode_session_jwt(forged)


def test_missing_token_version_rejected() -> None:
    uid = uuid.uuid7()
    now = int(time.time())
    # Correctly signed, but our own token_version claim is absent.
    claims = {"sub": str(uid), "iat": now, "exp": now + 900}
    token = jwt.encode({"alg": "HS256", "typ": "JWT"}, claims, _key())
    with pytest.raises(AuthError):
        decode_session_jwt(token)


def test_boolean_token_version_rejected() -> None:
    # bool is an int subclass — guard against True/False slipping through.
    uid = uuid.uuid7()
    claims = {**_valid_claims(uid), "token_version": True}
    token = jwt.encode({"alg": "HS256", "typ": "JWT"}, claims, _key())
    with pytest.raises(AuthError):
        decode_session_jwt(token)


@pytest.mark.parametrize("garbage", ["", "not-a-jwt", "a.b", "a.b.c.d"])
def test_malformed_token_rejected(garbage: str) -> None:
    with pytest.raises(AuthError):
        decode_session_jwt(garbage)
