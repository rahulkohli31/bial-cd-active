"""The shared "is a build session live right now?" guard, used by every destructive
owner-facing route that a concurrent build would race (U8).

Two routes need the same question answered and owe the user the same three answers, so
the question is asked in ONE place: `apps.submit` (copying a snapshot out from under a
live build captures the previous build's bundle — valid bytes, wrong tree, undetectable
by any header check) and `projects.delete_project` (deleting a project mid-build destroys
every file change since the last snapshot, and snapshots are written only at finalize —
R9's silent race).

WHY IT LIVES HERE, in `api/v1/`, and not in a service package (KD-5). `services/redis/`
is the cycle-free home for the *error taxonomy* (`services/redis/errors.py`), and this
guard composes it — but the guard itself is build-session domain logic that must reach
`lock_is_held` / `read_registry` / `app_name_for`, and `services/build_sessions` imports
`services/redis`, so a module-level import either way is a cycle. It is therefore a shared
`api/v1/` helper (the `api/v1/pagination.py` precedent: cross-domain, HTTP-shaped, owned by
no single domain package) that keeps the LAZY import `apps/router.py` already carried.

THE THREE ANSWERS (KD-1's two-tier taxonomy, plus the guard's own):

* `RedisNotConfiguredError` → PROCEED. With no Redis there is no build-session subsystem
  at all, so no lock can be held (dev/test only — production requires Redis at the
  settings gate).
* `RedisError` → 503. The store exists and failed to answer, so the check decided
  nothing (`.claude/rules/fail-first.md`: any error or ambiguity denies).
* A lock genuinely held → 409, in the caller's own words.

SCOPE — read this before reusing the helper. The lock is keyed on the USER
(`bial:sandbox:lock:{user_id}`) and carries no app or project axis, so a bare
`lock_is_held` answers "is this user building ANYTHING?", which is the wrong question for
a per-resource guard: with one app per project and many projects per user, building
project A would 409 a delete of unrelated project B. Callers that own a specific app pass
`app_id` and get the narrow answer; callers that deliberately want the coarse one (submit,
whose refusal predates this scoping and stays as shipped) omit it. See
`_the_live_session_is_this_app` for how the narrowing resolves, and for why it fails CLOSED.

WHAT THIS GUARD DOES **NOT** COVER — do not read a passing guard as "no container is
serving this app". A relaunched preview holds NO lock by design (`manager.py`'s
`relaunch_preview`: the scope "RELEASES the per-user lock on exit", and the container's
lifetime is owned by an explicit stay of execution on the registry hash instead,
`locks.py::grant_stay_of_execution`). So the container-still-serving state is PRECISELY
the state where `lock_is_held` is False: this guard returns without refusing, and the
delete proceeds with the container left running and serving a deleted project's UI. That
gap — recorded in
`docs/solutions/architecture-patterns/long-lived-resource-lifecycle-ownership-triad-2026-07-19.md`
— is STILL OPEN. Closing it means reading `preview_stay_until`
(`stay_of_execution_is_current`) and calling sandbox teardown from the delete path, which
needs a `SandboxDep` `delete_project` does not have. It is pinned by
`test_a_relaunched_preview_does_not_block_the_delete_and_is_not_torn_down`; do not mark it
closed because this guard shipped.
"""

from __future__ import annotations

import uuid

import redis.asyncio as aioredis
import structlog
from fastapi import status

from src.core.errors import AppApiError
from src.services.redis import build_coordination_or_503, get_redis
from src.services.redis.keys import REGISTRY_FIELD_APP_NAME

_log = structlog.get_logger()


async def refuse_while_build_session_live(
    user_id: uuid.UUID, *, conflict_message: str, app_id: uuid.UUID | None = None
) -> None:
    """Raise 409 `conflict_message` while this user has a live build session, 503 when
    Redis cannot say, and return normally otherwise.

    Pass `app_id` to narrow the refusal to a live session building THAT app; omit it for
    the coarse per-user refusal (any live build blocks the action).
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
        raise AppApiError(status.HTTP_409_CONFLICT, conflict_message)


async def _the_live_session_is_this_app(
    redis: aioredis.Redis, user_id: uuid.UUID, app_id: uuid.UUID
) -> bool:
    """Does the live session the lock represents belong to `app_id`?

    The lock carries no app axis, so the app identity is recovered from the sandbox
    REGISTRY hash, whose `app_name` field is a pure, stable function of the app id
    (`app_name_for(app_id)` — `sbx-` + 28 hex chars, written by the sandbox client at
    provision time). Equal name ⇒ the live session is this app's.

    FAILS CLOSED, deliberately. "The lock is held but the registry does not resolve to an
    app" is AMBIGUITY, not evidence of innocence, and it has a real cause: `_start_locked`
    takes the lock BEFORE it provisions the container that writes the registry hash, so
    that window reads exactly this way — lock held, registry absent. Proceeding there is
    the silent race R9 forbids (the delete lands mid-provision), so an unresolvable
    registry returns True and the caller refuses. Only a registry that positively names a
    DIFFERENT app buys a proceed.

    (A Redis ERROR while reading the registry is not this branch: `read_registry` is bare
    by module policy, so it propagates to `build_coordination_or_503` and becomes a 503 —
    "cannot answer" and "answers something else" stay distinct, per KD-1.)
    """
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
