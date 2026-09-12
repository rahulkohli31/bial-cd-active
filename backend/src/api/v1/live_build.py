"""The shared "is a build session live right now?" guard, used by every destructive owner-facing
route a concurrent build would race — copying a snapshot mid-build captures the wrong tree, and
deleting mid-build destroys uncommitted work. Asked in one place, by the submit service, the
deploy route, and `projects.delete_project`.

SCOPE. The lock is keyed on the USER, not the app — a bare `lock_is_held` answers "is this user
building ANYTHING?", the wrong question when one user has several projects. Pass `app_id` for the
narrow per-app answer; every current caller does. Omitting it is a decision to justify.

THREE ANSWERS, AND THE FIRST IS THE SURPRISING ONE. `RedisNotConfiguredError` means PROCEED:
with no Redis there is no build-session subsystem at all, so no lock can be held (dev and test
only — production requires Redis at the settings gate). `RedisError` means 503: the store exists
and failed to answer, so the check decided nothing, and any error or ambiguity denies. A lock
genuinely held means 409, in the caller's own words.

WHY THIS EXISTS — do not read a passing guard as "no container is serving this app". A
relaunched preview holds no lock by design (`manager.py::relaunch_preview` releases the
per-user lock on exit; the container's lifetime is owned by an explicit stay of execution on
the registry hash instead, `locks.py::grant_stay_of_execution`), so the container-still-serving
state is precisely the state where `lock_is_held` is False — this guard returns without
refusing, and it is the CALLER's job to deal with whatever is still running.

CLOSED ON THE DELETE PATH, AND ONLY THERE. `projects.delete_project` reaps the container
itself: post-commit it asks the registry whether it still names this project's app and, if
so, hands it to `reap_user` under the per-user start lock — best-effort, so a busy start
lock, an unconfigured sandbox, or a Redis blip still leave the container standing, and
nothing automatic comes for it outside production: `may_destroy_on_this_control_plane`
gates the scheduled sweep's destroy half on `environment == "production"`, so the delete
path alarms and files a `project:teardown-incomplete` audit row naming what survived.
Submit and deploy still only want the refusal, so the gap stays open for them. Pinned by
`test_a_relaunched_preview_is_torn_down_with_the_project_it_was_serving`.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import redis.asyncio as aioredis
import structlog
from fastapi import status
from fastapi.responses import JSONResponse

from src.core.errors import AppApiError
from src.schemas import CamelModel
from src.services.redis import build_coordination_or_503, get_redis
from src.services.redis.keys import REGISTRY_FIELD_APP_NAME

if TYPE_CHECKING:  # the runtime import stays lazy, as everywhere else in this module
    from src.services.build_sessions import SandboxReclaimBlockedError

_log = structlog.get_logger()


class ReclaimBlockedError(CamelModel):
    """The blocked-reclaim 409 body: the occupying project, and what is happening inside it."""

    message: str
    code: str
    project_id: str
    project_name: str
    dirty: bool | None  # True = known unsaved work; None = we could not tell
    # `dirty` is null whenever `building` is true, and there it means "not asked" rather than
    # "could not tell": probing a tree mid-write produces an answer true for no instant that
    # matters.
    building: bool
    # Wider than `building` and carried beside it, never folded in: `building` decides WHICH
    # dialog the client renders, `agentWorking` decides what that dialog says is happening now.
    agent_working: bool
    # WHICH REMEDY WORKS (#198). `projectId` above names a project the CALLER owns for an
    # ordinary build occupant — `stopActiveBuild`/`release` both gate on `owned_project_or_404`,
    # which that caller satisfies. For a colleague's shared view, `projectId` names its OWNER,
    # whom a recipient never owns, so those same two routes would 404 them out of their own
    # slot. `True` tells the client to route to the self-scoped give-up-my-shared-view endpoint
    # instead, which needs no project id or ownership check at all.
    is_shared_view: bool = False


class ReclaimBlockedEnvelope(CamelModel):
    """`{"error": {message, code, projectId, projectName, dirty, building, agentWorking,
    isSharedView}}` — the 409 a turn, start or relaunch returns when taking the one sandbox slot
    would destroy another project's work.

    Lives in this shared module rather than in one domain router because more than one router
    answers it."""

    error: ReclaimBlockedError


def reclaim_blocked_response(exc: SandboxReclaimBlockedError) -> JSONResponse:
    """Format the blocked-reclaim 409. Every entry point that can raise it comes through here,
    so they cannot drift into differently-worded answers.

    Names the PROJECT, not the mechanism. `dirty=None` (the container would not answer) reads
    as unsaved on purpose: claiming work is safe when nobody could check is the one wrong answer
    here. Two message shapes because only one situation is about saving — a project whose agent
    is mid-build cannot be released until the build stops, so "has unsaved changes" would point
    at a Save button the server will refuse.

    The hand-over dialog needs this same answer before the citizen chooses, and asking by
    SENDING is legitimate because every refusal on the send path is side-effect-free before
    anything is persisted. So all three routes that can raise it — send, the plan offer's build
    action, and relaunch — come through this one function rather than each wording its own; the
    count moves when a call site is added or removed."""
    if exc.building:
        message = f"“{exc.project_name}” is still being built."
    else:
        unsaved = "has unsaved changes" if exc.dirty else "may have unsaved changes"
        message = f"“{exc.project_name}” is still open and {unsaved}."
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={
            "error": {
                "message": message,
                "code": "sandbox_reclaim_blocked",
                "projectId": str(exc.project_id),
                "projectName": exc.project_name,
                "dirty": exc.dirty,
                "building": exc.building,
                "agentWorking": exc.agent_working,
                "isSharedView": exc.is_shared_view,
            }
        },
    )


async def refuse_while_build_session_live(
    user_id: uuid.UUID,
    *,
    conflict_message: str,
    app_id: uuid.UUID | None = None,
    conflict_code: str | None = None,
) -> None:
    """Raise 409 `conflict_message` while this user has a live build session, 503 when Redis
    cannot say, and return normally otherwise.

    Pass `app_id` to narrow the refusal to a live session building THAT app; omit it for the
    coarse per-user refusal. `conflict_code` gives the refusal a stable machine-readable code —
    optional since the two older callers never had one; the publish route passes it so an agent
    can tell "a build is running, wait and retry" from every other 409 without string-matching.
    """
    # Lazy import: a module-level `services.build_sessions` import cycles at load time
    # (build_sessions/__init__ → locks → api.build_sessions schemas → its router → deps
    # → back into the half-initialized build_sessions package).
    from src.services.build_sessions import lock_is_held

    with build_coordination_or_503():
        redis = get_redis()
        if not await lock_is_held(redis, user_id):
            return  # nothing is building — proceed
        if app_id is not None and not await _the_live_session_is_this_app(redis, user_id, app_id):
            return  # something IS building, but not this app — proceed
        raise AppApiError(status.HTTP_409_CONFLICT, conflict_message, code=conflict_code)


async def _the_live_session_is_this_app(
    redis: aioredis.Redis, user_id: uuid.UUID, app_id: uuid.UUID
) -> bool:
    """Does the live session the lock represents belong to `app_id`?

    The lock carries no app axis, so identity comes from the sandbox REGISTRY hash's `app_name`
    (a pure function of the app id). FAILS CLOSED: "lock held but registry unresolved" is
    AMBIGUITY, not innocence — exactly how `_start_locked`'s lock-before-provision window reads —
    so it returns True (refuse) rather than let a delete land mid-provision. Only a registry
    naming a DIFFERENT app proceeds. (A Redis error is not this branch — `read_registry` is bare
    and propagates to a 503.)"""
    from src.services.build_sessions import app_name_for, read_registry

    registry = await read_registry(redis, user_id)
    live_app_name = (registry or {}).get(REGISTRY_FIELD_APP_NAME, "")
    if not live_app_name:
        _log.info(
            "a build lock is held but names no app; refusing rather than racing it",
            user_id=str(user_id),
            app_id=str(app_id),
        )
        return True
    return live_app_name == app_name_for(app_id)
