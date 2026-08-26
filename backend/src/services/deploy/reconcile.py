"""Resolve deployments whose pipeline stopped beating.

The control plane restarts on every platform deploy, and a pipeline runs for minutes — so a
deploy straddling a restart is not an edge case, it is the expected case during a rollout.
Its task dies with the process, its row stays `running`, and nothing else in the system
knows whether the container app actually came up.

ARM is the authority on what is live; the row is the authority on what we were TRYING to do.
This compares them, and the asymmetry between the two answers is the whole design:

  digest matches   -> the deploy SUCCEEDED and we died before writing it down. Promote it.
  digest differs   -> a PREVIOUS deploy is live; ours never landed. Fail the row and LEAVE
                      THE APP ALONE — it is the citizen's currently-serving version.
  confirmed absent -> nothing came up. Fail the row.
  ARM unreachable  -> leave the row exactly as it is. Never guess.

That last line is the one that matters most. `get_app_fqdn` returns `None` only for a
CONFIRMED absence and raises on a transient failure, precisely so a throttled request can
never read as "gone" — and a reconciler that collapsed the two would eventually mark a live
app failed, or worse, offer to delete it.

A reconciler may PROMOTE a row it did not write. It may never DELETE a container app it
cannot prove it created, and the digest is that proof.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.deployment import Deployment
from src.services.deploy import store
from src.services.deploy.aca_publish import PublishedAppReader
from src.services.sandbox.aca import AcaTransientError

_log = structlog.get_logger()

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

# A row is considered abandoned once it has gone this long without a heartbeat. Well past
# the pipeline's 20-second cadence, so a slow ARM call holding the loop never looks like a
# crash — and far below the stale-CLAIM window, so the reconciler resolves a dead deploy
# long before the citizen's next Deploy click would have to take it over.
STALE_AFTER_S: float = 120.0

RECONCILED_EVENT = "deployment_reconciled"


def _public_url(app_id: uuid.UUID) -> str:
    """Where a browser reaches this published app — the same composition the deploy pipeline's
    own success terminal uses, so the two writers of `deployments.url` cannot disagree.

    Lazy imports for the module's own cycle reason: `src.config` reaches back into the service
    packages, and `deploy.names` is imported the same way at every other reconcile-adjacent site.
    """
    from src.config import settings
    from src.services.deploy.names import published_app_name

    return settings.app_url(published_app_name(app_id))


async def reconcile_stalled_deployments(
    session_factory: SessionFactory, published_apps: PublishedAppReader
) -> int:
    """Settle every abandoned deployment row. Returns how many were resolved.

    Never raises: this runs from the lifespan and from a background sweep, where an
    exception would take the loop with it and silently return the deployment to no
    reconciliation at all."""
    async with session_factory() as db:
        rows = await store.stalled(db, older_than_s=STALE_AFTER_S)
    if not rows:
        return 0

    resolved = 0
    for row in rows:
        try:
            if await _resolve(session_factory, published_apps, row):
                resolved += 1
        except AcaTransientError:
            # ARM could not say. Leaving the row untouched is the correct answer — the next
            # sweep asks again, and until then nobody has claimed anything false.
            _log.info("deployment_reconcile_deferred", deployment_id=str(row.id))
        except Exception:
            _log.warning("deployment_reconcile_failed", deployment_id=str(row.id), exc_info=True)
    return resolved


async def _resolve(
    session_factory: SessionFactory, published_apps: PublishedAppReader, row: Deployment
) -> bool:
    app_id: uuid.UUID = row.app_id
    fqdn = await published_apps.get_app_fqdn(app_id=app_id)

    if fqdn is None:
        return await _fail(
            session_factory,
            row,
            detail="the app was not running when the platform came back",
        )

    live_image = await published_apps.get_app_image(app_id=app_id)
    if row.image_digest and live_image and live_image.endswith(row.image_digest):
        # We got as far as provisioning and died before writing it down. The app IS the one
        # this row built, so the honest record is success.
        async with session_factory() as db:
            # The PUBLIC address, exactly as the pipeline's own success terminal writes it. This
            # path runs in BOTH roles — the API's boot one-shot and the worker's cron — and it
            # writes the same column, so the two must not disagree about what a deployment's URL
            # means. Composed from the container name rather than the fqdn for the same reason:
            # BIAL's environment is internal, and its own domain does not resolve from a desk.
            settled = await store.succeed(db, row.id, url=_public_url(app_id))
        if settled:
            _log.info(RECONCILED_EVENT, deployment_id=str(row.id), outcome="promoted")
        return settled

    # Something else is serving this app — the previous deploy. Ours never landed, and the
    # running container is the citizen's working version. Record the failure; touch nothing.
    return await _fail(
        session_factory,
        row,
        detail="the deploy was interrupted; the previous version is still running",
    )


async def _fail(session_factory: SessionFactory, row: Deployment, *, detail: str) -> bool:
    async with session_factory() as db:
        settled = await store.fail(db, row.id, code=store.INTERRUPTED, detail=detail)
    if settled:
        _log.info(RECONCILED_EVENT, deployment_id=str(row.id), outcome="failed")
    return settled
