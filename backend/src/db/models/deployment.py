"""The `deployments` table — one append-only row per one-click deploy attempt.

A citizen presses Deploy and their app goes live; there is no admin approval on this path.
That makes this table the ONLY durable record of what is running, so it carries the two
facts nothing else in the schema can answer:

* `head_sha` — WHICH commit went live. `app_registry` has `approved_commit_sha` and
  `deployed_submission_id`, but both belong to the manual-runbook lifecycle and are
  guarded on `status == APPROVED`; a self-deployed app is still `draft`, so those columns
  can never describe it. Deploy keeps its own lineage rather than relaxing that guard.
* `image_digest` — WHICH image is running. This is not bookkeeping. It is (a) the
  reconciler's **authorization to act**: after a crash it may only promote a row whose
  digest matches what ARM reports as live, and it must never delete a container app it
  cannot prove it created; and (b) the rollback source — redeploying the previous digest
  is one ARM call against an image that already exists. Without this column a failed
  deploy that supersedes a working revision is unrecoverable.

WHY A TABLE AND NOT COLUMNS ON `app_registry`: the deploy is a long-running attempt with
its own failure states, and an attempt that fails must not overwrite the record of the
version still serving traffic. One row per attempt keeps "what is live" and "what we last
tried" separately answerable, which is exactly what the reconciler and rollback need.

THE PARTIAL UNIQUE INDEX IS THE CONCURRENCY GUARD (`uq_deployments_one_in_flight`). Claim
with `INSERT ... ON CONFLICT DO NOTHING RETURNING`; zero rows back means a deploy is
already in flight. It is enforced in Postgres rather than in-process because the pipeline
runs for minutes and the control plane restarts on every platform deploy — an in-process
dict goes blind across exactly the restart a deploy is most likely to straddle, and it
would add a fourth single-replica assumption to the three the deployment checklist already
carries. Same shape as `project_databases`' unique claim (ADR-0028), one predicate wider.

`heartbeat_at` is renewed by the running pipeline, and staleness is measured from IT, not
from `created_at`: an image build legitimately runs for minutes, so a start-time threshold
either kills live deploys or lets a crashed one wedge the app until someone notices.

No `project_id` column: `app_registry` already enforces one app per project
(`uq_app_registry_project`), so project → app is 1:1 and the route resolves it before
claiming. Storing it here would be a second copy that can drift.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base
from src.db.mixins import OwnedByUserMixin, TimestampMixin, UUIDv7PrimaryKeyMixin
from src.db.models.app_registry import MAX_DEPLOYED_URL


class DeploymentStatus(StrEnum):
    """Three states, deliberately. The step-by-step phase (`packing`, `building`,
    `starting`, …) is display and lives in `step` as a plain string — adding a phase must
    never need a migration, while adding a STATUS would change what the partial index
    covers and is therefore a real schema decision. `unpublished_at` (below) is the same
    reasoning applied to "is it live right now" (#113) — a second axis, not a fourth
    status."""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


# The native PG enum type, shared by the model column and the Alembic migration.
# `create_type=False`: the migration owns CREATE/DROP TYPE explicitly (so a downgrade
# drops it) — the column must not try to create the type itself. Mirrors
# `app_registry.app_status_enum` (ADR-0008).
deployment_status_enum = sa.Enum(
    DeploymentStatus,
    name="deployment_status",
    values_callable=lambda enum: [member.value for member in enum],
    create_type=False,
)

# ACA container-app names are capped at 32 characters, and `published_app_name` produces
# exactly 32 (`pub-` + 28 hex). Sized to the platform's ceiling, not to today's slug.
MAX_ACA_NAME = 32

# `{container_app_name}--{revision_suffix}` — 32 + 2 + an 11-char suffix today. 64 leaves
# room for a longer suffix without a migration.
MAX_REVISION_NAME = 64

# "sha256:" + 64 hex = 71. Rounded up so a future longer digest algorithm still fits.
MAX_IMAGE_DIGEST = 80

# The phase label the client renders. A short closed-ish vocabulary in practice
# (`packing`, `building`, `provisioning`, `starting`), but a plain String by design.
MAX_STEP = 32

# A stable, greppable failure identifier (`acr_build_failed`, `revision_unhealthy`, …).
MAX_FAILURE_CODE = 64


class Deployment(UUIDv7PrimaryKeyMixin, OwnedByUserMixin, TimestampMixin, Base):
    __tablename__ = "deployments"

    __table_args__ = (
        # THE concurrency guard: at most one non-terminal deploy per app. A partial
        # unique index (not a UniqueConstraint) because the uniqueness holds only while
        # `status = 'running'` — succeeded and failed rows accumulate freely, which is
        # what makes the table an append-only history rather than a single mutable slot.
        # Claimed via `on_conflict_do_nothing(index_elements=[app_id],
        # index_where=...)`: a partial index cannot be named as an ON CONSTRAINT target,
        # so the inference form is required.
        sa.Index(
            "uq_deployments_one_in_flight",
            "app_id",
            unique=True,
            postgresql_where=sa.text("status = 'running'"),
        ),
    )

    # The app this deploy publishes. CASCADE so a deleted app can never leave a row
    # pointing at nothing — but note the cleanup ordering that implies: the container
    # app's name must be read OUT of this row before the delete commits, or the running
    # container becomes an orphan no sweeper can find (see `projects/delete.py`).
    app_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid,
        sa.ForeignKey("app_registry.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    status: Mapped[DeploymentStatus] = mapped_column(
        deployment_status_enum,
        nullable=False,
        server_default=DeploymentStatus.RUNNING.value,
    )

    # The phase, for display only. Never branched on.
    step: Mapped[str] = mapped_column(
        sa.String(MAX_STEP), nullable=False, server_default="claimed"
    )

    # The commit that went live, parsed from the snapshot bundle header
    # (`storage/bundle.parse_bundle_head_sha`) — the same validated 40-hex parse submit
    # trusts. NULL until the snapshot has been extracted.
    head_sha: Mapped[str | None] = mapped_column(sa.String(40), nullable=True)

    # See the module docstring: this is authorization to act, not bookkeeping.
    image_digest: Mapped[str | None] = mapped_column(sa.String(MAX_IMAGE_DIGEST), nullable=True)

    # Persisted BEFORE the build is awaited, so a crash mid-build leaves the run
    # attributable instead of orphaning an ACR run nobody can trace back to an app.
    acr_run_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)

    # Stored, never re-derived — teardown and the orphan sweep must keep working against
    # rows minted under an older name derivation (the `project_databases.db_name`
    # argument, verbatim).
    container_app_name: Mapped[str | None] = mapped_column(sa.String(MAX_ACA_NAME), nullable=True)
    revision_name: Mapped[str | None] = mapped_column(sa.String(MAX_REVISION_NAME), nullable=True)

    # Where the app answers. Platform-written, never human-typed — the opposite of
    # `app_registry.deployed_url`, which is the manual runbook's field and is deliberately
    # left alone by this path.
    url: Mapped[str | None] = mapped_column(sa.String(MAX_DEPLOYED_URL), nullable=True)

    failure_code: Mapped[str | None] = mapped_column(sa.String(MAX_FAILURE_CODE), nullable=True)
    # Redacted and length-capped by the writer before it lands here: a build log is
    # attacker-influenced text from a workspace the citizen's AI drove.
    failure_detail: Mapped[str | None] = mapped_column(sa.Text, nullable=True)

    # WHAT THE CITIZEN DECLARED THIS APP HANDLES, and the score that let it through the
    # gate. Recorded per DEPLOY rather than per app: the agent edits the app between
    # deploys, so a declaration attached to `app_registry` would keep describing a version
    # that is no longer running. Answering "what was this build claimed to handle" is only
    # possible if the answer travels with the build.
    #
    # Nullable because rows minted before the questionnaire existed have no declaration and
    # must not be back-filled with a guess — `NULL` reads as "never asked", which is true,
    # while a synthesised all-False set would read as "declared to handle nothing", which
    # is a claim nobody made.
    #
    # JSONB rather than six columns for the same reason `app_registry.current_code` is
    # JSONB: the questionnaire is expected to be reworded and reweighted, and a shape that
    # needs a migration per question would make that a schema conversation every time. The
    # keys are pinned to `CLASSIFICATION_KEYS`, so this is a stable shape, not a free-form
    # bag.
    classification: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    # Stored alongside the answers rather than recomputed from them. The weights are policy
    # and policy changes: recomputing later would report what TODAY's table says about an
    # old declaration, silently rewriting the reason a past deploy was allowed. This column
    # is the score that actually authorised this deploy.
    classification_score: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)

    # Renewed by the running pipeline; the reconciler's staleness clock. NOT NULL with a
    # server default so a row is never ambiguously "never beat" vs "beat long ago".
    heartbeat_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    # Set exactly once, in the same UPDATE that writes a terminal status.
    finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)

    # WHETHER THE APP IS CURRENTLY LIVE — a second, independent axis from `status` (#113).
    # `status` answers "how did this attempt end"; this answers "is it serving traffic right
    # now", which only matters on a SUCCEEDED row (a failed attempt was never live to begin
    # with). Deliberately not a fourth `DeploymentStatus` — that would change what
    # `uq_deployments_one_in_flight`'s partial index covers, a real schema decision the
    # admin kill-switch does not need to make. NULL means "still published (or never
    # attempted an unpublish)"; a later successful deploy is a NEW row with this NULL, so
    # republish needs no code of its own — `store.last_successful` picking the newest
    # SUCCEEDED row already does it.
    unpublished_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
