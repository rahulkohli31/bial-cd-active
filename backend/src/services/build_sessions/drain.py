"""The absolute age ceiling — the hole the confidence tiers cannot see.

WHAT THE TIERS STRUCTURALLY MISS. Every rule in `reclaim.py` asks "is anything claiming this
container?", so one held open by a JAMMED signal — a stay nobody can name, a lease whose writer
will not stop, an open tab renewing on a timer — is claimed *by definition* and no amount of tier
logic reaches it. The drain is the only rule that does not ask, and the only one that acts on a
container a builder still considers theirs — which is why it ships behind its own flag.

TWO MARKS, AND THE SECOND ONE IS WHY THE FIRST IS NOT ENOUGH. `is_drained` is the ordinary
ceiling and a turn in flight outranks it absolutely: past the mark a build still holds the
container, and what stops counting is everything else. That courtesy is also a hole, because a
lease whose writer will not stop is indistinguishable from an agent working. `past_the_turn_bound`
is the outer mark that closes it — the ceiling plus one whole run, plus one slow tool call, after
which a turn that is still claiming the container is not a turn, it is a jam.
"""

from __future__ import annotations

import datetime as dt

from src.services.sandbox.base import SandboxIdentity


def draining_at(
    identity: SandboxIdentity, *, enabled: bool, after_hours: int
) -> dt.datetime | None:
    """When this container will be drained, or `None` if it will not be.

    `None` when the flag is off, when the container carries no trustworthy age, or when the mark
    is still far enough away to be noise. A caller renders this to the builder, so it answers
    "when", never "whether" — a boolean would leave the UI inventing the sentence.

    NO AGE MEANS NO DRAIN: an untagged container must not be drained on a guess about how old it
    is. A backfilled age (`inventory.py`) is synthetic and can only push this mark later."""
    if not enabled or identity.created_at is None:
        return None
    return identity.created_at + dt.timedelta(hours=after_hours)


def is_drained(
    identity: SandboxIdentity,
    *,
    now: dt.datetime,
    enabled: bool,
    after_hours: int,
    turn_in_flight: bool,
) -> bool:
    """Is this container past its drain mark AND free to go?

    `turn_in_flight` is the one thing that outranks the drain here, and it outranks it
    absolutely — a container past its ceiling with an agent making tool calls inside it is doing
    exactly what the platform exists to do. The whole shape of this rule is: do not interrupt,
    tell the builder, reclaim at the pause. `past_the_turn_bound` is where that courtesy ends."""
    if turn_in_flight:
        return False
    mark = draining_at(identity, enabled=enabled, after_hours=after_hours)
    return mark is not None and now >= mark


def past_the_turn_bound(
    identity: SandboxIdentity,
    *,
    now: dt.datetime,
    enabled: bool,
    after_hours: int,
    turn_grace_seconds: float,
) -> bool:
    """Is this container past the mark that outranks even a turn in flight?

    THE OUTER MARK, and `is_drained`'s deliberate courtesy is what makes it necessary: a turn in
    flight spares a container absolutely, and a jammed lease renews forever, so a container held
    by a lease nobody is driving is spared forever by a rule designed to protect real work.

    `turn_grace_seconds` is what separates the two readings, and it is generous on purpose: a
    turn that started one second before the ceiling is entitled to its whole wall-clock run plus
    the one slow tool call that run can be sitting inside when the deadline is checked. Past
    that sum, no honest turn is still going.

    NO AGE MEANS NO BOUND, exactly as `draining_at` has it — an untagged container must not be
    torn out from under an agent on a guess about how old it is."""
    mark = draining_at(identity, enabled=enabled, after_hours=after_hours)
    return mark is not None and now >= mark + dt.timedelta(seconds=turn_grace_seconds)
