"""The `clear_data_tokens` table — a durable single-use clear-data confirm token
(R29; ADR-0006).

Express gated the destructive clear-data with an in-memory per-instance `Map` token
that a stateless / multi-worker backend would lose. This DB-backed row survives a
worker restart: `data-summary` mints one (2-minute TTL, app-bound), and `clear-data`
redeems it with an ATOMIC single-use `UPDATE ... WHERE used_at IS NULL AND expires_at
> now() RETURNING` so a replay or an expired/foreign token cannot re-fire the purge.
The token value is a `secrets.token_urlsafe` secret, never a raw UUID.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base
from src.db.mixins import TimestampMixin, UUIDv7PrimaryKeyMixin

# 2-minute TTL (Express CLEAR_TOKEN_TTL_MS).
CLEAR_TOKEN_TTL_SECONDS = 2 * 60


def mint_confirm_token() -> str:
    """A 128-bit url-safe confirm token (Express `randomBytes(16).base64url`)."""
    return secrets.token_urlsafe(16)


class ClearDataToken(UUIDv7PrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "clear_data_tokens"

    # The confirm secret — unique + indexed for the single point-read at redeem.
    token: Mapped[str] = mapped_column(sa.String(64), unique=True, index=True, nullable=False)
    # App-bound: a token minted for app A can never clear app B.
    app_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid,
        sa.ForeignKey("app_registry.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Actor-bound: the admin who minted it. The redeem requires actor_id == admin.id so a
    # token minted by one admin can never be redeemed under another admin's identity.
    actor_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid,
        sa.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    # Single-use marker; the atomic redeem stamps it and refuses an already-stamped row.
    used_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
