"""IS THIS APP LIVE? — one exact definition, used everywhere.

WHY THIS EXISTS: "live" is a deployment fact, not a lifecycle one — `AppStatus.APPROVED` means
an admin said yes, not that anything is serving. Per the client: "we have live = deployed /
published — if the application is published and has url we will show that status." Every
surface reads this and must never drift — the marketplace catalog, the projects list's status
column, the dashboard's "Live applications" count and the deployment read's live address — or
the dashboard says three are live while the list beneath it shows two.

`deployments` is APPEND-ONLY (one row per ATTEMPT), so liveness is a COLLAPSE of two: the
newest SUCCEEDED attempt with a URL (`last_success`), and the newest attempt bearing a takedown
stamp, whatever its status (`last_unpublished` — `unpublish` stamps whichever row was newest at
takedown time). Live = a `last_success` exists, no takedown landed at or after it (ids are
UUIDv7/creation-ordered, so "after" is `<`), and the registry itself records no withdrawal.

THE ORDERING INVARIANT THIS RESTS ON: "after" as `<` holds only because every id is minted by
CPython's in-process monotonic `uuid7()` through `store._try_claim`, serialised per app by
`uq_deployments_one_in_flight`, on a SINGLE-REPLICA control plane. A second API replica breaks
the comparison in both directions (an app shown live at a dead URL, or a live one shown as
merely approved) — whoever scales the control plane must revisit this. Both partial indexes
this relies on already exist (migration 0034), built for the marketplace's identical need.
"""

from __future__ import annotations

import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import aliased

from src.db.models.app_registry import AppRegistry, AppStatus
from src.db.models.deployment import Deployment, DeploymentStatus


def live_app_ids(*, owner_user_id: uuid.UUID | None = None) -> sa.Select[Any]:
    """A SELECT of the `app_id`s that are live right now — usable as an `IN (...)`, a LEFT
    JOIN, or a `COUNT(*)`, so every surface reads the same definition.

    `owner_user_id`, WHEN GIVEN, NARROWS THE COLLAPSE ITSELF, not applied by the caller after:
    doing it outside used to scan every deployment row on the PLATFORM before scoping (measured
    at 25,245 apps / 112,045 deployments: 60-200ms on first sign-in for one citizen's page,
    growing forever)."""

    def _owned(query: sa.Select[Any]) -> sa.Select[Any]:
        if owner_user_id is None:
            return query
        return query.where(
            Deployment.app_id.in_(
                sa.select(AppRegistry.id).where(AppRegistry.user_id == owner_user_id)
            )
        )

    last_success = (
        _owned(sa.select(Deployment.app_id, Deployment.id))
        # THE `status` PREDICATE MUST RENDER AS A LITERAL. A plain `== DeploymentStatus.X`
        # renders `status = $1`; asyncpg prepares server-side against a long-lived pool, so
        # from the 6th execution on a connection Postgres plans generically, and a generic
        # plan cannot prove `status = $1` implies `ix_deployments_success_collapse`'s
        # `status = 'succeeded'` predicate. It drops the index and Seq Scans a table that
        # grows with every deploy attempt the platform has ever made. Measured at 5.2k apps
        # / 52k rows: 13-15ms for executions 1-5, then 27-30ms.
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
        _owned(sa.select(Deployment.app_id, Deployment.id))
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
        # first, so `Switched off` is unreachable), and "Live applications" would count an app
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


def last_success_deployment() -> type[Deployment]:
    """The newest-succeeded-with-a-url deployment row per app, ORM-aliased — the row
    PROJECTION a catalog query needs (a deployment's `url`, and via it the builder), not a
    second membership rule (membership is `live_app_ids()` above, joined separately by each
    caller). Shares the SAME `ix_deployments_success_collapse` partial index `live_app_ids`
    does — same predicate, same reason it must render as a literal (see there).

    Lives here, not in `marketplace/router.py` where it was first written (#145) — moved
    here (#191 slice 4) so the duplicate check can reuse the identical collapse instead of a
    second copy of it; `services/projects/duplicates.py` and `api/v1/marketplace/router.py`
    both import it from this one place now.
    """
    last_success = (
        sa.select(Deployment)
        .where(
            Deployment.status
            == sa.bindparam("succeeded", DeploymentStatus.SUCCEEDED, literal_execute=True),
            Deployment.url.is_not(None),
        )
        .distinct(Deployment.app_id)
        .order_by(Deployment.app_id, Deployment.id.desc())
        .subquery()
    )
    return aliased(Deployment, last_success, name="last_success")
