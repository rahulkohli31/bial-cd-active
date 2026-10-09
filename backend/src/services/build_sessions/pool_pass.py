"""The pass that holds each pool of ready sandboxes, plain and connector, at its size, run every
minute by the worker, one at a time under an advisory lock.

It acts only on containers the pool's ledger holds, never on one a registry names: a claimed row
left past its deadline loses its container only when no registry record and no owed teardown names
it. Pool creates and deletes pass through the sandbox client, which bounds the pool work of each
process.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

import sqlalchemy as sa
import structlog

from src.config import settings
from src.core.alarms import SANDBOX_POOL_BELOW_SIZE_EVENT
from src.db import base as db_base
from src.db.models.pending_teardown import PendingTeardown
from src.db.models.sandbox_pool import SandboxPoolMember, SandboxPoolState
from src.db.models.sandbox_start import SandboxProjectType
from src.services.build_sessions.destroy import single_flight_lock
from src.services.build_sessions.inventory import registered_app_names
from src.services.redis import get_redis
from src.services.sandbox import pool
from src.services.sandbox.client import (
    CREATE_CEILING,
    FIRST_ANSWER_CEILING,
    AcaSandboxClient,
    FillOutcome,
    SandboxNotConfiguredError,
    get_sandbox,
)

_log = structlog.get_logger()

#: Longer than a pool create and its container's first answer, timed from when the fill holds the
#: pool's bound, or a start's own create and the registry write after it, can still be running. A
#: filling or claimed row older than this was left by a process that died, or is a fill still
#: queued for the bound, which makes nothing once its row is gone.
ROW_DEADLINE: Final = CREATE_CEILING + FIRST_ANSWER_CEILING + timedelta(minutes=1)

#: The advisory lock every pass takes.
POOL_LOCK_KEY: Final = 0x50_4F_4F_4C_01  # "POOL" + 01

POOL_PASS_EVENT: Final = "sandbox_pool_pass_completed"
POOL_PASS_LOCKED_OUT_EVENT: Final = "sandbox_pool_pass_locked_out"
POOL_PASS_DISABLED_EVENT: Final = "sandbox_pool_pass_disabled"


@dataclass(frozen=True)
class PoolPass:
    """What one pass found and did for one pool. `ready` is counted as that pool's run ends;
    `overdue` says it found a filling row past `ROW_DEADLINE`."""

    project_type: SandboxProjectType
    target: int
    ready: int
    filled: int
    retired: int
    deleted: int
    refused: bool
    overdue: bool


async def run_pool_pass() -> None:
    """One pass under the pool's lock with this process's sandbox client, or one line saying why
    there was none."""
    if settings.sandbox is None:
        _log.info(POOL_PASS_DISABLED_EVENT, reason="unconfigured")
        return
    sandbox = get_sandbox()
    if not isinstance(sandbox, AcaSandboxClient):
        raise SandboxNotConfiguredError("the pool of ready sandboxes needs the Azure client")
    await keep_the_pool_under_the_lock(sandbox)


async def keep_the_pool_under_the_lock(client: AcaSandboxClient) -> None:
    """One pass now, unless another pass holds the pool's lock — the second scheduler of a deploy,
    or the last tick's pass still filling — in which case this one says so and stands down."""
    async with single_flight_lock(POOL_LOCK_KEY) as took_the_lock:
        if not took_the_lock:
            _log.info(POOL_PASS_LOCKED_OUT_EVENT)
            return
        await keep_the_pool(client, at=datetime.now(UTC))


async def keep_the_pool(
    client: AcaSandboxClient, *, at: datetime
) -> dict[SandboxProjectType, PoolPass]:
    """One pass at `at`, an aware instant: the plain pool, then the connector pool, each over its
    own rows and logged as its own line. A start's own create and a held delete are plain rows."""
    return {
        project_type: await _keep_one_pool(client, project_type, at=at)
        for project_type in (SandboxProjectType.PLAIN, SandboxProjectType.CONNECTOR)
    }


async def _keep_one_pool(
    client: AcaSandboxClient, project_type: SandboxProjectType, *, at: datetime
) -> PoolPass:
    """One pool's run of a pass. It clears that pool's rows left past `ROW_DEADLINE` and retries
    deletes that failed; retires ready rows above its size for `at`, older images first, which
    swaps an image change in new before old; then fills until the configured image's rows reach
    the size. A refused create, or a filling row past its deadline, ends that pool's filling for
    this pass, and its alarm fires if it is left below its size."""
    config = client.config
    target = config.pool_size_at(at, project_type=project_type)
    members = [m for m in await pool.the_ledger() if m.project_type is project_type]
    overdue = [
        member
        for member in members
        if member.state in (SandboxPoolState.FILLING, SandboxPoolState.CLAIMED)
        and member.state_changed_at < at - ROW_DEADLINE
    ]
    stuck_claims = [m.name for m in overdue if m.state is SandboxPoolState.CLAIMED]
    in_use: set[str] = await _names_still_in_use(stuck_claims) if stuck_claims else set()
    deleted = 0
    for member in overdue:
        if member.name in in_use:
            # Its container reached the registry and its row outlived the write: the container is
            # somebody's workspace, or their work is still being saved out of it.
            await pool.forget(member.id)
        elif await pool.retire(member.id, was=member.state) and await _let_go(client, member):
            deleted += 1
    for member in members:
        if member.state is SandboxPoolState.RETIRING and await _let_go(client, member):
            deleted += 1

    ready = sorted(
        (member for member in members if member.state is SandboxPoolState.READY),
        key=lambda member: (member.image_ref == config.image_ref, member.state_changed_at),
    )
    retired = 0
    for member in ready[: max(len(ready) - target, 0)]:
        if not await pool.retire(member.id, was=SandboxPoolState.READY):
            continue
        retired += 1
        deleted += int(await _let_go(client, member))

    # A start's create left claimed is cleared above, and never holds back a fill.
    filling_overdue = any(member.state is SandboxPoolState.FILLING for member in overdue)
    filled = 0
    made: FillOutcome = "at_target"
    for _ in range(0 if filling_overdue else target):
        if (made := await client.fill_one(target, project_type=project_type)) != "filled":
            break
        filled += 1

    outcome = PoolPass(
        project_type=project_type,
        target=target,
        ready=await pool.ready_count(project_type=project_type),
        filled=filled,
        retired=retired,
        deleted=deleted,
        refused=made == "refused",
        overdue=filling_overdue,
    )
    _log.info(POOL_PASS_EVENT, **asdict(outcome))
    if outcome.ready < target and (outcome.refused or outcome.overdue):
        _log.warning(
            SANDBOX_POOL_BELOW_SIZE_EVENT,
            project_type=project_type,
            target=target,
            ready=outcome.ready,
            refused=outcome.refused,
            overdue=outcome.overdue,
        )
    return outcome


async def _names_still_in_use(names: Collection[str]) -> set[str]:
    """Which of `names` a registry record or an owed teardown names. The registry is read first:
    a workspace handed over is owed before its record goes, so a container moving from one to the
    other between the two reads is still seen."""
    held = await registered_app_names(get_redis()) & set(names)
    async with db_base.async_session_factory() as db:
        held.update(
            await db.scalars(
                sa.select(PendingTeardown.app_name).where(PendingTeardown.app_name.in_(names))
            )
        )
    return held


async def _let_go(client: AcaSandboxClient, member: SandboxPoolMember) -> bool:
    """Delete a retiring row's container, then the row once Azure confirms the container gone."""
    if not await client.delete_pool_container(member.name):
        return False
    await pool.forget(member.id)
    return True
