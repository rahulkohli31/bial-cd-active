"""The `users` table — the long-awaited target `OwnedByUserMixin` already FKs.

A user is provisioned fresh on first Entra sign-in and keyed by the stable Entra
Object ID (`azure_oid`), NEVER by email (email is mutable and reassignable — R3).
No password and no `role` (roles are computed from the env allowlist, ADR-0005).

`suspended_at` is a LOCAL governance suspension (R10–R13), not a mirror of Entra
account state: a super-admin blocks a user platform-side while the Entra account
stays whatever IT made it. Entra offboarding itself is still honored by the
absolute session lifetime — this column never tries to shadow the identity
provider, so there is nothing to drift.

`token_version` is the instant-revocation lever (KD-6): the session JWT carries it,
`current_user` compares it against this column on every authenticated request, and
logout — and now deactivation — bumps it, invalidating every live session JWT for
the user at once.
"""

from __future__ import annotations

import datetime
from typing import Literal

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
    # Local suspension marker (R10): NULL = active, a timestamp = blocked since then.
    # Set/cleared ONLY by the super-admin deactivate/reactivate endpoints; enforced
    # fail-closed at the three auth seams (login callback, current_user, refresh).
    suspended_at: Mapped[datetime.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    # Pending-approval marker: NULL = never approved, a timestamp = approved since
    # then. Independent of `suspended_at` — an approved user can still be suspended,
    # and the two are never conflated in storage; only `status()` below derives a
    # single combined state. Set ONLY by the super-admin approve endpoint (or, for
    # the very first sign-in of a `superadmin_emails` allowlist address, by the SSO
    # callback itself — a superadmin can never be locked out for want of an approver).
    approved_at: Mapped[datetime.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )

    def status(self) -> Literal["pending", "approved", "disabled"]:
        """The single derived 3-state status — computed here so `/auth/me` and
        `GET /admin/users` can never disagree on it. `suspended_at` wins over a
        never-approved account: a disabled user is disabled regardless of whether
        they were ever approved."""
        if self.suspended_at is not None:
            return "disabled"
        if self.approved_at is None:
            return "pending"
        return "approved"
