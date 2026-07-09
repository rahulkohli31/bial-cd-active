"""The `app_registry` table — the app-lifecycle spine of the deployed-app data
plane (R18, R4; ADR-0004, ADR-0006, ADR-0008).

One row per generated app. The row's `id` IS the appId (`AE3` identifier
preservation): a new app gets a UUIDv7 PK; the later one-time Cosmos→Postgres
backfill inserts a migrated app carrying its *existing* appId directly as the PK —
no `legacy_id`, no dual-lookup — so already-deployed apps and previously-issued
URLs keep resolving. `app_key` is a `secrets.token_urlsafe` label, NEVER a raw
UUID (ADR-0006); it is a publishable scoping key (the Stripe publishable-key
model), not a secret — the real wall is the IP-restricted network.

Ownership is the single-tenant boundary (`OwnedByUserMixin` → `user_id`, ADR-0004):
every query over an app is scoped by the owning `user_id`. The app is **project-scoped**
(KD-4): `project_id` is a NOT NULL FK and `uq_app_registry_project` enforces exactly
ONE app per project (a project is one tool = one codebase). `conversation_id` is
repurposed from the old 1:1 `id == conversation_id` identity into a SOFT head pointer
to the LAST builder session that touched the app — a plain indexed UUID with no FK;
the ownership/isolation predicate is `user_id`, never the conversation.

`status` is a native PG enum (ADR-0008). Snapshots are JSONB: `source_snapshot`
holds the latest submitted build (client-compiled artifact included, R19/AE4);
`approved_snapshot` holds the last super-admin-approved artifact the runner serves.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base
from src.db.mixins import OwnedByUserMixin, TimestampMixin, UUIDv7PrimaryKeyMixin


class AppStatus(StrEnum):
    """The app lifecycle states (Express `APP_STATUSES`, verbatim strings). Values
    are the native PG enum labels — stable, professional, safe in API responses."""

    DRAFT = "draft"
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    DISABLED = "disabled"


# The native PG enum type, shared by the model column and the Alembic migration.
# `create_type=False`: the migration owns CREATE/DROP TYPE explicitly (so a
# downgrade drops it) — the column must not try to create the type itself.
app_status_enum = sa.Enum(
    AppStatus,
    name="app_status",
    values_callable=lambda enum: [member.value for member in enum],
    create_type=False,
)


# The lifecycle state machine (Express `ALLOWED_FROM`, verbatim): target status →
# the set of source statuses a transition INTO it is allowed from. `draft` is not a
# transition target — it is minted only by provision. A transition is applied as an
# atomic `UPDATE ... WHERE status = ANY(allowed)`; zero rows updated is a rejected
# (illegal) transition (→ 409), never a silent no-op.
STATUS_TRANSITIONS: dict[AppStatus, frozenset[AppStatus]] = {
    AppStatus.PENDING: frozenset({AppStatus.DRAFT, AppStatus.REJECTED, AppStatus.APPROVED}),
    AppStatus.APPROVED: frozenset({AppStatus.PENDING, AppStatus.DISABLED}),
    AppStatus.REJECTED: frozenset({AppStatus.PENDING}),
    AppStatus.DISABLED: frozenset({AppStatus.APPROVED}),
}

# The X-App-Key chain treats these statuses as live (Express `ACTIVE_STATUSES`).
# `disabled` (kill-switch) and `rejected` are refused (403) at the data plane.
ACTIVE_STATUSES: frozenset[AppStatus] = frozenset(
    {AppStatus.DRAFT, AppStatus.PENDING, AppStatus.APPROVED}
)

# Publishable app-key shape (Express `bial_${randomBytes(24).base64url}`): the
# `bial_` prefix + 32 url-safe chars. token_urlsafe(24) yields the identical shape
# (base64url of 24 bytes, no padding). NEVER a raw UUID (ADR-0006).
_APP_KEY_PREFIX = "bial_"

MAX_APP_NAME = 120

# Per-app quota ceilings (Express `app-registry-repo.js`), enforced by atomic
# conditional reserves against the counter columns above.
APP_RECORD_COUNT_CAP = 50_000
APP_DATA_BYTES_CAP = 100 * 1024 * 1024  # 100 MB of record data per app
APP_FILE_COUNT_CAP = 2000
APP_FILE_BYTES_CAP = 500 * 1024 * 1024  # 500 MB of files per app


def mint_app_key() -> str:
    """Mint a fresh publishable app key. Minted once at provision and never
    rotated (disable is the kill-switch, not a key rotation)."""
    return f"{_APP_KEY_PREFIX}{secrets.token_urlsafe(24)}"


class AppRegistry(UUIDv7PrimaryKeyMixin, OwnedByUserMixin, TimestampMixin, Base):
    __tablename__ = "app_registry"

    __table_args__ = (
        # ONE app per project (KD-4): a project IS one tool = one codebase, so the app
        # is now project-scoped, not conversation-scoped. This replaces the old
        # `(user_id, conversation_id)` uniqueness — provision reuses the project's
        # single app (the continuity case) rather than minting one per builder session.
        sa.UniqueConstraint("project_id", name="uq_app_registry_project"),
    )

    # The parent project (R2/R22, KD-4). Every app belongs to exactly one project; the
    # cascade is a row-integrity backstop only — a raw delete that bypasses the U6
    # blob-aware cascade orphans object-store blobs (KD-3a). `user_id` stays the
    # isolation predicate (ADR-0004); `project_id` is organizational, not tenancy.
    project_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid,
        sa.ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # The publishable scoping key. Unique + indexed: the X-App-Key chain resolves an
    # app by a single point-read on this column (`getByKey`).
    app_key: Mapped[str] = mapped_column(sa.String(64), unique=True, index=True, nullable=False)

    # Head pointer to the LAST builder session that touched this app (KD-4). Repurposed
    # from the old 1:1 `id == conversation_id` app↔conversation identity: the app is now
    # project-scoped, and many sessions build against it over its life, so this tracks
    # the most recent builder session. Still a plain indexed UUID with NO ForeignKey
    # (soft link); ownership/isolation is `user_id`, never this column.
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid, index=True, nullable=True)

    name: Mapped[str] = mapped_column(sa.String(MAX_APP_NAME), server_default="", nullable=False)

    # Admin-owned gate re-read LIVE by the X-App-Key chain each request (login can't
    # be "prompted away" by app code). Seeded false at provision; set at approval.
    login_required: Mapped[bool] = mapped_column(
        sa.Boolean, server_default=sa.text("false"), nullable=False
    )

    status: Mapped[AppStatus] = mapped_column(
        app_status_enum, server_default=AppStatus.DRAFT.value, nullable=False
    )

    # Per-app quota counters (Express registry doc). Reserved atomically before a
    # write and released on delete/rollback. Bytes are BigInteger (500 MB file cap
    # fits an Integer, but bytes semantics warrant the wider type).
    data_count: Mapped[int] = mapped_column(
        sa.Integer, server_default=sa.text("0"), nullable=False
    )
    data_bytes: Mapped[int] = mapped_column(
        sa.BigInteger, server_default=sa.text("0"), nullable=False
    )
    file_count: Mapped[int] = mapped_column(
        sa.Integer, server_default=sa.text("0"), nullable=False
    )
    file_bytes: Mapped[int] = mapped_column(
        sa.BigInteger, server_default=sa.text("0"), nullable=False
    )

    # The project's LIVE source of truth for code continuity (KD-9, R21). Same shape as
    # `conversations.code`'s `{current: {source, entry, ...}}`. A builder session SEEDS
    # its working code from this on open and WRITES the new code back on a successful
    # build, so a later session in the project continues from the last stage rather than
    # a blank slate. `conversations.code` is demoted to a per-session working buffer;
    # THIS is authoritative. NULL until the first build. Scope: code continuity only —
    # versioning/branching/history stay deferred to the file-system + sandbox phase.
    current_code: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    # The latest submitted build: {src, entry, compiled, at}. `compiled` is the
    # CLIENT-produced artifact (R19/AE4) — the server validates + stores it, never
    # compiles. Absent until the first submit.
    source_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # The last super-admin-approved artifact the runner serves: {compiled, src,
    # entry, at, by}. Absent until the first approval; a pending re-submit keeps
    # serving the prior approved snapshot until re-approval.
    approved_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    # Governance metadata (set by the admin surface).
    approved_by: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid, nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    rejection_note: Mapped[str | None] = mapped_column(sa.String(1000), nullable=True)
