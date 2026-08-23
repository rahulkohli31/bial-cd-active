"""The browser client-error report store — the receiving half of the app's own error reporter
(U13, R17 runtime half, AE11).

The generated app has ALWAYS captured its own `window.onerror` / `unhandledrejection` /
`console.error` and relayed them to the framing portal — `sandbox/template/components/bial/
error-capture.tsx`, whose header has said since Stage 0 that the capture side is "consumed by
NOBODY". This module is the first consumer. The portal POSTs what it caught to the ingest route,
the report lands here, and `selfheal.verify` drains it as part of the health verdict, so an app
that answers 200 and then dies in the browser can no longer be called green.

WHY A STORE AND NOT A DIRECT CALL INTO THE TURN. A report arrives on its own HTTP request, in a
different task from the turn that is building the app, and there is no in-flight thing to hand it
to: the turn may be mid-run, between runs, or finished for the last time. Parking the report and
letting the next verify come and collect it is what lets all three cases work without any of them
knowing about this feature — and it is also, structurally, the answer to "a late report must not
resurrect a finished turn". Nothing here PUSHES. A report that lands after a turn's last verify
simply waits for the next one, or expires unread.

IN-PROCESS, deliberately. A build session already exists only in the process that started it —
`SessionManager` holds live sessions in a plain dict and `manager.get(session_id)` reads it — so
a Redis-backed store here would be the one cross-process component of a per-process feature, and
would still be read by a verify running in the process that owns the session anyway.

BOUNDED THREE WAYS, because the writer is a crashing browser and a crash LOOP is the ordinary
shape of this input: at most `MAX_REPORTS_PER_APP` reports are kept per app, at most `MAX_APPS`
apps are tracked at once, and a report older than `REPORT_TTL_S` is dropped unread. Note which
reports survive the per-app cap: the FIRST ones in the window, not the newest. A render loop that
throws the same error four hundred times is four hundred copies of one diagnostic, and the first
copy is the one closest to the fault; keeping the newest would mean a fast loop could push the
original cause out of its own report.

Everything in a report is UNTRUSTED TEXT produced by code running inside the generated app —
third-party npm, fetched content, a compromised dependency. This module stores it verbatim and
makes no claim about it; the redaction, the truncation and the data-only frame that stops it
reading as instructions all live at the point of use (`errors.from_client`).
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

MAX_REPORTS_PER_APP = 10
"""How many reports one app may have waiting for the next verify.

Ten is enough for a page that trips several distinct errors on one render and far too few to be
worth flooding: the eleventh report in a window is dropped at O(1) cost, and the ingest route says
so in its response rather than pretending it kept it."""

MAX_APPS = 256
"""How many apps may have reports parked at once, evicted oldest-first.

A ceiling on the whole structure, not just on each entry — without it a control plane serving many
users accumulates one list per app forever, since an app whose reports are never drained (nobody
ever built it again) has nothing else that would remove it."""

REPORT_TTL_S = 600.0
"""How long a parked report stays relevant.

