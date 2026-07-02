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
