"""The twenty-four-hour drain — the hole the confidence tiers cannot see (U16, R21).

WHAT THE TIERS STRUCTURALLY MISS. Every rule in `reclaim.py` asks "is anything claiming this
container?" A container held open by a JAMMED signal — a stay that keeps getting renewed by
something nobody can name, a lease whose writer will not stop — is claimed *by definition*, so no
amount of tier logic will ever reach it. The drain is the only rule that does not ask.

IT IS ALSO THE ONLY RULE THAT ACTS ON A CONTAINER A BUILDER STILL CONSIDERS THEIRS, which is why
it ships behind its own flag, off everywhere. ADR-0014 records that long-session behaviour was
never validated and the longest observed live session is about **31 minutes** — so a 24-hour
threshold is aimed at a scenario nobody has measured. Stating that is more useful than defending
the number.

A TURN IN FLIGHT IS NEVER INTERRUPTED. Past the mark, a build still holds the container; what
stops counting is everything else — interaction, served traffic, the ordinary end-of-turn stay.
The drain lands at the builder's next pause, and they are TOLD before it does.
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

    NO AGE MEANS NO DRAIN. An untagged container escalates to a human under AE2 and must not be
    drained on a guess about how old it is; a backfilled one carries a synthetic age that reads
    as *new*, which errs toward waiting exactly as R2 intends."""
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

    `turn_in_flight` is the one thing that outranks the drain, and it outranks it absolutely — a
    24-hour-old container with an agent making tool calls inside it is doing exactly what the
    platform exists to do. AE14's whole shape is: do not interrupt, tell the builder, reclaim at
    the pause."""
    if turn_in_flight:
        return False
    mark = draining_at(identity, enabled=enabled, after_hours=after_hours)
    return mark is not None and now >= mark
