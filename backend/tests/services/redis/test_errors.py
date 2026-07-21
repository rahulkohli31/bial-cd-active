"""U2 — the two-tier Redis error taxonomy and its single 503 mapping (KD-1)."""

from __future__ import annotations

import pytest
from fastapi import status
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError

from src.core.errors import AppApiError
from src.services.redis import (
    BUILD_COORDINATION_UNAVAILABLE_MSG,
    RedisNotConfiguredError,
    build_coordination_or_503,
)


def test_unconfigured_redis_proceeds_without_raising() -> None:
    """Tier 1 — a CERTAIN answer. With no Redis there is no build-session subsystem, so no
    lock can be held and the caller carries on. Folding this into the 503 tier is the exact
    anti-pattern that once 503'd every build start on storage-off deployments."""
    reached_the_body = False
    with build_coordination_or_503():
        reached_the_body = True
        raise RedisNotConfiguredError("no REDIS__URL in this environment")

    assert reached_the_body  # and control resumed here rather than propagating


def test_redis_error_becomes_a_503_with_the_approved_copy() -> None:
    """Tier 2 — real AMBIGUITY. The store failed to answer, so the check fails closed."""
    cause = RedisConnectionError("connection refused")
    with pytest.raises(AppApiError) as caught:
        with build_coordination_or_503():
            raise cause

    assert caught.value.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert caught.value.message == BUILD_COORDINATION_UNAVAILABLE_MSG
    assert caught.value.__cause__ is cause


def test_the_approved_copy_is_user_facing_and_leaks_nothing() -> None:
    """The frontend surfaces this string VERBATIM (`useBuildSession.ts:130`), so it is
    product copy, not a log line: professional, actionable, and free of internal detail."""
    assert (
        BUILD_COORDINATION_UNAVAILABLE_MSG
        == "Build coordination is temporarily unavailable. Please try again."
    )


def test_an_unrelated_exception_propagates_untouched() -> None:
    """Not a catch-all. Anything that is not a Redis outcome must reach its own handler —
    including the 409s the guarded bodies raise themselves."""
    with pytest.raises(AppApiError) as caught:
        with build_coordination_or_503():
            raise AppApiError(status.HTTP_409_CONFLICT, "A build session is still running.")

    assert caught.value.status_code == status.HTTP_409_CONFLICT

    with pytest.raises(ValueError, match="unrelated"):
        with build_coordination_or_503():
            raise ValueError("unrelated")


def test_a_clean_body_is_a_no_op() -> None:
    results: list[str] = []
    with build_coordination_or_503():
        results.append("ran")
    results.append("resumed")
    assert results == ["ran", "resumed"]


def test_unconfigured_is_not_a_redis_error_subclass() -> None:
    """The two tiers are disjoint by TYPE, not by catch ordering — so a future reader
    cannot accidentally collapse them by reordering the except clauses."""
    assert not issubclass(RedisNotConfiguredError, RedisError)
