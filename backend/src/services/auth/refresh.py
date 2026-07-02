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
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.db.models.refresh_token import RefreshToken

# 32 bytes -> ~43 URL-safe chars: high-entropy, cookie-safe, never guessable or
# sequential (ADR-0006).
_TOKEN_BYTES = 32


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
