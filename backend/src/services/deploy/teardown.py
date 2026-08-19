"""Remove a published app's container — when its app is deleted, and on the admin kill-switch.

WITHOUT THIS, DELETING A PROJECT LEAVES A LIVE APPLICATION RUNNING. It would keep serving
whatever it had cached, keep billing, and — because the deployment row went with the app
under `ON DELETE CASCADE` — there would be nothing left anywhere that names it. The sandbox
reaper cannot find it either: that sweep reads the Redis sandbox registry, which a published
app is deliberately never written to. It would run until a human noticed.

So the app id must be captured BEFORE the delete commits, which the project cascade already
does for the per-app Blob containers. The published container name is a pure function of the
app id, so the same list serves both — no extra column, no second query.

Best-effort and never raising, matching `sweep_app_containers`: a container that outlives its
app is a bounded, logged leak an operator can sweep, while an exception here would abort a
delete that has ALREADY COMMITTED and leave the caller believing it failed.

TWO CALLERS, TWO POSTURES, and the never-raise contract serves both. The delete paths are
best-effort as above. `deploy/router.py`'s `unpublish` (#113) is the opposite — it is a
synchronous admin lever that must FAIL LOUD — so it reads the return count back and 503s on
zero rather than asking this function to raise. Keep it never-raising: the delete paths call
it after their own commit, where an exception has nothing left to undo. Note what the count
means for that reader: `delete_app` no-ops on an absent container and still increments, so a
non-zero count means "no error", not "something was deleted".
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

import structlog

from src.services.deploy.aca_publish import (
    DeployNotConfiguredError,
    PublishedAppRemover,
    get_published_apps,
)

_log = structlog.get_logger()

SWEPT_EVENT = "published_app_swept"


async def sweep_published_apps(
    app_ids: Iterable[uuid.UUID], *, client: PublishedAppRemover | None = None
) -> int:
    """Delete the published container app for each id. Returns how many were removed.

    `client` is injectable so a test can assert on the delete without reaching Azure; the
    default resolves the process singleton, and a deployment with publishing switched off
    simply has nothing to sweep."""
    ids = list(app_ids)
    if not ids:
        return 0

    if client is None:
        try:
            client = get_published_apps()
        except DeployNotConfiguredError:
            # Publishing is off on this deployment, so nothing was ever published.
            return 0

    swept = 0
    for app_id in ids:
        try:
            await client.delete_app(app_id=app_id)
            swept += 1
        except Exception:
            # A live container that outlives its app is a leak; a raise here would abort a
            # delete that already committed. Log loudly and keep going.
            _log.warning("published_app_sweep_failed", app_id=str(app_id), exc_info=True)
    if swept:
        _log.info(SWEPT_EVENT, count=swept)
    return swept
