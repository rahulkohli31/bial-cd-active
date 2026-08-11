"""What a scheduled worker pass did, and — far more important — THAT it happened (U11, R20).

WHY THIS IS A TABLE AND NOT A REDIS KEY. The fleet-count alarm is emitted *by the pass itself*, so
a crashlooping scheduler emits nothing and reads exactly like a healthy quiet fleet: the origin
incident's epistemic failure, relocated one layer out. The only detector of a dead worker is
therefore the ABSENCE of a pass record, which means the record has to outlive everything the
worker depends on. Under `volatile-lru` — or any `allkeys-*` policy — a Redis marker is evictable,
so the staleness alarm would fire spuriously *and* its absence would be indistinguishable from a
real outage. Postgres also gives pass history for free, which report-only's three-consecutive-pass
exit condition needs anyway.

NOT USER-SCOPED, and deliberately so. This is the only table in the system that is not: a pass is
a property of the deployment, not of a citizen, and it holds no user data — a name, a timestamp,
an outcome and five integers. Every other model here carries an owning `user_id` because it holds
somebody's work; this one holds the platform's own pulse.
"""

from __future__ import annotations

import datetime as dt
import enum

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base
from src.db.mixins import TimestampMixin, UUIDv7PrimaryKeyMixin


class PassOutcome(enum.StrEnum):
    """How a pass ended. A NATIVE PG enum (ADR-0008), so a typo is a database error."""

    OK = "ok"
    #: The pass ran and refused to act — a store fault, an unreadable fleet, a disabled flag.
    #: Distinct from `failed`: nothing went wrong, the pass declined.
    DECLINED = "declined"
    #: The pass raised. Recorded rather than lost, because a pass that fails every time is
    #: otherwise indistinguishable from one that never runs — both leave no `ok` row.
    FAILED = "failed"


class WorkerPass(UUIDv7PrimaryKeyMixin, TimestampMixin, Base):
    """One completed run of one scheduled task."""

    __tablename__ = "worker_passes"

    #: The task's own name (`sandbox_reclamation`, `deploy_reconcile`). Not an enum: a new
    #: scheduled task should not need a migration to become observable.
    task_name: Mapped[str] = mapped_column(sa.String(128), index=True)
    outcome: Mapped[PassOutcome] = mapped_column(
        sa.Enum(PassOutcome, name="worker_pass_outcome", native_enum=True)
    )
    finished_at: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), index=True)
    #: Whatever counts the task wants an operator to be able to read back — scanned/spared/
    #: staged/destroyed/escalated for reclamation. JSONB rather than columns so a second task
    #: with different counts needs no migration.
    counts: Mapped[dict[str, int]] = mapped_column(sa.JSON, default=dict)
    #: Free text for the declined/failed arms. NEVER a container name (C10 §3.6) — this is read
    #: by an admin endpoint, and a sandbox name embeds 28 hex characters of its app's uuid.
    detail: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)
