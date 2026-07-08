"""The `user_limits` table — sparse per-user limit overrides (R13/R30).

Absent row (or a NULL column) → the global default (`settings.DAILY_TOKEN_LIMIT` for the
daily cap). Mirrors the Express sparse per-user `limits` object (`server/limits.js`
`resolveUserLimits`): each override is nullable and NULL means "use the default". The
daily gate reads `daily_token_limit`; the context limits are SPA per-conversation
guardrails (not part of the daily gate) carried here so the admin limits-patch (Plan B
B.U9) writes the whole cohesive `limits` unit without a later schema change.

One row per user (unique `user_id`) — an override is an attribute OF a user, not an
event log.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base
from src.db.mixins import OwnedByUserMixin, TimestampMixin, UUIDv7PrimaryKeyMixin


class UserLimit(UUIDv7PrimaryKeyMixin, TimestampMixin, OwnedByUserMixin, Base):
    __tablename__ = "user_limits"

    # One override row per user (the mixin's user_id is indexed; this makes it unique).
    __table_args__ = (sa.UniqueConstraint("user_id", name="uq_user_limits_user"),)

    # NULL = use the global default. The daily gate consults this first (R13).
    daily_token_limit: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    # SPA per-conversation context guardrails (NOT the daily gate). Carried for the
    # admin limits-patch; both NULL → the SPA falls back to its defaults.
    context_soft_limit: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    context_hard_limit: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