It has to outlive one agent run, because that is the gap between the crash the browser saw and the
verify that will read it — a long build run is minutes. It must NOT be indefinite: a report from
an hour ago describes an app the agent has since changed, and marking a fresh turn unhealthy on it
would spend a repair round-trip on a fault that may no longer exist."""


@dataclass(frozen=True)
class ClientErrorReport:
    """One crash the app's own reporter saw in the browser, as it was received.

    `source` / `title` / `stack` are the app's words, capped at the API boundary and otherwise
    untouched — see the module header on why nothing is cleaned here. `parked_at` is OUR clock
    (`time.monotonic`, so a system clock change cannot make a report look older or newer than it
    is), never the `ts` the app sent: the reporter's timestamp is app-controlled, and the one
    thing this value is used for is expiry."""

    source: str
    title: str
    stack: str
    parked_at: float


class ClientErrorStore:
    """The bounded per-app parking area. One instance, module-global (see `_STORE`).

    Not a `dict` of lists on its own, because all three bounds have to be enforced on every write
    and an inline structure would leave that to each call site to remember."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        # Insertion-ordered so the MAX_APPS eviction has an oldest to evict. `move_to_end` on
        # every write keeps a busy app from being evicted by a quiet one.
        self._parked: OrderedDict[str, list[ClientErrorReport]] = OrderedDict()
        # An injected clock, so the TTL can be tested by advancing time rather than by patching
        # `time.monotonic` globally — a test that reaches into the stdlib to age one report also
        # ages every timeout, sleep and event loop that happens to run while it holds the patch.
        # Production never passes one; the default IS the real clock.
        self._clock = clock

    def record(self, app_name: str, *, source: str, title: str, stack: str) -> bool:
        """Park one report against an app. Returns whether it was KEPT.

        `False` is not an error — it means this app already has `MAX_REPORTS_PER_APP` reports
        waiting and this one was dropped. The caller reports that honestly rather than answering
        "recorded" for a report that went nowhere: a client that believes every crash is being
        collected would have no way to know its loop is being throttled."""
        now = self._clock()
        self._expire(now)
        parked = self._parked.get(app_name)
        if parked is None:
            parked = []
            self._parked[app_name] = parked
            # Evict the least-recently-written app, never the one we just created — the pop must
            # happen AFTER the insert or a store already at MAX_APPS would drop the new entry.
            while len(self._parked) > MAX_APPS:
                self._parked.popitem(last=False)
        self._parked.move_to_end(app_name)
        if len(parked) >= MAX_REPORTS_PER_APP:
            return False
        parked.append(ClientErrorReport(source=source, title=title, stack=stack, parked_at=now))
        return True

    def drain(self, app_name: str) -> list[ClientErrorReport]:
        """Take everything parked for this app, oldest first, and forget it.

        DRAINING, not peeking, and that is the contract the verify depends on: a report counts
        against exactly one health verdict. Left in place it would re-fail every subsequent verify
        of the same build, so one browser crash would burn the entire self-heal budget re-reporting
        itself while the agent fixed it on the first pass."""
        self._expire(self._clock())
        return self._parked.pop(app_name, [])

    def forget_everything(self) -> None:
        """Empty the store. Nothing in production calls this — it exists so a test can start from
        a known-empty store, because a module-global that survives between tests is the classic
        way a suite starts proving something other than what it says."""
        self._parked.clear()

    def _expire(self, now: float) -> None:
        """Drop reports older than the TTL, and any app left with none.

        Run on both read and write rather than on a timer: this is a small structure with no
        background owner, and the cost is proportional to what is actually parked."""
        cutoff = now - REPORT_TTL_S
        for app_name in list(self._parked):
            fresh = [report for report in self._parked[app_name] if report.parked_at > cutoff]
            if fresh:
                self._parked[app_name] = fresh
            else:
                del self._parked[app_name]


_STORE = ClientErrorStore()


def park_client_error(app_name: str, *, source: str, title: str, stack: str) -> bool:
    """Park one browser-side crash report against an app; returns whether it was kept.

    A plain verb for a plain act. The evocative name for this class of failure — the dev server
    answered, the page was served, every server-side check comes back clean, and the app is dead
    anyway because the failure was inside the browser all along — belongs to
    `selfheal.the_call_is_coming_from_inside_the_house`, which turns these reports into the
    diagnostic. One name, one meaning: two functions in one subsystem sharing a name is one grep
    away from a reader believing the ingest and the verdict are the same call."""
    return _STORE.record(app_name, source=source, title=title, stack=stack)


def drain_client_errors(app_name: str) -> list[ClientErrorReport]:
    """Take (and forget) every fresh report parked for this app — see `ClientErrorStore.drain`."""
    return _STORE.drain(app_name)


def forget_all_client_errors() -> None:
    """Test-isolation seam — see `ClientErrorStore.forget_everything`."""
    _STORE.forget_everything()
