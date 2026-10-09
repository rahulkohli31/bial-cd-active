"""The stopwatch one sandbox start carries through every layer it passes.

The control plane opens it at the door and hands it down through a context variable, so the
client can time the steps inside the create seam without a change to the `SandboxClient`
methods every fake implements. It only measures: it writes nothing anywhere and never raises
into a start. Outside a timed start the context holds a stopwatch that keeps nothing.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Literal

#: The instants a start passes, in the order it passes them.
Split = Literal["admitted", "settings", "created", "dev_started", "first_page"]

#: The steps timed inside a stage. `files` and `restore_exec` are the bundle push and the restore
#: script; together with `registry_write` and `dev_start` they fill the gap between a container
#: being created and its dev server starting. `bearer_read` and `configure` are steps of a claim.
Lap = Literal["files", "restore_exec", "registry_write", "dev_start", "bearer_read", "configure"]

#: Why a start created a container rather than claiming a ready one.
Miss = Literal["no_ready", "unhealthy", "claim_failed", "size_zero", "connector"]


def _ms(seconds: float) -> int:
    return int(seconds * 1000)


class Stopwatch:
    """Splits are the boundaries between stages; laps time one step. A later split or lap of the
    same name replaces the earlier one, so a retried create is timed by the attempt that
    succeeded. Once stopped, nothing moves."""

    def __init__(self) -> None:
        self.started_at = datetime.now(UTC)
        self._origin = time.monotonic()
        self._splits: dict[Split, float] = {}
        self.laps: dict[str, int] = {}
        #: Whether the restore reinstalled packages; `None` when no restore ran.
        self.reinstalled: bool | None = None
        #: Whether the container came from the pool, why not when it did not, and how many were
        #: ready when the start asked. A retried birth is described by its last attempt.
        self.claimed = False
        self.miss_reason: Miss | None = None
        self.ready_count: int | None = None
        self._stopped = False

    def split(self, name: Split) -> None:
        if not self._stopped:
            self._splits[name] = time.monotonic()

    @contextmanager
    def lap(self, name: Lap) -> Iterator[None]:
        began = time.monotonic()
        try:
            yield
        finally:
            if not self._stopped:
                self.laps[name] = _ms(time.monotonic() - began)

    def saw_the_restore_reinstall(self, reinstalled: bool) -> None:
        if not self._stopped:
            self.reinstalled = reinstalled

    def took_a_ready_one(self, *, ready_count: int) -> None:
        if not self._stopped:
            self.claimed, self.miss_reason, self.ready_count = True, None, ready_count

    def missed(self, reason: Miss, *, ready_count: int | None) -> None:
        if not self._stopped:
            self.claimed, self.miss_reason, self.ready_count = False, reason, ready_count

    def stop(self) -> None:
        self._stopped = True

    def elapsed_ms(self, since: Split | None, until: Split) -> int | None:
        """Milliseconds between two splits, `None` for the door. `None` when either is missing."""
        began = self._origin if since is None else self._splits.get(since)
        ended = self._splits.get(until)
        if began is None or ended is None:
            return None
        return _ms(ended - began)


# What the context holds outside a timed start: stopped, so every reading is dropped.
_NOBODY_IS_TIMING = Stopwatch()
_NOBODY_IS_TIMING.stop()
_running: ContextVar[Stopwatch] = ContextVar("sandbox_start_stopwatch", default=_NOBODY_IS_TIMING)


def running_stopwatch() -> Stopwatch:
    """The stopwatch of the start this code runs inside."""
    return _running.get()


@contextmanager
def timed_by(stopwatch: Stopwatch) -> Iterator[None]:
    """Run the enclosed block as part of `stopwatch`'s start. Set and reset in one task: a reset
    from another task's context raises."""
    token = _running.set(stopwatch)
    try:
        yield
    finally:
        _running.reset(token)
