"""THE RECORD OF WHAT A DELETE COULD NOT DESTROY.

Two levers destroy a citizen's work — their own project delete and an administrator's app
hard-delete — and every post-commit arm of both is best-effort by construction: the rows are
already gone, so a failed drop must not 500 a delete that in fact succeeded. What that buys in
safety it owes in accountability, and this is where the debt is paid: one audit row naming
every artefact that outlived the delete.

SHARED RATHER THAN COPIED, and it is the second call site that earns the module.
The admin path used to discard `salt_the_earth`'s answer entirely, which meant the harsher of
the two levers — an administrator destroying somebody else's app, with no undo — was the one
that kept no record of a database left standing afterwards.
"""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from src.services.audit.log import append_audit

logger = structlog.get_logger()


async def record_what_survived(
    db: AsyncSession,
    *,
    actor_id: uuid.UUID,
    project_id: uuid.UUID,
    survivors: list[tuple[str, str]],
    app_id: uuid.UUID | None = None,
) -> None:
    """File ONE audit row naming everything a delete failed to destroy. Never raises.

    THE AUDIT LOG, NOT THE TOMBSTONE'S `remark`. `deleted_projects.remark` is the citizen's own
    words and nothing else — an operator note appended into it would need a delimiter
    convention and a parser, and would corrupt the one field an administrator reads to learn
    why somebody deleted something. The audit log already answers "what happened to this
    project", already survives every row it references (no FK), and is already written twice on
    both paths.

    AND THE CITIZEN IS NOT TOLD. The owner's rule: if the code, the files and the database are
    gone, the delete is done as far as they are concerned. The alternative was a banner saying
    an operator had been notified, which this platform cannot make true — there is no mail, no
    webhook and no metrics system in this deployment.

    THE PROJECT IS THE SUBJECT EVEN WHEN AN APP WAS THE LEVER. Every artefact that can survive
    either path belongs to the project — its database, its blobs, its per-app containers, its
    published container app, its registry repository — and the project outlives an app
    hard-delete, so its id is the handle an operator still has something to look up.

    …WHICH IS WHY `app_id` RIDES IN THE `detail` WHEN THERE IS ONE. This row's `resource_id` is
    the PROJECT, so the field is what ties it to the app an administrator deleted; the `db:*`
    levers in `admin/router.py` carry `appId` for the same reason. The citizen's own project
    delete passes `None`: a project may own several apps, so there is no single id to name, and
    that path's row is found by project.

    ITS OWN TRANSACTION, because the delete committed several sweeps ago. That is also why it
    swallows: a delete that genuinely succeeded must not answer 500 because the accountability
    row for a leaked blob could not be written. The leak is already on the log either way — the
    alarm each arm raised is the notice, this row is the record."""
    if not survivors:
        return
    try:
        await append_audit(
            db,
            actor_id=actor_id,
            action="project:teardown-incomplete",
            resource_type="project",
            resource_id=str(project_id),
            # IDENTIFIERS ONLY: a blob key, a container name, a database name, a repository —
            # never a DSN, a credential or any of the content that survived. `count` is here
            # so an operator can sort by severity without parsing the list.
            detail={
                "count": len(survivors),
                "survived": [
                    {"artefact": artefact, "id": identifier} for artefact, identifier in survivors
                ],
                **({"appId": str(app_id)} if app_id is not None else {}),
            },
        )
        await db.commit()
    except Exception:  # noqa: BLE001 — post-commit: never 500 a delete that succeeded
        # THE RECOVERY IS GUARDED TOO. Whatever broke `append_audit` or the commit is most
        # likely a connection, and a broken connection fails `rollback()` as readily as it
        # failed the insert — so an unguarded rollback here would raise out of a function whose
        # first docstring line promises it never does, and 500 a delete that already committed.
        # That is the exact failure this whole arm exists to prevent, one line further down.
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001 — nothing left to try; the leak is already on the log
            logger.warning("project_teardown_record_rollback_failed", project_id=str(project_id))
        logger.warning("project_teardown_record_failed", project_id=str(project_id), exc_info=True)
