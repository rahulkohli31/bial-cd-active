"""The `users` table — the long-awaited target `OwnedByUserMixin` already FKs.

A user is provisioned fresh on first Entra sign-in and keyed by the stable Entra
Object ID (`azure_oid`), NEVER by email (email is mutable and reassignable — R3).
No password, no `role` (RBAC is a later phase), and no local `is_active` flag:
Entra offboarding is honored by the absolute session lifetime, not a mirrored
enabled/disabled bit that could drift from the identity provider.

`token_version` is the instant-revocation lever (KD-6): the session JWT carries it,
`current_user` compares it against this column on every authenticated request, and
logout bumps it — invalidating every live session JWT for the user at once.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base
from src.db.mixins import TimestampMixin, UUIDv7PrimaryKeyMixin


class User(UUIDv7PrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"

    # The stable Entra Object ID (a GUID). The upsert key — unique + indexed so a
    # returning sign-in resolves to the same row and a concurrent first sign-in
    # can't create a duplicate. A natural string key, NOT UUIDv7-wrapped (ADR-0006
    # reserves UUIDv7 for OUR primary keys; this is an external identifier).
    azure_oid: Mapped[str] = mapped_column(sa.String(64), unique=True, index=True, nullable=False)
    # NOT NULL: the U4 validator guarantees a non-null value (email claim, else the
    # preferred_username fallback, else fail-closed — KD-3), so provisioning never
    # hits a NOT NULL violation.
    email: Mapped[str] = mapped_column(sa.String(320), nullable=False)
    # The Entra UPN (`preferred_username`), captured unconditionally as the
    # deterministic join key for the deferred POC->Postgres migration (POC users
    # are keyed by username, not oid/email — KD-3). Nullable: distinct from email.
    upn: Mapped[str | None] = mapped_column(sa.String(320), nullable=True)
    display_name: Mapped[str | None] = mapped_column(sa.String(256), nullable=True)
    # Instant-revocation counter (KD-6). Server default 0 so a raw insert also
    # starts a fresh user at version 0.
    token_version: Mapped[int] = mapped_column(
        sa.Integer, server_default=sa.text("0"), nullable=False
    )
