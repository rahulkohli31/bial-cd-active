"""Refresh-token crypto (U3) + DB-backed rotation / reuse-detection (U7).

The pure primitives are exercised first; the `rotate_refresh_token` service — the
atomic CAS + family reuse-detection + absolute-lifetime enforcement — is tested
directly against the real schema (KD-5, ADR-0010: a realized service-layer test,
not latent).
"""

from __future__ import annotations

import string
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select

from src.db.models.refresh_token import RefreshToken
from src.services.auth.errors import AuthError
from src.services.auth.refresh import (
    generate_refresh_token,
    hash_refresh_token,
    issue_new_family,
    rotate_refresh_token,
)
from tests.factories import UserFactory

_URL_SAFE = set(string.ascii_letters + string.digits + "-_")


async def _insert_token(
    db: Any,
    user_id: uuid.UUID,
    *,
    family_id: uuid.UUID | None = None,
    absolute_delta: timedelta = timedelta(hours=8),
    expires_delta: timedelta = timedelta(days=7),
    used_at: datetime | None = None,
    revoked: bool = False,
) -> str:
    """Insert a refresh token with fully-controlled timing/state; return its raw."""
    raw = generate_refresh_token()
    now = datetime.now(tz=UTC)
    db.add(
        RefreshToken(
            user_id=user_id,
            family_id=family_id or uuid.uuid7(),
            token_hash=hash_refresh_token(raw),
            expires_at=now + expires_delta,
            absolute_expires_at=now + absolute_delta,
            used_at=used_at,
            revoked=revoked,
        )
    )
    await db.flush()
    return raw


async def _family_all_revoked(db: Any, user_id: uuid.UUID) -> bool:
    # Aggregate reads hit the DB (not the identity map), so they see the Core UPDATE.
    total = await db.scalar(
        select(func.count()).select_from(RefreshToken).where(RefreshToken.user_id == user_id)
    )
    revoked = await db.scalar(
        select(func.count())
        .select_from(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked.is_(True))
    )
    return total >= 1 and total == revoked


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


# --- DB rotation / reuse / absolute lifetime (U7) -------------------------------


async def test_happy_rotation_marks_old_used_and_keeps_family(db_session) -> None:
    user = await UserFactory.create(db_session)
    raw = await issue_new_family(db_session, user.id)
    old = await db_session.scalar(
        select(RefreshToken).where(RefreshToken.token_hash == hash_refresh_token(raw))
    )

    result = await rotate_refresh_token(db_session, hash_refresh_token(raw))

    assert result.user_id == user.id
    assert result.token_version == user.token_version
    assert result.new_refresh_token != raw
    # Old token is now consumed; the successor shares family + absolute cap.
    await db_session.refresh(old)
    assert old.used_at is not None
    successor = await db_session.scalar(
        select(RefreshToken).where(
            RefreshToken.token_hash == hash_refresh_token(result.new_refresh_token)
        )
    )
    assert successor.family_id == old.family_id
    assert successor.absolute_expires_at == old.absolute_expires_at


async def test_reuse_revokes_entire_family(db_session) -> None:
    # AE2 — the security-critical path (execution note: reuse detection).
    user = await UserFactory.create(db_session)
    raw = await issue_new_family(db_session, user.id)
    result = await rotate_refresh_token(db_session, hash_refresh_token(raw))

    # Replaying the already-rotated original -> 0-rows CAS -> reuse -> family revoke.
    with pytest.raises(AuthError):
        await rotate_refresh_token(db_session, hash_refresh_token(raw))
    assert await _family_all_revoked(db_session, user.id)

    # The legitimate successor is now dead too (its family was revoked).
    with pytest.raises(AuthError):
        await rotate_refresh_token(db_session, hash_refresh_token(result.new_refresh_token))


async def test_absolute_lifetime_exceeded_denies(db_session) -> None:
    # AE3 — structurally valid, unused token, but the family's 8h cap has passed.
    user = await UserFactory.create(db_session)
    raw = await _insert_token(db_session, user.id, absolute_delta=timedelta(hours=-1))
    with pytest.raises(AuthError):
        await rotate_refresh_token(db_session, hash_refresh_token(raw))


async def test_per_token_expiry_denies(db_session) -> None:
    user = await UserFactory.create(db_session)
    raw = await _insert_token(db_session, user.id, expires_delta=timedelta(seconds=-1))
    with pytest.raises(AuthError):
        await rotate_refresh_token(db_session, hash_refresh_token(raw))


async def test_revoked_token_denied(db_session) -> None:
    user = await UserFactory.create(db_session)
    raw = await _insert_token(db_session, user.id, revoked=True)
    with pytest.raises(AuthError):
        await rotate_refresh_token(db_session, hash_refresh_token(raw))


async def test_unknown_token_denied(db_session) -> None:
    with pytest.raises(AuthError):
        await rotate_refresh_token(db_session, hash_refresh_token("never-issued"))
