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
#
# `submit` (`apps/router.py`) does NOT read this table at all — its own guarded UPDATE
# uses `_SUBMIT_FROM` (`STATUS_TRANSITIONS[PENDING] | {PENDING}`), independent of the
# APPROVED/REJECTED entries below. Widening those two entries to also cover submit's
# sources was tried in V4 Part 2 and reverted: it bought submit nothing (confirmed by
# code review) while quietly weakening `reject()`'s atomic guard — `_transition`'s
# `WHERE status IN STATUS_TRANSITIONS[target]` is the real race-safety net `approve()`'s
# and `enable()`'s own comments describe, and `reject()` had no extra predicate of its
# own to compensate for the widening. Keep this table exactly as narrow as the admin
# `approve`/`reject`/`enable` endpoints need.
STATUS_TRANSITIONS: dict[AppStatus, frozenset[AppStatus]] = {
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


# The submit-time data-classification questionnaire (task-sheet order, verbatim
# categories) — a single source of truth for the Pydantic schema, the portal's question
# list, and the weighted total. `(key, label, weight)`; `key` is the JSONB field name
# AND the CamelModel attribute name (camelCased on the wire by `CamelModel`). Weights
# are the task sheet's soft-gate values — Credentials/Secrets and Health Data alone
# exceed the notes-required threshold (25), which is intended (either one on its own
# forces an explanation).
DATA_CLASSIFICATION_QUESTIONS: tuple[tuple[str, str, int], ...] = (
    ("credentials_secrets", "Credentials / Secrets", 40),
    ("health_data", "Health Data", 25),
    ("personal_information", "Personal Information (PII)", 20),
    ("financial_data", "Financial Data", 20),
    ("confidential_business_data", "Confidential Business Data", 15),
    ("public_data", "Public Data", 0),
)


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

    # The submit-time data-classification questionnaire (V4): set together with the
    # triplet above, on every submit/re-submit — never touched anywhere else. NULL
    # until the first submit (a legacy/pre-feature submission stays NULL forever, never
    # backfilled), so "no answers on file" and "answered, all No" are distinguishable
    # (the latter is a dict with six `false` values, never absent). One JSONB blob
    # rather than six columns — the only other submission-adjacent structured field on
    # this model (`current_code`) is JSONB too, and the six keys are validated once at
    # the `DataClassificationAnswers` Pydantic boundary, not re-derived here. If this
    # ever needs to become six typed columns instead, `DATA_CLASSIFICATION_QUESTIONS`
    # and `DataClassificationAnswers` are unaffected — only this column and the
    # handful of call sites that read/write the dict change.
    data_classification: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    # The pinned approved artifact: exactly the submission the admin reviewed.
    # Absent until the first approval; a pending re-submit keeps this pin (and the
    # runbook keeps deploying it) until re-approval — status governs liveness,
    # the pin governs WHICH artifact, and reject deliberately does not clear it.
    approved_submission_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid, nullable=True)
    approved_commit_sha: Mapped[str | None] = mapped_column(sa.String(40), nullable=True)

    # V4 Part 2: was this row's current approve/reject decision made by `submit`'s
    # score gate rather than a human admin? `approved_by IS NULL` already happens to be
    # true for every auto-approved row (no human to name), but overloading that as an
    # implicit "no review happened" signal is easy to miss — this column says it directly
    # and durably, at the row, independent of the audit log. `False` (server default) for
    # every row that predates this feature or was ever touched by the admin `approve`/
    # `reject` endpoints; `True` for every row `submit` has decided since.
    decided_automatically: Mapped[bool] = mapped_column(
        sa.Boolean, server_default=sa.text("false"), nullable=False
    )

    # The go-live marker (D7): `redeploy_needed` is derived as `approved_submission_id
    # != deployed_submission_id` (exact, clock-skew-free). Originally written ONLY by
    # a human via `mark-deployed`; as of V4 Part 3 it is ALSO written by
    # `services/deploy/provision.py::deploy_app` on a successful auto-deploy — same
    # fields, same meaning, a second writer. `mark-deployed` remains the manual
    # fallback (kill switch off, reconciler not wired to a trigger, or an app needs
    # hand-holding).
    deployed_submission_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid, nullable=True)
    deployed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)

    # Where the deployed app actually lives. Was PURE DATA (R5: "the admin pastes the
    # URL the manual go-live runbook produced") until V4 Part 3, which adds a second,
    # AUTOMATED writer: `deploy_app` sets it to the ACA-issued
    # `https://<name>.<region>.azurecontainerapps.io` address on a successful
    # auto-deploy. Either way NULL until something records one; a re-deploy/re-mark
    # with no URL LEAVES this alone, so the owner's link survives every redeploy.
    # Length matches the `MAX_DEPLOYED_URL` boundary cap, so a value that parses at
    # the schema always fits the column.
    deployed_url: Mapped[str | None] = mapped_column(sa.String(MAX_DEPLOYED_URL), nullable=True)

    # V4 Part 3: why the MOST RECENT auto-deploy attempt failed, if it did — operator/
    # owner visibility into a `redeploy_needed` app that isn't converging on its own.
    # NULL when there has never been a failed attempt, or the last attempt succeeded
    # (cleared on success, same as `rejection_note` is cleared on approval). Never set
    # by the manual `mark-deployed` path — that path has no "attempt" to fail, a human
    # either recorded a URL or didn't.
    last_deploy_error: Mapped[str | None] = mapped_column(sa.String(1000), nullable=True)

    # Governance metadata (set by the admin surface).
    approved_by: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid, nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    rejection_note: Mapped[str | None] = mapped_column(sa.String(1000), nullable=True)
