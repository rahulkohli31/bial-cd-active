"""The durable-copy precondition — nothing is destroyed until its work is provably safe (U14).

R9, R11. This is the last gate before an ARM delete, and it is the one whose failure a builder
experiences directly: every other guard in this system protects money, this one protects work.

WHAT "CURRENT" MEANS. `HEAD` in the container versus the `head_sha` stamped on the recovery
bundle's blob metadata — **not** `last_modified`. Azure stamps that in whole seconds, so a Save and
an autosave landing inside one second are indistinguishable by time, and "indistinguishable" on
this path means deleting a container whose newest change was never copied.

WHY THE RECOVERY SLOT AND NOT THE SAVED BUNDLE. The recovery key is the platform's own autosave at
turn boundaries; the saved bundle is the user's explicit click. R11 asks for "their last completed
change", which is the former — a builder who never pressed Save still has work worth keeping, and
that is the population most likely to be reclaimed.

THE FALLBACK ORDER IS DELIBERATE, and its shape comes from a round-1 wording that made the gate
unsatisfiable: recover the token → read `HEAD` → compare. If TOKEN RECOVERY ITSELF fails, a present
and parseable bundle counts as confirmed — otherwise a container that is already dead can never be
collected, which is the entire point of the exercise. If there is no parseable bundle either,
escalate. The real comparison still happens in the normal case.

"STORAGE IS OFF" IS NOT "THERE IS NO WORK TO PRESERVE" (Q4). `manager.py` returns `False` from its
bundle-presence check on `StorageUnconfiguredError` and documents it as a *confirmed absent* —
which is right for its caller, because on a storage-off deployment you must not offer a restore
that cannot work. It is exactly wrong here: consumed by a destroy path, "confirmed no bundle" is
"nothing to preserve, safe to delete", so the most natural misconfiguration in the system would
produce a worker that deletes the entire fleet while believing it had verified every container.
This module distinguishes a fact about the DEPLOYMENT (storage unconfigured → escalate; in truth
the worker should never have started) from a fact about the CONTAINER (storage reachable, no
bundle for this app).
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass

import structlog

from src.services.storage import get_storage, recovery_key
from src.services.storage.errors import StorageError, StorageUnconfiguredError

_log = structlog.get_logger()


class CopyState(enum.StrEnum):
    """Whether this container's work is provably safe to lose."""

    #: The recovery bundle's `head_sha` matches the container's `HEAD`, or the container could
    #: not be reached but a parseable bundle exists. Destruction may proceed.
    CONFIRMED_CURRENT = "confirmed_current"
    #: A copy exists but is behind the container. Take one first.
    STALE = "stale"
    #: Nothing could be confirmed — no parseable bundle, an unreachable store, or storage not
    #: configured at all. SPARE AND REPORT. Never destroy.
    UNCONFIRMED = "unconfirmed"


@dataclass(frozen=True)
class CopyVerdict:
    state: CopyState
    reason: str

    @property
    def may_destroy(self) -> bool:
        """The single question every caller actually asks.

        A property rather than a comparison at each call site, because `state is CONFIRMED_CURRENT`
        spelled out four times is four chances to write `is not STALE` and quietly authorise the
        two states that mean "we could not tell"."""
        return self.state is CopyState.CONFIRMED_CURRENT


