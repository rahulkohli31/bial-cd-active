"""The `token_usage` table — per-user daily token accounting (R13/R30).

One row per (user, IST calendar day). The server-authoritative daily gate reconciles
each turn's spend against this ledger. Mirrors Express `server/usage-repo.js`, which
keeps one Cosmos doc per `username:istDateKey` and `$inc`s an `inputTokens` (with cache
folded in) + `outputTokens` pair. Here the four token classes stay in SEPARATE columns
for observability; the daily gate folds them back together at read time
(`services/usage/gate.py`) so the cap counts cache tokens exactly as Express does.

The `(user_id, usage_date)` uniqueness is the conflict target for the atomic
`INSERT … ON CONFLICT … DO UPDATE` that records usage with the add in SQL — parity with
Express's atomic `$inc` (no lost-update under-count). It does NOT close concurrent
overspend (the check-before-stream / bill-after-stream window is open by design; Redis
token-bucket hardening deferred).
"""

from __future__ import annotations

import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base
from src.db.mixins import OwnedByUserMixin, TimestampMixin, UUIDv7PrimaryKeyMixin


class TokenUsage(UUIDv7PrimaryKeyMixin, TimestampMixin, OwnedByUserMixin, Base):
    __tablename__ = "token_usage"

    # One row per user per IST day: the atomic upsert's conflict target.
    __table_args__ = (
        sa.UniqueConstraint("user_id", "usage_date", name="uq_token_usage_user_date"),
    )

    # The IST calendar day (Asia/Kolkata, no DST) this row accounts for. The daily cap
    # resets at the next IST midnight. A DATE (not a datetime) — the day IS the key.
    usage_date: Mapped[datetime.date] = mapped_column(sa.Date, nullable=False)

    # The four token classes, kept split for observability. BigInteger so a busy day can
    # never overflow. The daily gate's `used` folds all four (input + output + cache
    # read + cache write) — Express folds cache into inputTokens at write time; splitting
    # the columns must NOT silently loosen the cap, so the fold is asserted in a test.
    input_tokens: Mapped[int] = mapped_column(
        sa.BigInteger, server_default=sa.text("0"), nullable=False
    )
    output_tokens: Mapped[int] = mapped_column(
        sa.BigInteger, server_default=sa.text("0"), nullable=False
    )
    cache_read_tokens: Mapped[int] = mapped_column(
        sa.BigInteger, server_default=sa.text("0"), nullable=False
    )
    cache_write_tokens: Mapped[int] = mapped_column(
        sa.BigInteger, server_default=sa.text("0"), nullable=False
    )
