"""Row operations on `deployments` — the claim, the heartbeat, and the terminal write.

The claim is the interesting one. It is an `INSERT ... ON CONFLICT DO NOTHING RETURNING`
against a PARTIAL unique index (`uq_deployments_one_in_flight`), so exactly one caller gets
a row back and everyone else learns "already deploying" without a race and without a lock.
By INFERENCE, not by constraint name: a partial index cannot be an `ON CONSTRAINT` target,
so `index_elements` + `index_where` is required and the predicate must match the index's
exactly or Postgres refuses to infer it.

THE STALE-CLAIM SELF-HEAL EXISTS BECAUSE THE CONTROL PLANE RESTARTS. A pipeline runs for
minutes and every platform deploy kills it mid-flight; without recovery the `running` row it
left behind would wedge that app forever, and the citizen's only signal would be a Deploy
button that 409s until someone notices. So a claim that loses to a row nobody is beating for
takes it over. Same shape as `appdb/provision.py`'s stale-claim reprovision arm.

Staleness is measured from `heartbeat_at`, NEVER from `created_at`. An image build
legitimately runs for minutes, so a start-time threshold either kills live deploys or lets a
crashed one hold the slot for as long as the longest legitimate build.

Every function takes an `AsyncSession`, and every PIPELINE writer commits its own work: the
pipeline outlives its request, so it opens short sessions of its own rather than borrowing
one it does not own. `unpublish` is the one deliberate exception — it is called mid-request
from a route that sequences several commits around it, so it leaves the boundary to its
caller. See its own docstring.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import sqlalchemy as sa
import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.deployment import Deployment, DeploymentStatus

_log = structlog.get_logger()

# How long a `running` row may go unbeaten before another claim may take it over.
#
# Must exceed the longest legitimate pipeline, or a slow-but-healthy deploy gets stolen from
# underneath itself and two pipelines write the same app. The budget it has to clear is
# roughly: snapshot extraction + context packing + the image build ceiling + provisioning +
# the readiness wait. 30 minutes leaves generous headroom over that sum while still bounding
# how long a crashed deploy can hold the slot.
DEPLOY_STALE_AFTER_S: Final = 1800

# The heartbeat cadence the pipeline renews at. Comfortably inside the staleness window so
# a few missed beats (a slow ARM call holding the loop) never look like a crash.
HEARTBEAT_CADENCE_S: Final = 20.0

# The failure recorded on a row that was taken over. Distinct from every pipeline-authored
# code so "the control plane died" is never mistaken for "the app failed to build".
INTERRUPTED: Final = "interrupted"

# The predicate must match `uq_deployments_one_in_flight`'s exactly, or Postgres cannot
# infer the index and the insert raises instead of conflicting.
_IN_FLIGHT_PREDICATE: Final = sa.text("status = 'running'")


def _rows_touched(result: object) -> int:
    """How many rows an UPDATE actually changed.

    `AsyncSession.execute` is declared to return the general `Result`, which has no
    `rowcount` — only the `CursorResult` a DML statement really yields does. Reading it
    through one narrow accessor keeps that cast in a single place instead of scattering
    ignores across every guarded update, and every guarded update in this module depends on
    the count: it is how a caller learns whether it was the one that settled the row."""
    return int(getattr(result, "rowcount", 0) or 0)


async def claim(
    db: AsyncSession,
    *,
    app_id: uuid.UUID,
    user_id: uuid.UUID,
    classification: dict[str, Any] | None = None,
    classification_score: int | None = None,
) -> uuid.UUID | None:
    """Claim the one in-flight deploy slot for `app_id`, or `None` if one is genuinely
    running. Commits.

    The data-classification declaration that cleared the gate is written by the SAME insert
    that claims the slot, so a row cannot exist without the answers that authorised it. A
    second UPDATE would open a window where a crash leaves a running deploy whose
    justification is missing — the one question a post-incident review always asks.

    Two attempts at most: the first plain claim, then — only if a stale row is actually
    taken over — one retry. Bounded on purpose; an unbounded retry loop against a row a
    live pipeline keeps beating would spin."""
    claimed = await _try_claim(
        db,
        app_id=app_id,
        user_id=user_id,
        classification=classification,
        classification_score=classification_score,
    )
    if claimed is not None:
        return claimed

    if not await _take_over_stale(db, app_id=app_id):
        return None

    return await _try_claim(
        db,
        app_id=app_id,
        user_id=user_id,
        classification=classification,
        classification_score=classification_score,
    )


async def _try_claim(
    db: AsyncSession,
    *,
    app_id: uuid.UUID,
    user_id: uuid.UUID,
    classification: dict[str, Any] | None,
    classification_score: int | None,
) -> uuid.UUID | None:
    stmt = (
        pg_insert(Deployment)
        .values(
            app_id=app_id,
            user_id=user_id,
            step="claimed",
            classification=classification,
            classification_score=classification_score,
        )
        .on_conflict_do_nothing(
            index_elements=[Deployment.app_id],
            index_where=_IN_FLIGHT_PREDICATE,
        )
        .returning(Deployment.id)
    )
    claimed: uuid.UUID | None = await db.scalar(stmt)
    await db.commit()
    return claimed


async def _take_over_stale(db: AsyncSession, *, app_id: uuid.UUID) -> bool:
    """Fail an in-flight row nobody has beaten for. True iff a row was actually taken over.

    The `heartbeat_at` predicate is what makes this safe: a live pipeline renews inside the
    window, so this can only ever reach a row whose owner is gone."""
    cutoff = datetime.now(UTC) - timedelta(seconds=DEPLOY_STALE_AFTER_S)
    result = await db.execute(
        sa.update(Deployment)
        .where(
            Deployment.app_id == app_id,
            Deployment.status == DeploymentStatus.RUNNING,
            Deployment.heartbeat_at < cutoff,
        )
        .values(
            status=DeploymentStatus.FAILED,
            failure_code=INTERRUPTED,
            failure_detail=(
                "The deploy did not finish — the platform restarted while it was running. "
                "Nothing was changed for your app; press Deploy again."
            ),
            finished_at=sa.func.now(),
        )
    )
    await db.commit()
    taken = bool(_rows_touched(result))
    if taken:
        _log.warning("deployment_stale_claim_taken_over", app_id=str(app_id))
    return taken


async def heartbeat(db: AsyncSession, deployment_id: uuid.UUID) -> None:
    """Renew the liveness stamp. Scoped to `running` so a terminal row is never resurrected
    by a heartbeat that raced the finish."""
    await db.execute(
        sa.update(Deployment)
        .where(Deployment.id == deployment_id, Deployment.status == DeploymentStatus.RUNNING)
        .values(heartbeat_at=sa.func.now())
    )
    await db.commit()


async def advance(db: AsyncSession, deployment_id: uuid.UUID, *, step: str, **fields: Any) -> None:
    """Record the phase, plus whatever the pipeline learned reaching it (a head SHA, a run
    id, a digest). Beats the heartbeat in the same statement — every phase change is proof
    of life, so a separate renew would be a redundant round trip.

    Guarded on `running`: a pipeline that keeps writing after being taken over must not
    overwrite the terminal row that replaced it."""
    await db.execute(
        sa.update(Deployment)
        .where(Deployment.id == deployment_id, Deployment.status == DeploymentStatus.RUNNING)
        .values(step=step, heartbeat_at=sa.func.now(), **fields)
    )
    await db.commit()


async def succeed(db: AsyncSession, deployment_id: uuid.UUID, *, url: str, **fields: Any) -> bool:
    """Write the terminal success. True iff this call was the one that settled the row."""
    return await _finish(
        db, deployment_id, status=DeploymentStatus.SUCCEEDED, step="live", url=url, **fields
    )


async def fail(
    db: AsyncSession,
    deployment_id: uuid.UUID,
    *,
    code: str,
    detail: str | None = None,
    **fields: Any,
) -> bool:
    """Write the terminal failure. True iff this call was the one that settled the row.

    NOT EVERY FAILED ROW IS A BROKEN DEPLOY, and the `code` is the only thing that tells
    them apart (U10/ASM20). The drift re-check's `routed_for_review` settles here too:
    that deploy did exactly what it should have — it stopped and put the version in front
    of an administrator — and a reader (or a dashboard) that treats `status = failed` as
    "something went wrong" will mis-report it. Adding a fourth `DeploymentStatus` instead
    would move what `uq_deployments_one_in_flight`'s partial index covers, which is a real
    schema decision this outcome does not need to make."""
    return await _finish(
        db,
        deployment_id,
        status=DeploymentStatus.FAILED,
        step="failed",
        failure_code=code,
        failure_detail=detail,
        **fields,
    )


async def _finish(
    db: AsyncSession, deployment_id: uuid.UUID, *, status: DeploymentStatus, **fields: Any
) -> bool:
    """The single terminal write, guarded on `running` so a row settles exactly once.

    The return value matters to the reconciler: a `False` means someone else already settled
    this row (it was taken over, or the reconciler promoted it), which is precisely when a
    late-arriving pipeline must stop writing rather than contradict what is now on record.

    IT ALSO CLEARS `unpublished_at`, and that is not housekeeping. `unpublish` resolves its
    target through `latest_for_app`, which has no status predicate, so an unpublish landing in
    the check-then-act window after `in_flight` returned None can stamp the NEW running row —
    the one whose pipeline is at that moment publishing the container. Without this the row
    settles SUCCEEDED wearing a takedown stamp: the portal reports "Taken down" over an app
    that is genuinely live, and every later unpublish takes the idempotent early return and
    never calls Azure again — the kill-switch jams on exactly the app it exists to kill.

    Clearing here is safe by construction and cannot erase a legitimate takedown: the UPDATE is
    guarded on `RUNNING`, and a row that is still running has not finished publishing yet, so a
    stamp on it can only have come from that race. A real takedown targets an already-settled
    row, which this statement never touches."""
    result = await db.execute(
        sa.update(Deployment)
        .where(Deployment.id == deployment_id, Deployment.status == DeploymentStatus.RUNNING)
        # `unpublished_at` first so an explicit caller-supplied value in `**fields` still wins.
        .values(unpublished_at=None, status=status, finished_at=sa.func.now(), **fields)
    )
    await db.commit()
    return bool(_rows_touched(result))


async def unpublish(db: AsyncSession, deployment_id: uuid.UUID, *, at: datetime) -> bool:
    """Mark a deployment as taken down. True iff this call was the one that set it — a
    repeat call (or one that lost a race with a concurrent unpublish of the same row)
    touches zero rows and returns False, which the caller reads as "already unpublished"
    rather than an error (#113's idempotency requirement). Takes the timestamp as a
    parameter, rather than `sa.func.now()`, so the caller's response can report the exact
    value written without a second read.

    NO STATUS GUARD, deliberately: the route resolves the row through `latest_for_app`, so
    a FAILED attempt whose container was created before the readiness check failed is a
    legitimate target. Which row is stampable is the caller's decision; this function's job
    is to make the stamp exactly once.

    THE `unpublished_at IS NULL` PREDICATE IS THE WHOLE CONCURRENCY STORY. The return value,
    not a prior read of the column, is what tells the caller whether it won — a read is
    stale the moment it lands, and under audit-first ordering (see the route) the caller's
    in-memory row is a pre-commit snapshot besides.

    DOES NOT COMMIT — unlike this module's pipeline writers (`_finish`, `heartbeat`, …),
    which each own their transaction outright. This one is called mid-request from
    `deploy/router.py`'s `unpublish` route, which sequences several commits of its own and
    branches on this return value before choosing where the next boundary falls; committing
    here would take that decision away from the only caller in a position to make it.

    NOT the same reason as `admin/router.py`'s `_transition`, despite the identical shape.
    That helper is commit-less so its UPDATE lands in the SAME transaction as the audit row
    that follows it. This one does not share a transaction with any audit write: the route
    commits its accountability row BEFORE calling Azure (so the trail survives a 504), which
    means the stamp necessarily lands in a later transaction than the audit. The atomicity
    that ordering buys is "the audit cannot be lost", not "the two writes are atomic" — and
    they are deliberately not."""
    result = await db.execute(
        sa.update(Deployment)
        .where(Deployment.id == deployment_id, Deployment.unpublished_at.is_(None))
        .values(unpublished_at=at)
    )
    return bool(_rows_touched(result))


# --- reads -------------------------------------------------------------------------


async def latest_for_app(db: AsyncSession, *, app_id: uuid.UUID) -> Deployment | None:
    """The most recent deploy attempt for an app. UUIDv7 primary keys are time-sortable, so
    `id DESC` is a creation-order sort with no second column and no index gymnastics."""
    row: Deployment | None = await db.scalar(
        sa.select(Deployment)
        .where(Deployment.app_id == app_id)
        .order_by(Deployment.id.desc())
        .limit(1)
    )
    return row


async def in_flight(db: AsyncSession, *, app_id: uuid.UUID) -> uuid.UUID | None:
    """The running deployment id for this app, if any. Used to block unpublish while a
    deploy is in progress (#113) — letting it through would race the in-flight pipeline's
    own `create_or_update`, which could silently re-publish the app moments after an admin
    tears it down."""
    running: uuid.UUID | None = await db.scalar(
        sa.select(Deployment.id)
        .where(Deployment.app_id == app_id, Deployment.status == DeploymentStatus.RUNNING)
        .limit(1)
    )
    return running


async def stalled(db: AsyncSession, *, older_than_s: float) -> list[Deployment]:
    """Every in-flight row whose owner has stopped beating — the reconciler's work list."""
    cutoff = datetime.now(UTC) - timedelta(seconds=older_than_s)
    rows = await db.scalars(
        sa.select(Deployment).where(
            Deployment.status == DeploymentStatus.RUNNING,
            Deployment.heartbeat_at < cutoff,
        )
    )
    return list(rows)
