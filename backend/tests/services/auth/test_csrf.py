"""CSRF signed double-submit — session-bound, fail-closed unit tests (U3)."""

from __future__ import annotations

import uuid

import pytest

from src.services.auth.csrf import issue_csrf_token, verify_csrf


def test_valid_pair_verifies() -> None:
    uid = uuid.uuid7()
    token = issue_csrf_token(uid, token_version=2)
    assert verify_csrf(token, token, uid, token_version=2) is True


def test_mismatched_header_fails() -> None:
    uid = uuid.uuid7()
    token = issue_csrf_token(uid, token_version=2)
    other = issue_csrf_token(uid, token_version=2)
    assert verify_csrf(token, other, uid, token_version=2) is False
    assert verify_csrf(token, "totally-wrong", uid, token_version=2) is False


def test_stale_token_version_fails() -> None:
    # A token minted before a token_version bump (logout / revocation) no longer
    # verifies — CSRF tokens die with the session.
    uid = uuid.uuid7()
    token = issue_csrf_token(uid, token_version=1)
    assert verify_csrf(token, token, uid, token_version=2) is False


def test_wrong_user_fails() -> None:
    uid = uuid.uuid7()
    token = issue_csrf_token(uid, token_version=1)
    assert verify_csrf(token, token, uuid.uuid7(), token_version=1) is False


def test_tampered_signature_fails() -> None:
    uid = uuid.uuid7()
    token = issue_csrf_token(uid, token_version=1)
    nonce, _, sig = token.partition(".")
    tampered = f"{nonce}.{sig[:-1]}{'a' if sig[-1] != 'a' else 'b'}"
    assert verify_csrf(tampered, tampered, uid, token_version=1) is False


@pytest.mark.parametrize("bad", ["", "no-dot", ".", "nonce.", ".sig"])
def test_malformed_inputs_fail_closed(bad: str) -> None:
    uid = uuid.uuid7()
    assert verify_csrf(bad, bad, uid, token_version=1) is False
