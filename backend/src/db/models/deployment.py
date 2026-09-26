"""The `deployments` table.

WHY THIS EXISTS

One append-only row per one-click deploy attempt. A citizen presses Deploy and the app
goes live with no admin approval, so this is the ONLY durable record of what is running:
`head_sha` says WHICH commit went live (`app_registry`'s commit/submission columns belong
to the review lifecycle and stay unset while a self-deployed app is still `draft`),
and `image_digest` says WHICH image is running — the reconciler's authorization to act
(it may only promote a row whose digest matches what ARM reports as live, and must never
delete a container app it cannot prove it created) and the rollback source (redeploying
the previous digest is one ARM call against an image that already exists); without it a
failed deploy that supersedes a working revision is unrecoverable.

It is a TABLE, not columns on `app_registry`, because a failed attempt must not overwrite
the record of the version still serving traffic — one row per attempt keeps "what is
live" and "what we last tried" separately answerable. The partial unique index
`uq_deployments_one_in_flight` is the concurrency guard: claim with `INSERT ... ON
CONFLICT DO NOTHING RETURNING`, where zero rows back means a deploy is already in flight.
It lives in Postgres, not in-process, because the control plane restarts mid-pipeline and
an in-process lock would go blind across exactly that restart.

`heartbeat_at` is renewed by the running pipeline; staleness is measured from IT, never
`created_at`, since an image build legitimately runs for minutes. No `project_id` column:
`app_registry` already enforces one app per project, so the route resolves that mapping
before claiming, rather than storing a second copy that can drift.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base
from src.db.mixins import OwnedByUserMixin, TimestampMixin, UUIDv7PrimaryKeyMixin


class DeploymentStatus(StrEnum):
    """Three states, deliberately. The step-by-step phase (`packing`, `building`,
    `starting`, …) is display and lives in `step` as a plain string — adding a phase must
    never need a migration, while adding a STATUS would change what the partial index
    covers and is therefore a real schema decision. `unpublished_at` (below) is the same
    reasoning applied to "is it live right now" — a second axis, not a fourth
    status."""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


# The native PG enum type, shared by the model column and the Alembic migration.
# `create_type=False`: the migration owns CREATE/DROP TYPE explicitly (so a downgrade
# drops it) — the column must not try to create the type itself. Mirrors
# `app_registry.app_status_enum`.
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

# 2083, the address-bar ceiling pydantic's own `HttpUrl` adopts as `max_length`.
MAX_URL = 2083


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
        # THE MARKETPLACE'S TWO COLLAPSES (migration 0034). Partial indexes matching
        # `_live_catalog`'s predicates exactly, so each collapse is an index scan rather
        # than a Seq Scan of this table.
        #
        # The reason they are worth having on a 10-200 app catalog: this table is
        # APPEND-ONLY with no reaper, so what the collapses scan is TOTAL HISTORICAL DEPLOY
        # ATTEMPTS across the platform's life, not the number of live apps. Measured on
        # PG18 at 51k rows: ~100-180ms of DB time per request without these, ~35ms with
        # them. Declared here, not only in the migration, so `--autogenerate`
        # does not emit a `drop_index` for them.
        #
        # THE SUCCESS INDEX HAS A LOAD-BEARING REQUIREMENT ON ITS QUERY: the `status`
        # predicate in `marketplace/router.py` must render as a LITERAL. As a bound
        # parameter the planner cannot prove `status = $1` implies this index's
        # `status = 'succeeded'`, so from the 6th execution on a pooled connection —
        # once Postgres switches to a generic plan — the index goes unused and the table
        # pays its write and storage cost for nothing. That is the fact most likely to be
        # silently undone by a later refactor, and no functional test can see it: the
        # answer stays correct and only the plan degrades. The compiled SQL is pinned by
        # `test_the_success_collapse_predicate_renders_a_literal`.
        #
        # The unpublished index below is immune — its predicate carries no parameter.
        sa.Index(
            "ix_deployments_success_collapse",
            "app_id",
            sa.text("id DESC"),
            postgresql_where=sa.text("status = 'succeeded' AND url IS NOT NULL"),
        ),
        sa.Index(
            "ix_deployments_unpublished_collapse",
            "app_id",
            sa.text("id DESC"),
            postgresql_where=sa.text("unpublished_at IS NOT NULL"),
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

    # Where the app answers. Platform-written, never human-typed.
    url: Mapped[str | None] = mapped_column(sa.String(MAX_URL), nullable=True)

    failure_code: Mapped[str | None] = mapped_column(sa.String(MAX_FAILURE_CODE), nullable=True)
    # Redacted and length-capped by the writer before it lands here: a build log is
    # attacker-influenced text from a workspace the citizen's AI drove.
    failure_detail: Mapped[str | None] = mapped_column(sa.Text, nullable=True)

    # Renewed by the running pipeline; the reconciler's staleness clock. NOT NULL with a
    # server default so a row is never ambiguously "never beat" vs "beat long ago".
    heartbeat_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    # Set exactly once, in the same UPDATE that writes a terminal status.
    finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)

    # WHETHER THE APP IS CURRENTLY LIVE — a second, independent axis from `status`.
    # `status` answers "how did this attempt end"; this answers "is it serving traffic right
    # now". Deliberately not a fourth `DeploymentStatus` — that would change what
    # `uq_deployments_one_in_flight`'s partial index covers, a real schema decision the
    # admin kill-switch does not need to make.
    #
    # NOT SUCCEEDED-ONLY, and the tempting shorthand "a failed attempt was never live to
    # begin with" is simply false. The pipeline creates the container app at step 5 and only
    # THEN awaits the revision, so a row that settles FAILED at step 6 can name a container
    # that exists, is externally addressable, holds the app's database URL and Blob SAS, and
    # bills. The kill-switch therefore resolves the row to stamp through
    # `store.latest_for_app` — the newest row, never the newest SUCCEEDED one — and a stamp on
    # a FAILED row means exactly what it says: THIS is the attempt whose container was torn
    # down.
    #
    # READ IT OFF THE NEWEST ROW. An older row keeps whatever value it had when it was
    # current and is not maintained afterwards — "is this app live?" is a question about the
    # latest attempt, not an aggregate. NULL means "still published (or never attempted an
    # unpublish)"; a later successful deploy is a NEW row with this NULL, so republish needs
    # no code of its own — the newest-row read already does it.
    unpublished_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
