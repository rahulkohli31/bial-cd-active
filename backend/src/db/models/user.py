"""The `users` table — the long-awaited target `OwnedByUserMixin` already FKs.

Provisioned fresh on first Entra sign-in, keyed by the stable Entra Object ID (`azure_oid`),
NEVER by email (mutable, reassignable). No password, no `role` (computed from the env allowlist).

`suspended_at` is a LOCAL governance suspension, not a mirror of Entra state — a super-admin
blocks a user platform-side while the Entra account stays whatever IT made it, so there is
nothing to drift. `token_version` is the instant-revocation lever: the session JWT carries it,
`current_user` compares it every request, and logout (or deactivation) bumps it, invalidating
every live session JWT at once.
"""

from __future__ import annotations

import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base
from src.db.mixins import TimestampMixin, UUIDv7PrimaryKeyMixin


class User(UUIDv7PrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"

    # The stable Entra Object ID (a GUID). The upsert key — unique + indexed so a
    # returning sign-in resolves to the same row and a concurrent first sign-in
    # can't create a duplicate. A natural string key, NOT UUIDv7-wrapped (UUIDv7 is
    # reserved for OUR primary keys; this is an external identifier).
    azure_oid: Mapped[str] = mapped_column(sa.String(64), unique=True, index=True, nullable=False)
    # NOT NULL: the validator guarantees a non-null value (email claim, else the
    # preferred_username fallback, else fail-closed), so provisioning never
    # hits a NOT NULL violation.
    email: Mapped[str] = mapped_column(sa.String(320), nullable=False)
    # The Entra UPN (`preferred_username`), captured unconditionally as the
    # deterministic join key for the deferred POC->Postgres migration (POC users
    # are keyed by username, not oid/email). Nullable: distinct from email.
    upn: Mapped[str | None] = mapped_column(sa.String(320), nullable=True)
    display_name: Mapped[str | None] = mapped_column(sa.String(256), nullable=True)
    # Instant-revocation counter. Server default 0 so a raw insert also
    # starts a fresh user at version 0.
    token_version: Mapped[int] = mapped_column(
        sa.Integer, server_default=sa.text("0"), nullable=False
    )
    # Local suspension marker: NULL = active, a timestamp = blocked since then.
    # Set/cleared ONLY by the super-admin deactivate/reactivate endpoints; enforced
    # fail-closed at the three auth seams (login callback, current_user, refresh).
    suspended_at: Mapped[datetime.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    # Ever signed in, not signed in now. Server default true: only a user created from the
    # directory starts false, and sign-in sets it true for good.
    has_signed_in: Mapped[bool] = mapped_column(
        sa.Boolean, server_default=sa.text("true"), nullable=False
    )
