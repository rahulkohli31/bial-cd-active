"""Refresh tokens — opaque generation + hash-at-rest (crypto), and (U5/U7) the
DB-backed family issuance + atomic rotation with reuse detection.

This module holds the pure crypto primitives now; `issue_new_family` (U5) and
`rotate_refresh_token` (U7) — the single compare-and-swap that closes the
rotation race the POC deferred to the backend (KD-5) — land alongside the
endpoints that use them.

A refresh token is a 256-bit random secret handed to exactly one client, NOT a
password: only its SHA-256 hash is stored (R7), and a fast unsalted hash is
correct — lookup is by exact hash match and there is nothing to brute-force that
guessing the 256-bit token wouldn't already break.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.db.models.refresh_token import RefreshToken
from src.db.models.user import User
from src.services.auth.errors import REASON_INVALID_REFRESH, REASON_SESSION_EXPIRED, AuthError

# 32 bytes -> ~43 URL-safe chars: high-entropy, cookie-safe, never guessable or
# sequential (ADR-0006).
_TOKEN_BYTES = 32


@dataclass(frozen=True, slots=True)
class RotationResult:
    """A successful rotation: the successor raw token (for the new cookie) plus
    the owner + current token_version (for re-minting the session JWT + CSRF)."""

    user_id: uuid.UUID
    token_version: int
    new_refresh_token: str


def generate_refresh_token() -> str:
    """A fresh opaque, URL-safe, high-entropy refresh token."""
    return secrets.token_urlsafe(_TOKEN_BYTES)


def hash_refresh_token(raw: str) -> str:
    """SHA-256 hex digest (64 chars) — the only form stored / compared. The raw
    token is never returned from here."""
    return hashlib.sha256(raw.encode()).hexdigest()


async def issue_new_family(db: AsyncSession, user_id: uuid.UUID) -> str:
    """Mint the FIRST refresh token of a brand-new family (login).

    Sets `absolute_expires_at = now + absolute_session_seconds` — the family-wide
    hard cap that every rotation inherits and that enforces the ~8h re-auth (AE3).
    Only the hash is persisted; the raw token is returned to the caller to place in
    the (path-scoped, HttpOnly) refresh cookie."""
    raw = generate_refresh_token()
    now = datetime.now(tz=UTC)
    db.add(
        RefreshToken(
            user_id=user_id,
            family_id=uuid.uuid7(),
            token_hash=hash_refresh_token(raw),
            expires_at=now + timedelta(seconds=settings.auth.refresh_ttl_seconds),
            absolute_expires_at=now + timedelta(seconds=settings.auth.absolute_session_seconds),
        )
    )
    await db.flush()
    return raw


async def _revoke_family(db: AsyncSession, family_id: uuid.UUID) -> None:
    # One indexed sweep kills every token in the family (KD-5).
    await db.execute(
        sa.update(RefreshToken).where(RefreshToken.family_id == family_id).values(revoked=True)
    )


async def rotate_refresh_token(db: AsyncSession, presented_hash: str) -> RotationResult:
    """Atomically rotate a refresh token, detecting reuse and enforcing the
    absolute lifetime (KD-5). Returns a `RotationResult` on success; raises
    `AuthError` (fail closed) on not-found / revoked / expired / reuse — after
    revoking the whole family on reuse, which the caller must commit.

    The single compare-and-swap `SET used_at = now() WHERE id = :id AND used_at IS
    NULL` is the ONLY reuse detector: a replay of an already-rotated token (whether
    a sequential attacker replay or a concurrent race) claims 0 rows and triggers a
    family revoke (AE2). This closes the read-then-write race the POC deferred to
    the backend (institutional learning, 2026-06-18)."""
    row = await db.scalar(select(RefreshToken).where(RefreshToken.token_hash == presented_hash))
    if row is None:
        raise AuthError("refresh token not recognized", reason=REASON_INVALID_REFRESH)

    now = datetime.now(tz=UTC)
    if row.revoked:
        # The family was already killed (a prior reuse or a logout).
        raise AuthError("refresh family revoked", reason=REASON_INVALID_REFRESH)
    if now >= row.absolute_expires_at:
        # AE3 — the ~8h family cap. No rotation past it; a fresh Entra sign-in is
        # required. (Constant across rotations, so every sibling is denied too.)
        raise AuthError("session absolute lifetime exceeded", reason=REASON_SESSION_EXPIRED)
    if now >= row.expires_at:
        raise AuthError("refresh token expired", reason=REASON_SESSION_EXPIRED)

    claim = (
        sa.update(RefreshToken)
        .where(RefreshToken.id == row.id, RefreshToken.used_at.is_(None))
        .values(used_at=now)
        .returning(RefreshToken.id)
    )
    if (await db.execute(claim)).first() is None:
        # 0 rows -> the token was already consumed: reuse (AE2). Kill the family.
        await _revoke_family(db, row.family_id)
        raise AuthError("refresh reuse detected", reason=REASON_INVALID_REFRESH)

    # Mint the successor in the SAME family, inheriting the absolute cap and a
    # fresh per-token expiry.
    new_raw = generate_refresh_token()
    db.add(
        RefreshToken(
            user_id=row.user_id,
            family_id=row.family_id,
            token_hash=hash_refresh_token(new_raw),
            expires_at=now + timedelta(seconds=settings.auth.refresh_ttl_seconds),
            absolute_expires_at=row.absolute_expires_at,
        )
    )
    await db.flush()

    user = await db.get(User, row.user_id)
    if user is None:  # the FK makes this unreachable, but fail closed rather than assume.
        raise AuthError("refresh owner missing", reason=REASON_INVALID_REFRESH)
    return RotationResult(
        user_id=row.user_id, token_version=user.token_version, new_refresh_token=new_raw
    )
