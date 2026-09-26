"""The `app_registry` table — the app-lifecycle spine for generated apps.

One row per generated app, and the row's `id` IS the appId: a new app gets a UUIDv7 PK, and the
one-time Cosmos→Postgres backfill inserts a migrated app carrying its *existing* appId directly
as the PK, so already-deployed apps and previously-issued URLs keep resolving.

The registry records artifact *references*, never bytes and never a blob key — the key derives
from `(app_id, submission_id)` via `submission_key`."""

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


# The lifecycle state machine (Express `ALLOWED_FROM` heritage): the KEY is the target
# status and the value is the set of sources a transition INTO it may start from — read
# the other way round the map says the opposite of what it means. `DRAFT: {PENDING}` is
# the one backwards-looking entry, and it is withdrawal rather than a rollback of a
# decision: remove it and an owner can no longer pull their own pending submission out of
# the queue. A transition is applied as an atomic `UPDATE ... WHERE status = ANY(allowed)`;
# zero rows updated is a rejected (illegal) transition (→ 409), never a silent no-op.
#
# `DISABLED` accepts DRAFT and REJECTED as well as APPROVED. The kill switch used
# to reach approved apps ONLY, and the ORDINARY member of the marketplace catalog is a
# DRAFT — one-click deploy never writes a status (`deployment.py`: "a self-deployed app is
# still `draft`") — so the two categories most likely to need switching off could only be
# hard-deleted, which destroys the owner's work. PENDING is deliberately NOT in the set: an
# app sitting in the review queue is REJECTED, not switched off, and adding it would let an
# admin bypass the review decision with the ops lever.
#
# THE `DRAFT` ROW IS NOT THE PLACE TO UNDO THIS, and the temptation is real: a switched-off
# draft has no approval to be re-enabled back to, so the obvious repair is to let
# `disabled → draft`. Do not. `apps/router.py::withdraw` is CITIZEN-facing and reads this
# same row with only an ownership predicate, so widening it would let the OWNER of an app an
# administrator killed walk it straight back to draft — containment turned into a bypass.
STATUS_TRANSITIONS: dict[AppStatus, frozenset[AppStatus]] = {
    AppStatus.DRAFT: frozenset({AppStatus.PENDING}),
    AppStatus.PENDING: frozenset({AppStatus.DRAFT, AppStatus.REJECTED, AppStatus.APPROVED}),
    AppStatus.APPROVED: frozenset({AppStatus.PENDING, AppStatus.DISABLED}),
    AppStatus.REJECTED: frozenset({AppStatus.PENDING}),
    AppStatus.DISABLED: frozenset({AppStatus.APPROVED, AppStatus.DRAFT, AppStatus.REJECTED}),
}

# Publishable app-key shape (Express `bial_${randomBytes(24).base64url}`): the
# `bial_` prefix + 32 url-safe chars. token_urlsafe(24) yields the identical shape
# (base64url of 24 bytes, no padding). NEVER a raw UUID.
_APP_KEY_PREFIX = "bial_"


def mint_app_key() -> str:
    """Mint a fresh publishable app key. Minted once at provision and never
    rotated (disable is the kill-switch, not a key rotation)."""
    return f"{_APP_KEY_PREFIX}{secrets.token_urlsafe(24)}"


