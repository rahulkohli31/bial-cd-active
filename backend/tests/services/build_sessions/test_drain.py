"""U16 — the twenty-four-hour drain (R21).

The tiers ask "is anything claiming this container?". A container held open by a jammed signal is
claimed by definition, so the tiers can never reach it. The drain is the only rule that does not
ask — and therefore the only one that acts on a container a builder still considers theirs, which
is why it ships flag-off and why AE14's shape is *do not interrupt, tell them, reclaim at the
pause*.
"""

from __future__ import annotations

import datetime as dt

import pytest

from src.services.build_sessions.drain import draining_at, is_drained
from src.services.sandbox.base import (
    KIND_BUILD_SANDBOX,
    TAG_CREATED_AT,
    TAG_KIND,
    SandboxIdentity,
    identity_from_tags,
)

NOW = dt.datetime(2026, 8, 11, 12, 0, tzinfo=dt.UTC)


def _aged(hours: float) -> SandboxIdentity:
    return identity_from_tags(
        {
            TAG_KIND: KIND_BUILD_SANDBOX,
            TAG_CREATED_AT: (NOW - dt.timedelta(hours=hours)).isoformat(),
        }
    )


def test_with_the_flag_off_nothing_ever_drains() -> None:
    """The default posture everywhere. ADR-0014 records that long-session behaviour was never
    validated and the longest observed live session is ~31 minutes, so this threshold targets a
    scenario nobody has measured — and it is the only rule that touches a container its builder
    still considers theirs."""
    old = _aged(100)

    assert draining_at(old, enabled=False, after_hours=24) is None
    assert is_drained(old, now=NOW, enabled=False, after_hours=24, turn_in_flight=False) is False


def test_a_turn_in_flight_is_never_interrupted() -> None:
    """*Covers AE14.* A 24-hour-old container with an agent making tool calls inside it is doing
    precisely what the platform exists to do. The drain waits for the pause."""
    assert (
        is_drained(_aged(48), now=NOW, enabled=True, after_hours=24, turn_in_flight=True) is False
    )


def test_a_builder_who_keeps_working_keeps_the_container() -> None:
    """The same property stated from the builder's side: as long as turns keep starting, the
    drain never lands."""
    old = _aged(200)
    for _ in range(5):  # turn after turn, well past the mark
        assert is_drained(old, now=NOW, enabled=True, after_hours=24, turn_in_flight=True) is False


def test_past_the_mark_and_idle_the_container_drains() -> None:
    assert (
        is_drained(_aged(25), now=NOW, enabled=True, after_hours=24, turn_in_flight=False) is True
    )


def test_before_the_mark_it_does_not() -> None:
    assert (
        is_drained(_aged(23), now=NOW, enabled=True, after_hours=24, turn_in_flight=False) is False
    )


def test_a_container_with_no_trustworthy_age_is_never_drained() -> None:
    """An untagged container escalates to a human under AE2; draining it would be acting on a
    guess about its age, which is the one thing R2 exists to forbid."""
    untagged = identity_from_tags({})

    assert draining_at(untagged, enabled=True, after_hours=24) is None
    assert (
        is_drained(untagged, now=NOW, enabled=True, after_hours=24, turn_in_flight=False) is False
    )


def test_the_drain_time_is_answered_as_when_not_whether() -> None:
    """The value is rendered to a builder, so a boolean would leave the UI inventing the
    sentence. "Your workspace refreshes at 14:00" is a different message from "your workspace
    will be reclaimed", and only one of them is true."""
    mark = draining_at(_aged(1), enabled=True, after_hours=24)

    assert mark == NOW + dt.timedelta(hours=23)


@pytest.mark.parametrize("hours", [1, 24, 72])
def test_the_threshold_is_configurable_rather_than_baked(hours: int) -> None:
    """Because the number is admittedly unvalidated, it must be movable without a code change —
    an operator who measures a real long session should be able to act on what they learned."""
    mark = draining_at(_aged(0), enabled=True, after_hours=hours)

    assert mark == NOW + dt.timedelta(hours=hours)
