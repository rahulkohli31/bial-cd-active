"""IS THIS APP LIVE? — the question, answered in one place.

"Live" was settled on the #158 call and it is a deployment fact, not a lifecycle one:

>   we have live = deployed / published — if the application is published and has url we
>   will show that status

That is worth stating plainly because the obvious reading is wrong. `AppStatus.APPROVED`
means an administrator said yes; it does not mean anything is serving. `PublishStatusChip`
already keeps `Approved` and `Live` apart for exactly this reason, and a list that collapsed
them would tell a citizen their app is live when it may never have been deployed at all.

THREE SURFACES ASK THIS, and they must not drift:

  * the marketplace catalog — an app is IN it because it is live (`marketplace/router.py`)
  * the projects list's status column (#158 §10)
  * the dashboard's "In production" count (#158 §1)

A count computed over a different predicate than the rows it describes is the failure the
marketplace docstring already argues against — page numbers you can click and find empty.
Across surfaces the same mistake is quieter and worse: the dashboard says three are live and
the list beneath it shows two.

`deployments` is APPEND-ONLY — one row per deploy ATTEMPT, not one per app — so this is a
COLLAPSE, never a flat filter, and it takes two of them:

  * `last_success`  — the newest attempt that SUCCEEDED and carries a URL. That is
    "published and has a url".
  * `last_unpublished` — the newest attempt bearing a takedown stamp, WHATEVER its status.
    `unpublish` stamps whichever row was newest at the time, so the question is not "does
    the newest row carry a stamp" but "did a takedown land after the thing we are about to
    call live".

An app is live when a `last_success` row exists, no takedown landed at or after it, AND the
registry does not record a withdrawal of its own. Because ids are UUIDv7 and
creation-ordered, "after" is a straight `<` comparison.

THE ORDERING INVARIANT THIS RESTS ON lives nowhere near here and breaks silently: `uuid`
ordering equals creation order only because every row is minted by CPython's in-process
monotonic `uuid7()` through `store._try_claim` (the only insert path), serialised per app by
`uq_deployments_one_in_flight`, on a control plane that is SINGLE-REPLICA BY DESIGN. A
second API replica reopens it — two hosts minting from independent clocks can interleave —
and an out-of-order id breaks the comparison in both directions: an app shown live at a dead
URL, or a live one shown as merely approved. Whoever scales the control plane revisits this,
not just the deployment topology.

Both partial indexes this relies on already exist (migration 0034), because the marketplace
needed the same two collapses.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa

from src.db.models.app_registry import AppRegistry, AppStatus
from src.db.models.deployment import Deployment, DeploymentStatus


def live_app_ids() -> sa.Select[Any]:
    """A SELECT of the `app_id`s that are live right now.

    Usable as a subquery on either side — `IN (...)`, a LEFT JOIN for a per-row flag, or a
    `COUNT(*)` for the dashboard — so every surface reads the same definition rather than
    re-deriving it.
    """
    last_success = (
        sa.select(Deployment.app_id, Deployment.id)
        # THE `status` PREDICATE MUST RENDER AS A LITERAL. A plain `== DeploymentStatus.X`
        # renders `status = $1`; asyncpg prepares server-side against a long-lived pool, so
        # from the 6th execution on a connection Postgres plans generically, and a generic
        # plan cannot prove `status = $1` implies `ix_deployments_success_collapse`'s
        # `status = 'succeeded'` predicate. It drops the index and Seq Scans a table that
        # grows with every deploy attempt the platform has ever made. Measured at 5.2k apps
        # / 52k rows: 13-15ms for executions 1-5, then 27-30ms (#147 round 3).
        #
        # `literal_execute` rather than `sa.text`: identical SQL, but the value stays the
        # ENUM, so renaming SUCCEEDED moves the predicate with it instead of leaving a
        # string that silently matches nothing.
        .where(
            Deployment.status
            == sa.bindparam("live_succeeded", DeploymentStatus.SUCCEEDED, literal_execute=True),
            Deployment.url.is_not(None),
        )
        .distinct(Deployment.app_id)
        .order_by(Deployment.app_id, Deployment.id.desc())
        .subquery()
    )
    last_unpublished = (
        sa.select(Deployment.app_id, Deployment.id)
        .where(Deployment.unpublished_at.is_not(None))
        .distinct(Deployment.app_id)
        .order_by(Deployment.app_id, Deployment.id.desc())
        .subquery()
    )
    return (
        sa.select(last_success.c.app_id)
        .select_from(last_success)
        # OUTER: an app that has never been unpublished has no row here and is still live.
        .outerjoin(last_unpublished, last_unpublished.c.app_id == last_success.c.app_id)
        # INNER, and load-bearing: A WITHDRAWAL IS NOT ALWAYS RECORDED ON THE DEPLOYMENT.
        # `unpublish` stamps the deployment row, but `disable` and `reject` write only the
        # REGISTRY — `disable` transitions the status and severs the app's database without
        # touching `unpublished_at` at all. A purely deployment-side predicate therefore
        # calls a kill-switched app live: it still has a newest-succeeded row with a URL and
        # no takedown stamp. The row would render the green Live badge (serving is checked
        # first, so `Switched off` is unreachable), and "In production" would count an app
        # an administrator had already killed.
        #
        # So liveness reads BOTH sides of the same question. These are the predicates the
        # marketplace applies for the same reason, `rejection_standing` included: `status`
        # is mutable lifecycle state, and a reject -> publish -> withdraw round trip lands
        # back at DRAFT while the standing rejection survives.
        .join(AppRegistry, AppRegistry.id == last_success.c.app_id)
        .where(
            sa.or_(
                last_unpublished.c.id.is_(None),
                last_unpublished.c.id < last_success.c.id,
            ),
            AppRegistry.status.notin_((AppStatus.DISABLED, AppStatus.REJECTED)),
            AppRegistry.rejection_standing.is_(False),
        )
    )
