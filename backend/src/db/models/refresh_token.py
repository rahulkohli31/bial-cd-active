"""The `refresh_tokens` table — one row per issued refresh token.

Refresh reuse-detection is served entirely by columns denormalized onto each row
(`family_id` + `used_at` + `revoked` + `absolute_expires_at`) — there is no
separate `token_families` table (KD-7). A login mints a fresh family; each silent
rotation writes a NEW row sharing the parent's `family_id` and `absolute_expires_at`
and stamps the old row's `used_at`. Presenting an already-`used_at` (or `revoked`)
token is reuse -> the whole family is revoked (KD-5, AE2).

Only the SHA-256 hash of the opaque token is stored, never the token itself (R7):
a database read cannot reconstruct a usable credential.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base
from src.db.mixins import OwnedByUserMixin, TimestampMixin, UUIDv7PrimaryKeyMixin


class RefreshToken(UUIDv7PrimaryKeyMixin, TimestampMixin, OwnedByUserMixin, Base):
    __tablename__ = "refresh_tokens"

    # Groups every rotation of one login session. Indexed so a reuse-triggered
    # family revoke (UPDATE ... WHERE family_id = :fam) is a single indexed sweep.
    # App-supplied (uuid7 at login); NOT a column default — rotations copy the
    # parent's value rather than mint a new family.
    family_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, index=True, nullable=False)
    # SHA-256 hex digest (64 chars) of the opaque token — the lookup key. Unique so
    # a hash collision / duplicate insert fails loudly rather than aliasing rows.
    token_hash: Mapped[str] = mapped_column(sa.String(64), unique=True, index=True, nullable=False)
    # Per-token lifetime (rotates on each refresh).
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    # Family-wide hard cap, constant across rotations (~8h) — enforces AE3: past
    # this instant no rotation succeeds and a fresh Entra sign-in is required.
    absolute_expires_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False
    )
    # Set (once) when this token is rotated. NULL = unused. The atomic CAS pivots
    # on this column: `SET used_at = now() WHERE id = :id AND used_at IS NULL`.
    used_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    # Family-kill flag: logout or reuse-detection sets it across the whole family.
    revoked: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.false(), nullable=False)