async def confirm_durable_copy(
    app_id: uuid.UUID, *, container_head: str | None, container_dirty: bool | None
) -> CopyVerdict:
    """Is this container's work provably preserved? (R9, R11.)

    `container_head` is the container's current `HEAD`, or `None` when it could not be read —
    which is the ordinary case for the population this gate exists to judge, since an orphan has
    no registry record and may not be reachable at all.

    `container_dirty` is whether that container's working tree has uncommitted changes, and it is
    KEYWORD-REQUIRED WITH NO DEFAULT on purpose. A permissive default on a gate that authorises
    destruction is how the bug below shipped; a caller that does not know must say `None` and be
    refused, not stay silent and be believed. `None` means the probe did not answer.

    A HEAD MATCH ALONE STOPPED MEANING "PRESERVED" WHEN THE AGENT STOPPED COMMITTING (U19).
    The comparison below was written when the build agent committed as it worked, so a turn that
    wrote files MOVED `HEAD` and a copy from the previous turn was detectably behind it. U19
    deleted that commit discipline — the platform now commits only at the turn boundary — so
    "HEAD unchanged + dirty tree" is the normal shape of every building turn. A turn that dies
    before its finalizer (process death, OOM, a deploy restart, eviction) therefore leaves `HEAD`
    exactly where the LAST turn's recovery copy was stamped, and a HEAD-only comparison reads that
    as provably preserved and destroys a whole turn's uncommitted work — writing an audit row
    saying it was safe. The dirty flag is what closes that, and it is why this signature changed
    rather than the call sites quietly passing `head` alone.

    The plan that removed the commits guards the recovery-copy WRITE path against the same new
    normal (`test_a_dirty_tree_at_unchanged_head_still_writes_a_recovery_copy`). This is the same
    lesson applied to the DESTROY path, which that test does not reach.

    FAILS TOWARD SPARING, ALWAYS. Every branch that could not establish a fact returns
    `UNCONFIRMED`, and `UNCONFIRMED` never authorises a delete. A timeout is not a death
    certificate, and neither is a storage blip."""
    try:
        store = get_storage()
    except StorageUnconfiguredError:
        # THE MISCONFIGURATION THAT WOULD DELETE THE FLEET. Read as "confirmed no bundle" — which
        # is how the build path is entitled to read it — this becomes "nothing to preserve, safe
        # to delete" for every container at once. It is a fact about the deployment, not about
        # anybody's work, and a worker on such a deployment should never have started.
        _log.error(
            "durable-copy gate asked on a storage-off deployment; refusing to authorise anything",
            app_id=str(app_id),
        )
        return CopyVerdict(CopyState.UNCONFIRMED, "the object store is not configured")

    try:
        meta = await store.head(recovery_key(app_id))
    except StorageError:
        _log.warning("recovery slot unreadable; sparing", app_id=str(app_id), exc_info=True)
        return CopyVerdict(CopyState.UNCONFIRMED, "the object store could not be read")

    if meta is None:
        return CopyVerdict(CopyState.UNCONFIRMED, "no recovery copy exists for this app")

    stamped = (meta.metadata or {}).get("head_sha")
    if not stamped:
        # A bundle whose head is unknown cannot be compared against anything. Older bundles
        # predate the metadata stamp, and this is exactly the "unreadable signal" R4 covers.
        return CopyVerdict(CopyState.UNCONFIRMED, "the recovery copy carries no head_sha")

    if container_head is None:
        # TOKEN RECOVERY OR THE CONTAINER READ FAILED. A present, parseable bundle counts as
        # confirmed here — deliberately. Requiring the live comparison in this branch is what made
        # the round-1 wording unsatisfiable: a container that is already dead can never answer,
        # so the gate would have spared every genuinely-dead container forever and collected
        # nothing at all.
        return CopyVerdict(
            CopyState.CONFIRMED_CURRENT,
            "the container could not be read; a parseable recovery copy stands in",
        )

    if stamped != container_head:
        return CopyVerdict(CopyState.STALE, "the recovery copy is behind HEAD")

    # HEAD MATCHES — now ask the question a HEAD comparison cannot answer (see the docstring).
    if container_dirty is None:
        # We reached the container and read its HEAD, but not its tree. That is an unestablished
        # fact on a path that authorises destruction, so it spares rather than confirms.
        return CopyVerdict(
            CopyState.UNCONFIRMED,
            "the recovery copy matches HEAD, but the working tree could not be read",
        )
    if container_dirty:
        # The copy is not behind HEAD — it is behind the WORKING TREE, which is the shape every
        # building turn now has. STALE rather than UNCONFIRMED because this is a known state with
        # a known remedy: take a copy first, then reclaim.
        return CopyVerdict(
            CopyState.STALE,
            "the recovery copy matches HEAD but the working tree has uncommitted work",
        )
    return CopyVerdict(
        CopyState.CONFIRMED_CURRENT, "the recovery copy matches HEAD on a clean tree"
    )
