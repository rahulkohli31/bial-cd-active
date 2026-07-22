"""The `project_databases` table — one row per project that has (or is getting) its own
PostgreSQL database and login role (ADR-0028).

A dedicated table, not columns on `projects`, for three reasons: **absence is clean** (no
row at all means "never provisioned", so nothing has to distinguish NULL-because-new from
NULL-because-failed); the row doubles as the **concurrency claim** (`INSERT ... ON CONFLICT
DO NOTHING RETURNING` over the unique `project_id` elects exactly one racer to run the
external DDL sequence); and the encrypted role password lives away from the row every
project listing selects.

`db_ready` is the single TERMINAL marker and is committed LAST, after every external step
has succeeded. That ordering is the whole correctness argument: a crash between `CREATE
DATABASE` and `REVOKE CONNECT ... FROM PUBLIC` leaves the cross-app wall DOWN, so the row
must read as not-ready and the next ensure must re-run the entire (idempotent) sequence.
A marker that flipped early would claim a wall that does not exist.

`db_name` / `role_name` are STORED, not merely derived from `project_id`, so teardown and
the orphan reconciler keep working against rows minted under an older derivation.

No `OwnedByUserMixin`: the `projects` row is the ownership anchor and already carries
`user_id` (ADR-0004), so every user-facing query reaches this table through a join on
`projects` — the `user_id` predicate lives there and is never dropped here.
No `relationship()` — the repo uses explicit selects/joins everywhere.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base
from src.db.mixins import TimestampMixin, UUIDv7PrimaryKeyMixin

# PostgreSQL's identifier cap. The derived names are 40/41 chars (`names.py`), so the
# column is sized to the server's own ceiling rather than to today's prefix.
MAX_PG_IDENTIFIER = 63


class ProjectDatabase(UUIDv7PrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "project_databases"

    # One database per project, forever — this constraint IS the claim that serializes
    # concurrent provisioning (`ON CONFLICT ON CONSTRAINT ... DO NOTHING`).
    __table_args__ = (sa.UniqueConstraint("project_id", name="uq_project_databases_project"),)

    project_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid,
        sa.ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    # The provisioned database and its owning login role, as actually created.
    db_name: Mapped[str] = mapped_column(sa.String(MAX_PG_IDENTIFIER), nullable=False)
    role_name: Mapped[str] = mapped_column(sa.String(MAX_PG_IDENTIFIER), nullable=False)
    # Fernet token for the role's password (`services/appdb/secrets.py`). Encrypted at rest
    # because one role serves BOTH the sandbox and the deployed app, so it cannot be reset
    # on demand without cutting the live deployment off; reset survives only as the
    # deliberate rotation/leak-response lever. Text, not LargeBinary: a Fernet token is
    # urlsafe-base64 ASCII, and text keeps it greppable-out-of-a-dump-free of encoding games.
    password_encrypted: Mapped[str] = mapped_column(sa.Text, nullable=False)
    # THE terminal marker. False = the external sequence has not completed; the next ensure
    # re-runs all of it. Never set in the same commit as the claim.
    db_ready: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.text("false")
    )
    # Stamped in the same UPDATE that flips `db_ready`; NULL until then.
    provisioned_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
