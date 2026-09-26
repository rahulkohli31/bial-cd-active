"""The `classification_reviews` table — ONE row per app, upserted and stamped with the commit
it read (`head_sha`), so a stale answer for an older commit is detectable rather than kept.

Opposite of `deployments` on purpose: reviews are overwritten wholesale when the version moves
because a stale claim is worse than none, while `deployments` stays append-only so a failed
attempt can never overwrite the version still serving.

`attempt` resets per version because review bypasses the daily token gate — service layer caps
it at three per version, not the store. `evidence` (the machine-checkable half of `verdicts`)
is NEVER projected to the citizen or administrator. `answers_complete` is independent of
`status`: COMPLETE can still answer fewer than six, which the publish gate treats as failed.
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


class ClassificationReviewStatus(StrEnum):
    """Three states, deliberately — mirroring `DeploymentStatus`'s discipline. The
    failure BUCKET (`failure_code`) is a plain string precisely so a new way to fail
    never needs a migration, while adding a STATUS would change what every claim guard
    and the publish gate's ladder read, and is therefore a real schema decision."""

    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"


# The native PG enum type, shared by the model column and the Alembic migration.
# `create_type=False`: the migration owns CREATE/DROP TYPE explicitly (so a downgrade
# drops it) — the column must not try to create the type itself. Mirrors
# `deployment.deployment_status_enum`.
classification_review_status_enum = sa.Enum(
    ClassificationReviewStatus,
    name="classification_review_status",
    values_callable=lambda enum: [member.value for member in enum],
    create_type=False,
)

# A stable, greppable failure bucket (`nothing_saved`, `review_failed`, …) — the same
# convention as `deployments.failure_code`.
MAX_FAILURE_CODE = 64


class ClassificationReview(UUIDv7PrimaryKeyMixin, OwnedByUserMixin, TimestampMixin, Base):
    __tablename__ = "classification_reviews"

    __table_args__ = (
        # ONE ROW PER APP — the whole design. The unique constraint is also the claim's
        # `ON CONFLICT` inference target, so the fresh-insert race is settled in
        # Postgres, not in-process (the control plane restarts mid-run; see the store).
        sa.UniqueConstraint("app_id", name="uq_classification_reviews_app"),
    )

    # The app this review describes. CASCADE so a deleted app can never leave a stale
    # verdict behind. No `index=True`: `uq_classification_reviews_app`'s unique index
    # covers lookups.
    app_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid,
        sa.ForeignKey("app_registry.id", ondelete="CASCADE"),
        nullable=False,
    )

    # THE VERSION STAMP — the commit the review read, parsed from the snapshot bundle
    # header (the same validated 40-hex parse the deploy pipeline trusts). NOT NULL: a
    # claim always knows which version it is reviewing, and a row without a stamp would
    # be exactly the un-datable answer this table exists to prevent.
    head_sha: Mapped[str] = mapped_column(sa.String(40), nullable=False)

    # The class-definition fingerprint the review read. A row without one predates fingerprints
    # and is never current.
    definitions_fingerprint: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)

    status: Mapped[ClassificationReviewStatus] = mapped_column(
        classification_review_status_enum,
        nullable=False,
        server_default=ClassificationReviewStatus.RUNNING.value,
    )

    # How many runs have been claimed for THIS `head_sha`. Increments per same-version
    # claim, resets to 1 when the version changes. The cap (three) is service-layer
    # policy; the store only counts.
    attempt: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default=sa.text("1"))

    # The six verdicts with their plain-language reasons, keyed by `CLASSIFICATION_KEYS`.
    # NULL while running and after a failure — a failed run stores its bucket, never a
    # partial answer set dressed up as one.
    verdicts: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    # The machine-checkable evidence behind each verdict. INTERNAL: stored for the
    # gate and the disagreement record, never shown to the citizen or the administrator.
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    # Whether the run answered all six questions. NULL until a terminal write; a
    # COMPLETE row carrying False is treated as failed by the publish gate's ladder.
    answers_complete: Mapped[bool | None] = mapped_column(sa.Boolean, nullable=True)

    # The failure bucket (one of five defined states) and its redacted, length-capped
    # detail. The detail is model- and workspace-influenced text; the writer redacts it
    # before it lands here, same as `deployments.failure_detail`.
    failure_code: Mapped[str | None] = mapped_column(sa.String(MAX_FAILURE_CODE), nullable=True)
    failure_detail: Mapped[str | None] = mapped_column(sa.Text, nullable=True)

    # Timings for the CURRENT attempt. `started_at` is reset by every claim — the
    # review's wall-clock ceiling is measured from it, so a reload never extends a run
    # and a restart leaves a row that ages out rather than hangs. `finished_at` is set
    # exactly once, in the same UPDATE that writes a terminal status.
    started_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)

    # The four raw token classes the CURRENT attempt spent, exactly as the model
    # reported them — `input_tokens` is INCLUSIVE of the two cache classes under
    # pydantic-ai, and nothing here re-adds them (the documented double-count
    # regression). Kept raw so the single shared spend expression is the only fold.
    # Zero until the terminal write, and reset by every claim: the counts describe this
    # attempt, and the per-citizen ledger (`token_usage`) owns the accumulation.
    input_tokens: Mapped[int] = mapped_column(
        sa.BigInteger, server_default=sa.text("0"), nullable=False
    )
    output_tokens: Mapped[int] = mapped_column(
        sa.BigInteger, server_default=sa.text("0"), nullable=False
    )
    cache_read_tokens: Mapped[int] = mapped_column(
        sa.BigInteger, server_default=sa.text("0"), nullable=False
    )
    cache_write_tokens: Mapped[int] = mapped_column(
        sa.BigInteger, server_default=sa.text("0"), nullable=False
    )
