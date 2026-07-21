"""The Redis error TAXONOMY, and the single place it becomes an HTTP status (KD-1).

Two Redis failure modes look alike and are not alike, and collapsing them is an
anti-pattern this repo has already shipped and been burned by (see
`docs/solutions/best-practices/green-tests-that-prove-nothing-fixture-and-passthrough-coverage-gaps-2026-07-17.md`
— a uniform "unavailable ⇒ 503" 503'd every build start on deployments that had
deliberately switched the dependency off, with the whole suite green because the
fixture always bound one):

* `RedisNotConfiguredError` is a CERTAIN answer. Redis is genuinely optional outside
  production (`settings.redis: RedisConfig | None`), and with no Redis there is no
  build-session subsystem at all — so no lock can be held, and the caller PROCEEDS.
* `RedisError` is AMBIGUITY. The store exists and failed to answer, so a check over it
  decided nothing → 503 (`.claude/rules/fail-first.md`: any error or ambiguity denies).

Anything else propagates untouched — this is a taxonomy, never a catch-all.

WHY THIS LIVES IN `services/redis/` and not `services/build_sessions/` (KD-5): the
build-sessions package has a real module-level import cycle
(`build_sessions/__init__ → locks → api.build_sessions.schemas → its router → deps →
back into the half-initialized package`), which `apps/router.py` works around with a
lazy import. `services/redis/` is cycle-free, so a helper here is importable from any
call site without one.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Final

import structlog
from fastapi import status
from redis.exceptions import RedisError

from src.core.errors import AppApiError
from src.services.redis.client import RedisNotConfiguredError

_log = structlog.get_logger()

# USER-FACING COPY, not a log line. `portal/src/hooks/useBuildSession.ts:130` surfaces the
# backend's 503 message VERBATIM (asserted at `useBuildSession.test.ts:76-83`), so this
# string is read by citizen developers: professional, actionable, and leaking no internal
# detail (`.claude/rules/security.md`). It is hoisted to a constant so the router and this
# helper can never drift apart into two subtly different apologies.
BUILD_COORDINATION_UNAVAILABLE_MSG: Final = (
    "Build coordination is temporarily unavailable. Please try again."
)


@contextmanager
def build_coordination_or_503() -> Iterator[None]:
    """Run a build-coordination check under the two-tier taxonomy above.

    Wrap the `get_redis()` call TOGETHER with the commands that follow it, so both tiers
    are read at one seam::

        with build_coordination_or_503():
            redis = get_redis()
            if await lock_is_held(redis, user_id):
                raise AppApiError(status.HTTP_409_CONFLICT, "...")

    Note the flow-control shape: an unconfigured Redis SKIPS THE REST OF THE BLOCK and
    resumes after it — the caller proceeds as if the check had passed, because it has
    (there is nothing to hold a lock). Put nothing in the block that must run regardless.

    Deliberately a plain `contextmanager` rather than an async one: `with` composes with
    an `await`-bearing body just fine, and a sync generator is the smaller surface.
    """
    try:
        yield
    except RedisNotConfiguredError:
        # Not an error state: the DEFINED meaning of "no Redis" is "no build sessions".
        _log.debug("redis is not configured; skipping the build-coordination check")
        return
    except RedisError as exc:
        raise AppApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE, BUILD_COORDINATION_UNAVAILABLE_MSG
        ) from exc
