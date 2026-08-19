"""The `app_registry` table — the app-lifecycle spine for generated apps
(R18, R4; ADR-0004, ADR-0006, ADR-0008).

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

`status` is a native PG enum (ADR-0008). The registry records artifact *references*,
never artifact bytes (APPROVAL D1): `source_submission_id` + `source_commit_sha` point
at the immutable per-submission git bundle `submit` copies into Blob, and
`approved_submission_id` pins the exact submission an admin reviewed. The blob key is
derivable from `(app_id, submission_id)` via `submission_key`, so it is not stored.
`deployed_url` is the one field a human types: the address the manual go-live runbook
produced, recorded at mark-deployed so the owner sees a Live link (R5).

`approval_route` records which LINEAGE the current submission entered through (R17a):
`runbook` approvals authorise the manual go-live runbook and nothing else, while
`self_publish` approvals let the owner publish the pinned version themselves — and the
runbook levers (deploy-needed, mark-deployed) are refused for that lineage. `declaration`
carries what the publish flow attached at submit: both answer sets, the differences, and
the redacted explanation the administrator's review screen leads with (R15).
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


class ApprovalRoute(StrEnum):
    """How the CURRENT submission entered the approval queue (R17a, P5). Values are
    the native PG enum labels.

    An EXPLICIT column, not a derivation tweak (ASM8): `redeploy_needed` derives from
    two columns a self-published app never sets, so without this a self-published app
    would read "Deploy needed" forever and prompt an administrator to run a runbook
    that must not be run. The two lineages authorise DIFFERENT things:

    * `runbook` — the retired citizen submit route, plus every pre-feature row the
      0030 backfill marked. Approval authorises the manual go-live runbook only; it
      never satisfies the publish gate's self-publish rule (P5). This lineage gets no
      new entrants — a runbook-lineage PENDING item cannot even be approved anymore
      (the citizen must re-submit through the publish flow).
    * `self_publish` — the publish flow routed the app here with its declaration.
      Approval pins the version the citizen may publish THEMSELVES; the runbook
      levers (deploy-needed, mark-deployed) are suppressed and refused.
    """

    RUNBOOK = "runbook"
    SELF_PUBLISH = "self_publish"


# Same convention as `app_status_enum` above: the migration (0030) owns CREATE/DROP
# TYPE explicitly, so the column must not try to create the type itself.
approval_route_enum = sa.Enum(
    ApprovalRoute,
    name="approval_route",
    values_callable=lambda enum: [member.value for member in enum],
    create_type=False,
)


# The lifecycle state machine (Express `ALLOWED_FROM` heritage): target status →
# the set of source statuses a transition INTO it is allowed from. `draft` is minted
# by provision AND is the withdrawal target (U8: P6) — an owner pulling their own
# pending submission back OUT of the queue moves pending→draft, clearing the
# submission pin, the declaration and the lineage as it goes. No other status
# reaches draft: approve/reject/disable all move forward, never back to unsubmitted.
# A transition is applied as an atomic `UPDATE ... WHERE status = ANY(allowed)`;
# zero rows updated is a rejected (illegal) transition (→ 409), never a silent no-op.
STATUS_TRANSITIONS: dict[AppStatus, frozenset[AppStatus]] = {
    AppStatus.DRAFT: frozenset({AppStatus.PENDING}),
    AppStatus.PENDING: frozenset({AppStatus.DRAFT, AppStatus.REJECTED, AppStatus.APPROVED}),
    AppStatus.APPROVED: frozenset({AppStatus.PENDING, AppStatus.DISABLED}),
    AppStatus.REJECTED: frozenset({AppStatus.PENDING}),
    AppStatus.DISABLED: frozenset({AppStatus.APPROVED}),
}

# Publishable app-key shape (Express `bial_${randomBytes(24).base64url}`): the
# `bial_` prefix + 32 url-safe chars. token_urlsafe(24) yields the identical shape
# (base64url of 24 bytes, no padding). NEVER a raw UUID (ADR-0006).
_APP_KEY_PREFIX = "bial_"

# The recorded deployed-app URL cap (R5). 2083 is the historical IE address-bar
# ceiling that pydantic's own `HttpUrl` adopts as `max_length` — reusing the number
# keeps the schema boundary and the column exactly the same width, so a URL that
# parses can never overflow the column.
MAX_DEPLOYED_URL = 2083


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
    # No `index=True`: `uq_app_registry_project`'s unique index covers lookups.
    project_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid,
        sa.ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )

    # The publishable scoping key, surfaced by `GET /apps/{id}/status` and consumed by
    # the portal's approval view. Unique + indexed. (The per-request app-key chain that
    # once point-read this column died with the shared data plane; the key itself and
    # its uniqueness guarantee did not.)
    app_key: Mapped[str] = mapped_column(sa.String(64), unique=True, index=True, nullable=False)

    # Head pointer to the LAST builder session that touched this app (KD-4). Repurposed
    # from the old 1:1 `id == conversation_id` app↔conversation identity: the app is now
    # project-scoped, and many sessions build against it over its life, so this tracks
    # the most recent builder session. Still a plain indexed UUID with NO ForeignKey
    # (soft link); ownership/isolation is `user_id`, never this column.
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid, index=True, nullable=True)

    # Admin-owned gate on the deployed app (login can't be "prompted away" by app
    # code). Seeded false at provision; set at approval.
    login_required: Mapped[bool] = mapped_column(
        sa.Boolean, server_default=sa.text("false"), nullable=False
    )

    status: Mapped[AppStatus] = mapped_column(
        app_status_enum, server_default=AppStatus.DRAFT.value, nullable=False
    )

    # The project's LIVE source of truth for code continuity (KD-9, R21). Same shape as
    # `conversations.code`'s `{current: {source, entry, ...}}`. A builder session SEEDS
    # its working code from this on open and WRITES the new code back on a successful
    # build, so a later session in the project continues from the last stage rather than
    # a blank slate. `conversations.code` is demoted to a per-session working buffer;
    # THIS is authoritative. NULL until the first build. Scope: code continuity only —
    # versioning/branching/history stay deferred to the file-system + sandbox phase.
    current_code: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    # The submission under review (APPROVAL R1/R4): `submit` copies the app's
    # mutable snapshot bundle to the immutable `submission_key(app_id,
    # source_submission_id)` blob and records the ref + the bundle's HEAD commit
    # SHA here. NULL until the first submit; every re-submit mints a FRESH
    # submission id (ids are never reused, R2). `submitted_at` is the admin
    # queue's order axis (R16). Approve is guarded on `source_submission_id`
    # (D5), so a re-submit between review and approval updates zero rows → 409.
    source_submission_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid, nullable=True)
    source_commit_sha: Mapped[str | None] = mapped_column(sa.String(40), nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )

    # The pinned approved artifact: exactly the submission the admin reviewed.
    # Absent until the first approval; a pending re-submit keeps this pin (and the
    # runbook keeps deploying it) until re-approval — status governs liveness,
    # the pin governs WHICH artifact, and reject deliberately does not clear it.
    approved_submission_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid, nullable=True)
    approved_commit_sha: Mapped[str | None] = mapped_column(sa.String(40), nullable=True)

    # The manual-runbook marker (D7): a marker, NOT a status — `mark-deployed`
    # records that a human ran the go-live runbook for this exact submission.
    # `redeploy_needed` is derived as `approved_submission_id !=
    # deployed_submission_id` (exact, clock-skew-free).
    deployed_submission_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid, nullable=True)
    deployed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)

    # Where the deployed app actually lives — DATA, not automation (R5): the admin
    # pastes the URL the manual go-live runbook produced, and the owner gets a Live
    # link. NULL until an admin records one; mark-deployed with no URL LEAVES this
    # alone (a re-deploy of the same app keeps the same address), so the owner's
    # link survives every redeploy. Length matches the `MAX_DEPLOYED_URL` boundary
    # cap, so a value that parses at the schema always fits the column.
    deployed_url: Mapped[str | None] = mapped_column(sa.String(MAX_DEPLOYED_URL), nullable=True)

    # Governance metadata (set by the admin surface).
    approved_by: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid, nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    rejection_note: Mapped[str | None] = mapped_column(sa.String(1000), nullable=True)

    # The submission's lineage (R17a, P5 — see `ApprovalRoute`). NULLABLE, and NULL is a
    # real state, not sloppiness: a never-submitted draft has no lineage, and a row the
    # interim submit path wrote between 0030 and the publish-flow submit service (U8)
    # carries NULL and keeps today's behaviour everywhere — the projection treats only
    # an explicit `self_publish` as suppressing the runbook levers, and approve refuses
    # only an explicit `runbook`. The 0030 backfill marked every pre-feature row with an
    # approval AND every then-outstanding pending row as `runbook`, which is what makes
    # "approvals granted before this shipped do not authorise self-publishing" true.
    approval_route: Mapped[ApprovalRoute | None] = mapped_column(
        approval_route_enum, nullable=True
    )

    # What the publish flow attached at submit (R15): both answer sets (the citizen's and
    # the review's), the per-question differences, and the citizen's REDACTED explanation
    # — the payload the administrator's review screen leads with. JSONB for the same
    # reason `deployments.classification` is: the questionnaire is expected to be
    # reworded and reweighted, and a typed shape would make that a migration every time.
    # Written by the publish-flow submit (U8), cleared by withdraw; NULL for every
    # runbook-lineage and pre-feature row (the review screen says so rather than
    # rendering blanks). Never contains evidence locations — internal evidence stays
    # internal (OD-B).
    declaration: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
