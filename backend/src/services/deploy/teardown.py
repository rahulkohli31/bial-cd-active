"""Remove a published app's container — when its app is deleted, when its owner takes it out
of production, and on the admin kill-switch.

WHY THIS EXISTS: without it, deleting a project leaves a live application running — still
serving, still billing, and named nowhere once the deployment row goes with the app under
`ON DELETE CASCADE`. The sandbox reaper can't find it either: it reads the Redis sandbox
registry, which a published app is deliberately never written to. It would run until a human
noticed.

The app id is captured BEFORE the delete commits (the project cascade already does this for the
per-app Blob containers, and the published container's name is a pure function of the app id, so
the same list serves both — no extra column, no second query). Best-effort and never-raising,
like `sweep_app_containers`: a failure raises `TEARDOWN_ARTEFACT_SURVIVED_EVENT` rather than
aborting a delete that has ALREADY COMMITTED, which would leave the caller believing it failed.
A container that outlives its app is therefore a leak AN OPERATOR must sweep by hand: nothing
automatic comes for it, because the reaper cannot see a published app at all and no other
reconciler on the delete path runs on a timer outside production. The caller records what
survived.

TWO POSTURES, NOT ONE: the delete paths are best-effort as above; the two synchronous levers
in `deploy/router.py` — an owner's `takedown` and the admin `unpublish` — must FAIL LOUD, so
they read the survivors back and 503 rather than claim a removal nobody observed. The MECHANISM
is shared and the SEMANTICS are not: the kill-switch also has `disable` beside it, which severs
the app's database credential, while an owner's take-down keeps every artefact intact.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

import structlog

from src.core.alarms import TEARDOWN_ARTEFACT_SURVIVED_EVENT
from src.services.deploy.aca_publish import (
    DeployNotConfiguredError,
    PublishedAppRemover,
    get_published_apps,
)

_log = structlog.get_logger()

SWEPT_EVENT = "published_app_swept"


async def sweep_published_apps(
    app_ids: Iterable[uuid.UUID], *, client: PublishedAppRemover | None = None
) -> list[uuid.UUID]:
    """Delete the published container app for each id. Returns the ids that SURVIVED.

    IT ANSWERS WITH SURVIVORS, LIKE ITS SIBLINGS, and that is the whole of the difference from a
    bare count. Four teardown arms on the delete path hand back what they could not destroy; a
    bare count would make its caller re-derive whether publishing was configured at all (a fact
    this function already answers internally) and, on a short count, name EVERY id as a survivor.
    The audit row that records what outlived a delete is supposed to be attributable — naming an
    app that was in fact deleted is worse than naming none, because it sends an operator after
    something that is not there.

    An empty list therefore means "nothing survived", including on a deployment with publishing
    switched off, where nothing was ever published.

    `client` is injectable so a test can assert on the delete without reaching Azure; the
    default resolves the process singleton."""
    ids = list(app_ids)
    if not ids:
        return []

    if client is None:
        try:
            client = get_published_apps()
        except DeployNotConfiguredError:
            # Publishing is off on this deployment, so nothing was ever published.
            return []

    swept = 0
    survived: list[uuid.UUID] = []
    for app_id in ids:
        try:
            await client.delete_app(app_id=app_id)
            swept += 1
        except Exception:
            # A live container that outlives its app keeps serving and keeps BILLING, and
            # nothing automatic will come for it; a raise here would abort a delete that
            # already committed. Alarm loudly and keep going.
            _log.warning(
                TEARDOWN_ARTEFACT_SURVIVED_EVENT,
                artefact="published_app",
                artefact_id=str(app_id),
                reason="the container app could not be deleted",
                exc_info=True,
            )
            survived.append(app_id)
    if swept:
        _log.info(SWEPT_EVENT, count=swept)
    return survived
