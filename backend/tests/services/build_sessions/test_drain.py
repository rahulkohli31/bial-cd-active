"""The twenty-four-hour drain.

The tiers ask "is anything claiming this container?". A container held open by a jammed signal is
claimed by definition, so the tiers can never reach it. The drain is the only rule that does not
ask — and therefore the only one that acts on a container a builder still considers theirs, which
is why it ships flag-off and why its rule is *do not interrupt, tell them, reclaim at the
pause*.
"""

from __future__ import annotations

import datetime as dt

import pytest

from src.services.build_sessions.drain import draining_at, is_drained, past_the_turn_bound
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


def test_with_no_sandbox_configured_nothing_ever_drains() -> None:
    """The ceiling is not optional, but it is not INVENTABLE either: a deployment with no sandbox
    configured has no container ages to reason about, and `the_ceiling_hours` answers `None`
    there. Draining on that would be draining on a guess."""
    old = _aged(100)

    assert draining_at(old, after_hours=None) is None
    assert is_drained(old, now=NOW, after_hours=None, turn_in_flight=False) is False


def test_a_turn_in_flight_is_never_interrupted() -> None:
    """A 24-hour-old container with an agent making tool calls inside it is doing precisely what
    the platform exists to do. The drain waits for the pause."""
    assert is_drained(_aged(48), now=NOW, after_hours=24, turn_in_flight=True) is False


def test_a_builder_who_keeps_working_keeps_the_container() -> None:
    """The same property stated from the builder's side: as long as turns keep starting, the
    drain never lands."""
    old = _aged(200)
    for _ in range(5):
        assert is_drained(old, now=NOW, after_hours=24, turn_in_flight=True) is False


def test_past_the_mark_and_idle_the_container_drains() -> None:
    assert is_drained(_aged(25), now=NOW, after_hours=24, turn_in_flight=False) is True


def test_before_the_mark_it_does_not() -> None:
    assert is_drained(_aged(23), now=NOW, after_hours=24, turn_in_flight=False) is False


def test_a_container_with_no_trustworthy_age_is_never_drained() -> None:
    """An untagged container is escalated to a human, never drained. Why an age Azure reports
    is not trusted lives in `inventory.py`."""
    untagged = identity_from_tags({})

    assert draining_at(untagged, after_hours=24) is None
    assert is_drained(untagged, now=NOW, after_hours=24, turn_in_flight=False) is False


def test_the_drain_time_is_answered_as_when_not_whether() -> None:
    """The value is rendered to a builder, so a boolean would leave the UI inventing the
    sentence. "Your workspace refreshes at 14:00" is a different message from "your workspace
    will be reclaimed", and only one of them is true."""
    mark = draining_at(_aged(1), after_hours=24)

    assert mark == NOW + dt.timedelta(hours=23)


@pytest.mark.parametrize("hours", [1, 24, 72])
def test_the_threshold_is_configurable_rather_than_baked(hours: int) -> None:
    """Because the number is admittedly unvalidated, it must be movable without a code change —
    an operator who measures a real long session should be able to act on what they learned."""
    mark = draining_at(_aged(0), after_hours=hours)

    assert mark == NOW + dt.timedelta(hours=hours)


# --- the outer mark, which nothing outranks ---------------------------------


def test_a_turn_still_holds_the_container_just_past_the_ceiling() -> None:
    """The courtesy is real and it must stay real: a build that started shortly before the mark
    is entitled to finish. Only the sum of a whole run and one slow tool call is long enough to
    say, honestly, that nothing is working in there."""
    assert (
        past_the_turn_bound(_aged(2.4), now=NOW, after_hours=2, turn_grace_seconds=2400) is False
    )


def test_a_lease_that_will_not_stop_renewing_runs_out_of_benefit_of_the_doubt() -> None:
    """THE HOLE THIS CLOSES. A jammed lease is indistinguishable from an agent making tool calls,
    so the arm that spares a live turn spares a wedged one forever — and a wedged container is
    exactly the population a ceiling exists to collect."""
    assert past_the_turn_bound(_aged(5), now=NOW, after_hours=2, turn_grace_seconds=2400) is True


def test_the_outer_mark_needs_a_configured_ceiling_too() -> None:
    """The outer mark follows the mark it sits behind: with no sandbox configured there is no
    ceiling to be past, including this one."""
    assert (
        past_the_turn_bound(_aged(500), now=NOW, after_hours=None, turn_grace_seconds=2400)
        is False
    )


def test_an_untagged_container_is_never_torn_out_from_under_an_agent() -> None:
    """No age means no bound — the one clause that can interrupt live work never reaches it.
    A container whose birthday cannot be proved is reported, never cut."""
    ageless = identity_from_tags({TAG_KIND: KIND_BUILD_SANDBOX})

    assert past_the_turn_bound(ageless, now=NOW, after_hours=2, turn_grace_seconds=2400) is False


def test_the_outer_mark_is_strictly_later_than_the_ordinary_one() -> None:
    """Stated as an ordering rather than two numbers: whatever the grace is tuned to, a container
    must always reach the ordinary ceiling before it reaches the one that cuts a turn."""
    just_past = _aged(2.1)

    assert is_drained(just_past, now=NOW, after_hours=2, turn_in_flight=False) is True
    assert past_the_turn_bound(just_past, now=NOW, after_hours=2, turn_grace_seconds=2400) is False