class AppRegistry(UUIDv7PrimaryKeyMixin, OwnedByUserMixin, TimestampMixin, Base):
    __tablename__ = "app_registry"

    __table_args__ = (
        # ONE app per project: a project IS one tool = one codebase, so the app is
        # project-scoped, not conversation-scoped. Provision reuses the project's single app
        # (the continuity case) rather than minting one per builder session.
        sa.UniqueConstraint("project_id", name="uq_app_registry_project"),
    )

    # The parent project. Every app belongs to exactly one project; this DB cascade is a
    # row-integrity backstop only — a raw delete that bypasses the blob-aware project cascade
    # orphans the app's object-store blobs, because a DB cascade never reaches the store.
    # No `index=True`: `uq_app_registry_project`'s unique index covers lookups.
    project_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid,
        sa.ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )

    # The publishable scoping key, surfaced by `GET /apps/{id}/status` and consumed by the
    # portal's approval view. A label, not a secret — the wall in front of a deployed app is
    # the IP-restricted network — so nothing may treat holding this key as authorisation.
    app_key: Mapped[str] = mapped_column(sa.String(64), unique=True, index=True, nullable=False)

    # Head pointer to the LAST builder session that touched this app. The app is
    # project-scoped and many sessions build against it over its life, so this tracks the
    # most recent one.
    # A plain indexed UUID with NO ForeignKey — a soft link, and deleting a conversation
    # does not clear it, so a reader must tolerate an id whose row is gone.
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid, index=True, nullable=True)

    # Admin-owned gate on the deployed app (login can't be "prompted away" by app
    # code). Seeded false at provision; set at approval.
    login_required: Mapped[bool] = mapped_column(
        sa.Boolean, server_default=sa.text("false"), nullable=False
    )

    status: Mapped[AppStatus] = mapped_column(
        app_status_enum, server_default=AppStatus.DRAFT.value, nullable=False
    )

    # WHAT THE APP WAS BEFORE THE KILL SWITCH. `disable` writes the pre-disable status here —
    # from the column itself, inside the same guarded UPDATE, so it can never record a status
    # the row had stopped holding — and `enable` restores from it.
    #
    # IT EXISTS BECAUSE `disable` NOW REACHES DRAFT AND REJECTED APPS. Enable used to resolve
    # to the literal APPROVED, which on a never-approved app invents an approval nobody gave;
    # with the widened source set above, a switched-off draft would either be promoted past
    # the review gate or stranded in DISABLED forever. Neither is acceptable, and the obvious
    # repair — widening the DRAFT row so `disabled → draft` becomes a legal transition — is
    # the citizen-facing bypass the STATUS_TRANSITIONS comment forbids. So the target is
    # remembered rather than derived.
    #
    # NULL IS A REAL STATE, TWICE OVER, AND NEITHER IS AN ERROR: an app that is not switched
    # off has nothing to remember (`enable` clears this on the way back out), and a row that
    # was already DISABLED before this column existed has nothing to have remembered —
    # migration 0038 backfills those to `approved`, and `enable` reads a NULL as `approved`
    # for the same reason, because that is what the code before it resolved them to.
    previous_status: Mapped[AppStatus | None] = mapped_column(app_status_enum, nullable=True)

    # The submission under review: `submit` copies the app's mutable snapshot bundle to
    # the immutable `submission_key(app_id, source_submission_id)` blob and records the
    # ref + the bundle's HEAD commit SHA here. NULL until the first submit; every
    # re-submit mints a FRESH submission id (ids are never reused). `submitted_at` is the
    # admin queue's order axis. Approve is guarded on `source_submission_id`, so a
    # re-submit between review and approval updates zero rows → 409.
    source_submission_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid, nullable=True)
    source_commit_sha: Mapped[str | None] = mapped_column(sa.String(40), nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )

    # The pinned approved artifact: exactly the submission the admin reviewed.
    # Absent until the first approval; a pending re-submit keeps this pin until
    # re-approval — status governs liveness, the pin governs WHICH artifact, and reject
    # deliberately does not clear it.
    approved_submission_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid, nullable=True)
    approved_commit_sha: Mapped[str | None] = mapped_column(sa.String(40), nullable=True)

    # Governance metadata (set by the admin surface).
    approved_by: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid, nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    rejection_note: Mapped[str | None] = mapped_column(sa.String(1000), nullable=True)

    # The durable half of a refusal. `status` answers "where is this app in its lifecycle" and a
    # citizen may legitimately move it (publish routes REJECTED->PENDING, withdraw moves
    # PENDING->DRAFT); this answers "has a human refused it", and ONLY an administrator
    # clears it — `reject` raises it, `approve` lowers it, nothing on the citizen's side
    # touches it. Ladder rule 5 reads THIS, never the status: reading a durable policy
    # fact off mutable lifecycle state is what let a reject->publish->withdraw round trip
    # launder a rejection and publish unattended.
    rejection_standing: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.false()
    )

    # What the publish flow attached at submit: both answer sets (the citizen's and the
    # review's), the per-question differences, and the citizen's REDACTED explanation — the
    # payload the administrator's review screen leads with. JSONB for the same reason
    # `deployments.classification` is: the questionnaire is expected to be reworded and
    # reweighted, and a typed shape would make that a migration every time. Written by the
    # publish-flow submit, cleared by withdraw; NULL on a row queued without one (the review
    # screen says so rather than rendering blanks). Never contains evidence locations —
    # internal evidence stays internal.
    declaration: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
