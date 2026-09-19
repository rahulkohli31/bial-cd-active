"""The `token_usage` table — per-user daily token accounting.

One row per (user, IST calendar day, kind); the server-authoritative daily gate reconciles
each turn's spend against this ledger. The four token classes stay in SEPARATE columns so the
gate can COST-WEIGHT them: `input_tokens` already includes the two cache classes, so billable
is fresh input + output at face value, cache reads at ~10%, writes at ~125%
(`gate.py:billable_spend`) — never a re-add
of the cache columns on top (that double-counts the cached prefix).

The `(user_id, usage_date, kind)` uniqueness is the atomic upsert's conflict target, so
concurrent increments never lose an update. It does NOT close concurrent overspend — that
window is open by design; Redis token-bucket hardening is deferred."""

from __future__ import annotations

import datetime
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base
from src.db.mixins import OwnedByUserMixin, TimestampMixin, UUIDv7PrimaryKeyMixin


class TokenUsageKind(StrEnum):
    """What the spend was FOR. Values are the native PG enum labels.

    Keeps apart who generated the spend (every row, whatever its kind) from whose budget it
    comes out of (only `build` rows). `build` is the ONLY kind the daily gate and cap read;
    `review` (pre-publish classification) is metered but never counted against the cap — a
    heavy build day must not make an app unpublishable."""

    BUILD = "build"
    REVIEW = "review"


# Same convention as `app_status_enum` (app_registry.py): the migration (0031) owns
# CREATE/DROP TYPE explicitly, so the column must not try to create the type itself.
token_usage_kind_enum = sa.Enum(
    TokenUsageKind,
    name="token_usage_kind",
    values_callable=lambda enum: [member.value for member in enum],
    create_type=False,
)


class TokenUsage(UUIDv7PrimaryKeyMixin, TimestampMixin, OwnedByUserMixin, Base):
    __tablename__ = "token_usage"

    # One row per user per IST day PER KIND: the atomic upsert's conflict target.
    __table_args__ = (
        sa.UniqueConstraint("user_id", "usage_date", "kind", name="uq_token_usage_user_date_kind"),
    )

    # The IST calendar day (Asia/Kolkata, no DST) this row accounts for. The daily cap
    # resets at the next IST midnight. A DATE (not a datetime) — the day IS the key.
    usage_date: Mapped[datetime.date] = mapped_column(sa.Date, nullable=False)

    # What the spend was FOR (see `TokenUsageKind`). Defaults to `build` at BOTH layers
    # — ORM and database — because `build` is the DEFINED meaning of an unspecified
    # kind: every writer that predates the dimension was a build writer, so old call
    # sites and raw inserts keep their exact behaviour without being touched.
    kind: Mapped[TokenUsageKind] = mapped_column(
        token_usage_kind_enum,
        nullable=False,
        default=TokenUsageKind.BUILD,
        server_default=sa.text("'build'"),
    )

    # The four token classes, kept split so the daily gate can cost-weight them. BigInteger so
    # a busy day can never overflow. `input_tokens` is INCLUSIVE of the two cache classes
    # (pydantic-ai: `cache_read`/`cache_write` are sub-buckets already inside it); the gate
    # bills fresh input + output at face value, reads at ~10%, writes at ~125%
    # (`services/usage/gate.py:billable_spend`).
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
