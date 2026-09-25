"""What a scheduled worker pass did, and — far more important — THAT it happened.

A TABLE, NOT A REDIS KEY: every alarm a pass could raise is emitted *by the pass itself*, so a
crashlooping scheduler reads like a healthy quiet fleet — the only detector of a dead worker
is the ABSENCE of a pass record, which must outlive everything the worker depends on. A Redis
marker is evictable under any `allkeys-*` policy and would make that absence indistinguishable
from a real outage.

NOT USER-SCOPED, deliberately: the only table in the system that isn't. A pass is a property of
the deployment, not a citizen's, and it holds NO user data — a name, a timestamp, an outcome,
five integers. Every other model carries an owning `user_id` because it holds somebody's work.
"""

from __future__ import annotations

import datetime as dt
import enum

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base
from src.db.mixins import TimestampMixin, UUIDv7PrimaryKeyMixin


class PassOutcome(enum.StrEnum):
    """How a pass ended. A NATIVE PG enum, so a typo is a database error."""

    OK = "ok"
    #: The pass ran and refused to act — a store fault, an unreadable fleet, a disabled flag.
    #: Distinct from `failed`: nothing went wrong, the pass declined.
    DECLINED = "declined"
    #: The pass raised. Recorded rather than lost, because a pass that fails every time is
    #: otherwise indistinguishable from one that never runs — both leave no `ok` row.
    FAILED = "failed"


# The native PG enum type, shared by the model column and the Alembic migration — the same shape
# `app_status` and every other enum here uses.
#
# `values_callable` IS THE WHOLE POINT. Without it SQLAlchemy stores the member NAMES (`OK`,
# `DECLINED`, `FAILED`) while every other enum in this schema stores its values, so `outcome`
# would be the one column an operator has to remember is shouted. Worse, `PassOutcome` is a
# `StrEnum` — its members compare equal to their lowercase values everywhere in Python — so the
# mismatch is invisible until somebody writes `WHERE outcome = 'failed'` against the database and
# gets nothing back.
worker_pass_outcome_enum = sa.Enum(
    PassOutcome,
    name="worker_pass_outcome",
    values_callable=lambda enum: [member.value for member in enum],
    create_type=False,
)


class WorkerPass(UUIDv7PrimaryKeyMixin, TimestampMixin, Base):
    """One completed run of one scheduled task."""

    __tablename__ = "worker_passes"

    #: The task's own name (`sandbox_durable_copy`, `conversation_retention`). Not an enum: a new
    #: scheduled task should not need a migration to become observable.
    task_name: Mapped[str] = mapped_column(sa.String(128), index=True)
    outcome: Mapped[PassOutcome] = mapped_column(worker_pass_outcome_enum)
    finished_at: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), index=True)
    #: Whatever counts the task wants an operator to be able to read back — `copied`/`spared` for
    #: a durable-copy attempt, `condemned`/`removed` for retention. JSONB rather than columns so
    #: a second task with different counts needs no migration.
    #:
    #: `JSONB`, NOT `sa.JSON`, which is what this said and is not the same type. `sa.JSON` renders
    #: as `json` on Postgres: text, re-parsed on every read, un-indexable and with no containment
    #: operators. Every other JSON column in this schema is `JSONB` (`audit.detail`,
    #: `app_registry.declaration`), and an operator wanting "every pass that destroyed anything"
    #: needs the operators `json` does not have.
    counts: Mapped[dict[str, int]] = mapped_column(JSONB, default=dict)
    #: Free text for the declined/failed arms. NEVER a container name — this is read
    #: by an admin endpoint, and a sandbox name embeds 28 hex characters of its app's uuid.
    detail: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)
