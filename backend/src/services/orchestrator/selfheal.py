"""The between-runs self-heal verify (KD-5 / KD-6 / KD-7 / KD-8).

After each `agent.iter` run the harness runs a CHEAP, harness-driven verify — `tsc --noEmit` over
the C2 command op and the `dev_logs` cursor tail — and decides the gate. Completion is an OBJECTIVE
green signal (`tsc` clean AND the dev server ready AND a clean log tail), never the model's word
alone (KD-6). `next build` is NOT run in Wave-1 (the production build is a DEPLOY concern, D2). A
slow-but-healthy dev server is distinguished from a stuck one by a bounded readiness poll before
any run is burned (open-Q F). A red signal becomes a redacted `BuildError` the loop re-seeds as the
next run's prompt (KD-5).

Every one of those signals is asked of the SERVER, and an app can satisfy all of them and still
throw in the browser before it paints. U13 adds the missing witness: the app's own error reporter
POSTs what it caught, `client_errors` parks it, and `verify` drains it here — so a reported
browser-side crash is a not-green verdict exactly like a failed type-check (R17, AE11). This is
the ONE authority both loops consult, which is why the runtime half lands here rather than at
either call site: `turns/engine.py` runs the live path and `harness.py` the vestigial one, and a
health rule that only one of them knew would be a health rule with an escape hatch.

U6 adds the two halves R9 asks for and a third VALUE. The halves: the app's own root is fetched
over HTTP (`what_is_it_serving`) so a 500 can no longer ship green past a readiness probe that
fail-opens on it, and `app/page.tsx` is compared against the repository's ROOT COMMIT so an app
still serving the golden template can no longer be called finished — the nine-minute false
"Build complete" of 2026-08-18. The value is `INDETERMINATE`: a readiness budget that ran out and
a serving probe that timed out are "we could not tell", not "it is broken", and feeding either to
the model as a defect spent a repair run on a fault that may never have existed. `verify` asks
again instead; only `HEALTHY` is green.

A DEAD dev child gets the IT Crowd treatment first — "have you tried turning it off and on
again?": verify captures the child's last output + exit code (the ring resets on restart), calls
`dev_start` once, and only then polls readiness. Before this rescue, nothing in the system ever
restarted a dead child — not the supervisor, not the harness, and the agent is forbidden to — so
one startup crash burned the whole self-heal budget re-prompting the agent to fix a rendering bug
that did not exist (the 2026-07-30 calculator build: 3 repair runs, ~875k tokens, dead process).
"""

from __future__ import annotations

import asyncio
import enum
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Final

import structlog

from src.api.v1.build_sessions.schemas import BuildError
from src.core.integrity_types import BaselineIdentity
from src.services.orchestrator.client_errors import ClientErrorReport, drain_client_errors
from src.services.orchestrator.constants import (
    EXEC_TIMEOUT_S,
    LOG_TAIL_MAX_LINES,
    TYPECHECK_CMD,
    VERIFY_INDETERMINATE_BACKOFF_S,
    VERIFY_INDETERMINATE_RETRIES,
    VERIFY_RETRY_BACKOFF_S,
    VERIFY_TRANSIENT_RETRIES,
)
from src.services.orchestrator.errors import from_client, from_server, from_tsc
from src.services.sandbox import (
    SandboxClient,
    SandboxError,
    SandboxGoneError,
    SandboxHandle,
    ServedPage,
)

logger = structlog.get_logger()

# Next.js dev-server markers that reliably indicate a server-side crash or a compile failure (a
# benign request log like "GET / 200" must not trip the gate).
_CRASH_MARKERS = (
    "⨯",
    "Failed to compile",
    "unhandledRejection",
    "Unhandled Runtime Error",
    "UnhandledPromiseRejection",
)

# The nudge that re-seeds a run that ended green but without declaring done — not an error, just
# "keep going" (it still consumes a self-heal budget so the loop stays bounded, KD-7).
CONTINUE_PROMPT = (
    "The app is not finished yet — you ended your turn without calling `declare_done`. Continue "
    "building the requested features, then call `declare_done` when the app is complete."
)

# The synthesized diagnostic for "tsc clean, no crash marker, but the dev server never became
# ready" — an app that throws on load, hangs at startup, or renders blank without printing a
# recognized crash marker. Without it the loop would misread this state as "green but not done"
# (KD-5/KD-6).
_DEV_NOT_READY_DETAIL = (
    "The dev server did not report ready within the readiness budget, and no type-check or "
    "compile error was found. The app most likely throws during render, hangs at startup, or "
    "renders a blank page without logging a recognized error. Inspect the runtime code under "
    "`app/` (and any component it renders) for an error thrown on load, and fix the root cause. "
    "The migrate step `npm run dev` runs first is non-fatal and cannot be the cause — a failed "
    "migration prints its reason and the app still starts."
)


def dev_not_ready_error() -> BuildError:
    """A synthesized SERVER `BuildError` for the 'tsc clean, no crash marker, but the dev server
    never became ready' state (KD-5). Without it the self-heal repair prompt would misfire as a
    'you forgot declare_done' nudge and a budget-exhausted escalation would be diagnostic-free."""
    return from_server(_DEV_NOT_READY_DETAIL)


# The dead child's last words included in the died diagnostic — bounded well under the
# redactor's LOG_TAIL_MAX_LINES so the death evidence never dominates the repair prompt.
_DEATH_TAIL_LINES = 40


def dev_died_error(
    *, exit_code: int | None, restarted: bool, last_output: list[str]
) -> BuildError:
    """The HONEST diagnostic for a dev child found dead at verify: name the process failure and
    its exit code instead of guessing at a rendering bug — the misattribution that sent the
    build agent on a 3-run wild-goose chase (2026-07-30). Routed through `from_server` so the
    dead child's last output is redacted like any other server tail."""
    exit_clause = f"exit code {exit_code}" if exit_code is not None else "exit code unknown"
    restart_clause = (
        "an automatic restart did not report ready within the readiness budget"
        if restarted
        else "an automatic restart attempt failed"
    )
    detail = (
        f"The dev server PROCESS was found dead ({exit_clause}) when the build was verified, "
        f"and {restart_clause}. This is a server-process failure, not necessarily a rendering "
        "bug: check the most recent changes for anything that runs at server startup or module "
        "load (a top-level throw, a bad import, an edited config file) and fix the root cause. "
        "Exit code 137 means the process was killed for memory — simplify what loads at startup."
    )
    if last_output:
        detail += "\n\nLast dev-server output before it died:\n" + "\n".join(last_output)
    return from_server(detail)


# U6 / R9 — THE SERVING HALF. The supervisor's readiness probe fail-opens on 4xx/5xx by explicit
# design (`_dev_port_serving`), and `someone_has_to_go_first`'s status is contractually
# non-load-bearing, so neither of them can carry this. An app whose root answers 500 has to be
# called broken by something, and this is the diagnostic that says so.
_SERVED_BADLY_DETAIL = (
    "The app's own home page answered with HTTP {status} when it was checked, so the app is not "
    "usable even though the dev server is up and the type-check is clean. A 5xx here is a server "
    "error thrown while rendering the root route — check the code that runs on the server for "
    "that page (data fetching, top-level awaits, anything reading the database or the "
    "environment) and fix the root cause. A 4xx means the root route does not resolve at all: "
    "make sure `app/page.tsx` exists and exports a default React component."
)


def served_badly_error(status: int) -> BuildError:
    """The diagnostic for "the dev server is up, the types are clean, and the app's own home page
    answers with an error" (U6, R9). Routed through `from_server` because the status came from the
    app's own route, which is sandbox output like any other."""
    return from_server(_SERVED_BADLY_DETAIL.format(status=status))


# U6 / R9 — THE CONTENT HALF. On 2026-08-18 "Build complete — your app is live below" sat above the
# untouched starter template for nine minutes. Every server-side signal was green, because every
# server-side signal answers "is a Next app running here" and none of them answers "is it THEIR
# app". This one does, and it is the diagnostic the model gets when the answer is no.
_STILL_THE_STARTER_PAGE_DETAIL = (
    "The app's home page (`app/page.tsx`) is still byte-for-byte the starter template the "
    "workspace was created with — nothing the user asked for is on the page they will actually "
    "look at. Whatever else was built, the root route has to become the user's app: write "
    "`app/page.tsx` so it renders what they asked for, or make it lead to the screens you built "
    "elsewhere. This is checked against the workspace's first commit, so editing any other file "
    "will not clear it."
)


def still_the_starter_page_error() -> BuildError:
    """The diagnostic for an app that responds perfectly and is still the golden template (U6,
    AE6). `from_server` for consistency with its siblings; nothing about it is app-authored, but
    routing every synthesized diagnostic through one door is what keeps the redaction contract
    from having exceptions."""
    return from_server(_STILL_THE_STARTER_PAGE_DETAIL)


NON_FATAL_CLIENT_SOURCES: Final = frozenset({"console.error", "console.warn"})
"""The reporter sources that mean the app SAID something, as opposed to actually broke.

A DENYLIST, NOT AN ALLOWLIST, and the direction is the whole point. `source` is a free-form label
the capture component chooses — `schemas.py` keeps it a bounded string rather than an enum
precisely so that the day the template learns a fifth capture point, the backend does not answer
422 and make the crash it was reporting invisible. An allowlist of FATAL sources would reintroduce
that failure by the back door and without the 422 to notice it by: a new reporter would land
outside the list, read as non-fatal, and a real crash would pass as green. Naming the two that are
NOT crashes fails closed instead — anything unrecognised is treated as a crash.

The capture component wraps four things: `window.onerror`, `unhandledrejection`, `console.error`
and `console.warn`. Only the first two are crashes. The other two are ordinary output — and React
logs its own development warnings (a missing `key`, a hydration mismatch, a deprecated lifecycle)
through `console.error`, not `console.warn`, so "console.error fired" is emphatically not a
synonym for "the app is broken".

GATING THE VERDICT ON ALL FOUR WOULD FAIL ALMOST EVERY BUILD. A single missing `key` prop would
make `verify` red, spend the whole self-heal budget chasing a warning, and — because the client
diagnostic is deliberately not narrated — do it with nothing on screen explaining why. That is a
worse lie than the one this feature exists to remove, so the verdict gates on crashes only.

The non-fatal reports are not thrown away when a crash IS present: they ride along as context in
the same diagnostic, because a warning emitted just before a crash is often the thing that
explains it. With no crash they are dropped, which is what "the app is noisy but working" means."""


def the_call_is_coming_from_inside_the_house(reports: list[ClientErrorReport]) -> BuildError:
    """The diagnostic for "every server-side check is clean and the app is still broken" (U13,
    R17 runtime half).

    Named for what these reports mean: the dev server answered, `tsc` is clean, the log tail is
    quiet, `/dev/status` says ready — and the app is dead anyway, because the failure was inside
    the browser the whole time. That class is invisible to every signal the harness polls, which
    is precisely why suppressing the framework's runtime overlay could otherwise turn a crash the
    user could SEE into a success nobody could see at all.

    The blob assembled here is app-authored text and is handled as such by `from_client`: redacted
    on the same single path as any other sandbox output, then wrapped in a data-only frame. The
    numbering matters more than it looks — a crash loop reports the same fault repeatedly, and an
    explicit `[1] … [2] …` is what stops the model reading a repeated stack as several distinct
    faults to chase."""
    # Crashes first, then whatever else the browser said. A warning logged moments before a crash
    # is frequently the thing that explains it, so the context is worth carrying — but it is
    # carried BEHIND the crash, because the crash is what the agent has to fix.
    ordered = [r for r in reports if r.source not in NON_FATAL_CLIENT_SOURCES] + [
        r for r in reports if r.source in NON_FATAL_CLIENT_SOURCES
    ]
    blocks = [
        f"[{index}] {report.source}: {report.title}\n{report.stack}".rstrip()
        for index, report in enumerate(ordered, start=1)
    ]
    return from_client("\n\n".join(blocks))


class HealthState(enum.StrEnum):
    """The harness's read of an app's health — THREE values, and the third is the point (U6, R10).

    This verdict was a boolean, and a boolean cannot tell "the app is broken" apart from "we could
    not find out". Every way of not finding out — a readiness poll whose budget ran out, a serving
    probe that timed out, a content check with no baseline to compare against — was folded into
    "red" and handed to the model as a defect to fix. It then spent a repair run, the user's
    tokens and the user's time on a fault that may never have existed, and three of the four repair
    cycles in the 2026-08-18 demo were exactly that.

    Naming discipline, inherited from `durable_copy.CopyState`: `INDETERMINATE` must not be
    skimmable as either of the other two. It is not a soft red and it is not a cautious green. It
    means ASK AGAIN — and, specifically, it never revokes a reveal already granted and never
    reaches a teardown, a restore or a reclaim."""

    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True)
class VerifyOutcome:
    """The harness's read of the app's health after a run."""

    # HEALTHY / UNHEALTHY / INDETERMINATE — see `HealthState`. The authority; `green` is derived.
    state: HealthState
    # The dev server reports ready (drives the preview_ready transition).
    dev_ready: bool
    # A redacted tsc/server diagnostic when red, else None. None on INDETERMINATE by construction:
    # a verdict that could not be reached has nothing to tell the model, and synthesizing something
    # would be handing it a misdiagnosis to chase.
    error: BuildError | None
    # The framable preview URL once the dev server is ready.
    preview_url: str | None
    # What the app's own root actually answered, and with what. RAW EVIDENCE kept beside the
    # derived verdict, per the 2026-08-02 learning where a derived metric produced a false
    # accusation that the raw field disproved in one step. `None` means the probe could not ask —
    # which is an INDETERMINATE input, never a broken app.
    served: ServedPage | None = None
    # Whether the app's root route is still byte-identical to the seeded baseline. `None` when the
    # check was not consulted at all — a brand-new app with no prior building turns is SUPPOSED to
    # be showing the template, so asking would only produce a false accusation.
    baseline: BaselineIdentity | None = None
    # WHICH check could not be answered, on an INDETERMINATE verdict — `None` on every other.
    #
    # THE THREE ARE NOT THE SAME KIND OF SILENCE, and collapsing them cost this a round of
    # review. A serving probe that never came back, or a baseline with no root commit to compare
    # against, describes an app that is up and answering: "we could not confirm this change went
    # in" is true of it. A READINESS budget that ran out describes an app that is not serving at
    # all — and once patience is spent, thirty seconds of not coming up, three times over, has
    # stopped being our impatience and become a fact about the app. Telling that citizen their
    # app "looks like it's running" would be a new false claim, in the plan whose entire purpose
    # is removing one.
    unanswered: Unanswered | None = None
    # The browser crash reports this pass consumed. Carried on the outcome so `verify` can hand
    # them to a later pass rather than letting a discarded one take them to the grave — see
    # `_verify_once`'s `carried_reports`.
    client_reports: tuple[ClientErrorReport, ...] = ()
    # U9 — does this red verdict rest on the DEV LOG, as opposed to something derived fresh in
    # this pass? Only log evidence can be older than the agent's last edit: a type-check, a
    # serving status and a baseline comparison are all produced during the pass that reads them,
    # while the log tail accumulates and a restart resets the ring underneath the cursor. False on
    # every green and every indeterminate verdict, so a caller cannot re-check its way past one.
    rests_on_log_evidence: bool = False

    @property
    def green(self) -> bool:
        """tsc clean AND dev ready AND a clean log tail AND no browser crash AND the app serves
        AND it is no longer the starter page (KD-6, R9).

        A PROPERTY rather than a field, for the reason `durable_copy.CopyVerdict.may_destroy`
        exists: `state is HealthState.HEALTHY` spelled out at every call site is a chance at each
        one to write `is not UNHEALTHY` instead — which would read an INDETERMINATE verdict as a
        completion claim, the exact class of lie this plan removes."""
        return self.state is HealthState.HEALTHY


def detect_server_crash(lines: list[str]) -> str | None:
    """Return the joined new log tail when it carries a crash/compile-failure marker, else None."""
    if any(marker in line for line in lines for marker in _CRASH_MARKERS):
        return "\n".join(lines)
    return None


async def _try_try_again[T](step: Callable[[], Awaitable[T]]) -> T:
    """Run one verify step, retrying a TRANSIENT `SandboxError` ("if at first you don't
    succeed…") up to `VERIFY_TRANSIENT_RETRIES` extra attempts with a short backoff — one
    supervisor blip must not escalate a healthy build to a hard FAILED. `SandboxGoneError`
    re-raises immediately (restore-needed, a retry cannot help) and `asyncio.CancelledError` is a
    `BaseException`, so a stop/idle cancel is never caught here. Exhausted → the last error
    propagates and escalates exactly as an unretried failure would."""
    attempts_left = VERIFY_TRANSIENT_RETRIES
    while True:
        try:
            return await step()
        except SandboxGoneError:
            raise  # terminal for the handle — the caller must restore, never retry
        except SandboxError:
            if attempts_left <= 0:
                raise
            attempts_left -= 1
            await asyncio.sleep(VERIFY_RETRY_BACKOFF_S)


_LATER_PASS_POLL_DIVISOR: Final = 3
"""How much of the readiness budget a SECOND look at the same app gets.

Not a tuning knob so much as a statement about what the second look is for: the first pass already
waited the full budget, so re-paying it wholesale is asking the same question again rather than a
smaller one. A third is enough to catch an app that came up moments after we stopped watching, and
small enough that patience cannot turn one 30-second verify into a minute and a half."""


class Unanswered(enum.StrEnum):
    """Which check came back with no answer. See `VerifyOutcome.unanswered` for why it matters
    which one — only `READINESS` becomes a defect once the patience budget is spent."""

    READINESS = "readiness"
    SERVING = "serving"
    BASELINE = "baseline"


class Readiness(enum.StrEnum):
    """How a bounded readiness poll ENDED, which is not the same question as whether it succeeded.

    The bool this replaces answered "is it ready" and threw away the difference between the two
    ways of answering no — and those two mean opposite things. A process that reported
    `running=False` is genuinely down: that is a defect, and the agent should hear about it. A
    poll budget that simply ran out over a process still reporting `running=True` is a slow
    startup we stopped waiting for: that is not evidence of anything, and calling it a defect
    spends a repair run on an app that may have come up two seconds later."""

    READY = "ready"
    DIED = "died"
    STILL_TRYING = "still_trying"


async def where_are_we(
    sandbox_client: SandboxClient, handle: SandboxHandle, *, max_polls: int, poll_s: float
) -> Readiness:
    """Poll `dev_status` until the dev server is `ready` (a slow-but-healthy startup), the process
    dies (`running=False` → not slow, genuinely down), or the poll budget is spent. Bounded, so a
    readiness wait never burns a repair run (open-Q F)."""
    for attempt in range(max_polls):
        status = await sandbox_client.dev_status(handle)
        if status.ready:
            return Readiness.READY
        if not status.running:
            return Readiness.DIED
        if attempt < max_polls - 1:
            await asyncio.sleep(poll_s)
    return Readiness.STILL_TRYING


async def verify(
    sandbox_client: SandboxClient,
    handle: SandboxHandle,
    *,
    log_cursor: int,
    max_polls: int,
    poll_s: float,
    app_id: uuid.UUID,
    had_prior_building_turns: bool,
    indeterminate_retries: int = VERIFY_INDETERMINATE_RETRIES,
    indeterminate_backoff_s: float = VERIFY_INDETERMINATE_BACKOFF_S,
) -> tuple[VerifyOutcome, int]:
    """The health verdict, asked with patience: run `_verify_once`, and when it comes back
    INDETERMINATE ask again rather than reporting a defect (U6, R10, AE8).

    THE RETRY LIVES HERE, not at either loop, and that is deliberate. `selfheal` is the ONE health
    authority both harnesses consult precisely so a verdict cannot mean two things depending on
    which loop built the app; a patience budget applied in `turns/engine.py` and forgotten in
    `harness.py` would be a health rule with an escape hatch. It is also why the retry cannot live
    inside `_verify_once`, which has to stay a single honest pass so a test can observe one.

    `log_cursor` is threaded through every attempt, so a retry reads only what is genuinely new
    and a crash printed during the first pass is not re-reported by the second — and the browser
    crash reports a discarded pass consumed are carried into the next one, or a pass that is
    thrown away would take a real crash with it.

    ONE CONVERSION HAPPENS AT EXHAUSTION and it is narrow: a readiness budget that has now run out
    several times over has stopped being our impatience and become a fact about the app, so it
    becomes the diagnosis this loop has always given for it. The other two unanswerable checks
    describe an app that IS serving and are returned as they are — inventing a startup fault for
    one of those is the misdiagnosis U6 exists to remove."""
    attempts_left = indeterminate_retries
    rechecked = False
    first_pass = True
    # Reports an earlier pass already took out of the store, so a discarded pass cannot carry a
    # browser crash out of the verdict with it.
    carried: tuple[ClientErrorReport, ...] = ()
    while True:
        outcome, log_cursor = await _verify_once(
            sandbox_client,
            handle,
            log_cursor=log_cursor,
            # A LATER PASS DOES NOT RE-PAY THE WHOLE READINESS BUDGET. The commonest reason to be
            # on one is that the budget just ran out, so spending it again in full turns a 30s
            # wait into 90 — measured at 93 `dev_status` calls for one `verify`. A later pass asks
            # "has it come up in the last few seconds", which is a far smaller question than the
            # first one asked, and `max(1, …)` keeps it a question rather than a formality.
            max_polls=max_polls if first_pass else max(1, max_polls // _LATER_PASS_POLL_DIVISOR),
            poll_s=poll_s,
            app_id=app_id,
            had_prior_building_turns=had_prior_building_turns,
            carried_reports=carried,
        )
        first_pass = False
        carried = outcome.client_reports
        # U9 / R15 — ONE AUTHORITATIVE RE-CHECK BEFORE A REPAIR ROUND-TRIP IS BOUGHT. Three of the
        # four repair cycles in the 2026-08-18 demo were the platform re-reporting errors it had
        # already fixed, and the mechanism is structural: `log_cursor` bounds the read by log
        # POSITION rather than by agent action, a dev-server restart resets the ring underneath it,
        # and a dead child's last words are deliberately carried forward. So a crash printed before
        # the agent's edit can be read after it and charged as a fresh defect.
        #
        # Gated on the EVIDENCE, not on the verdict, and that gate is what keeps this both cheap
        # and safe: a failed type-check, a 500 from the root route and a baseline comparison are
        # all produced during the pass that reads them and cannot be stale, so they never buy a
        # second pass — and a dead child's last words, which nothing re-emits, are excluded for
        # the opposite reason (a second look at them would read an empty window and call the
        # death fixed).
        #
        # `changed is True`, never a truthiness test: `None` means the container could not answer,
        # and the honest response to that is today's behaviour rather than a re-check we cannot
        # justify. Once per call, so a container that keeps changing cannot loop this.
        if (
            not rechecked
            and outcome.state is HealthState.UNHEALTHY
            and outcome.rests_on_log_evidence
        ):
            rechecked = True
            if await _has_the_agent_touched_anything(sandbox_client, handle) is True:
                logger.info(
                    "verify_rechecking_stale_log_evidence",
                    app=handle.app_name,
                    app_id=str(app_id),
                )
                continue
        if outcome.state is not HealthState.INDETERMINATE:
            return outcome, log_cursor
        if attempts_left <= 0:
            # PATIENCE SPENT. See the docstring: only the readiness arm converts, and only here.
            if outcome.unanswered is Unanswered.READINESS:
                return replace(
                    outcome, state=HealthState.UNHEALTHY, error=dev_not_ready_error()
                ), log_cursor
            return outcome, log_cursor
        attempts_left -= 1
        logger.info(
            "verify_indeterminate_retrying",
            app=handle.app_name,
            app_id=str(app_id),
            attempts_left=attempts_left,
            unanswered=outcome.unanswered,
            served_status=outcome.served.status if outcome.served else None,
            baseline=outcome.baseline,
        )
        await asyncio.sleep(indeterminate_backoff_s)


async def _has_the_agent_touched_anything(
    sandbox_client: SandboxClient, handle: SandboxHandle
) -> bool | None:
    """`integrity.anything_changed_since_the_watermark`, through a function-scoped import for the
    package cycle documented on `_ask_the_container_what_it_is_showing` below.

    `None` when the container could not answer — never folded into `False`, because the caller
    reads it as "change nothing" and a container that cannot answer should cost the improvement,
    not the correctness."""
    from src.services.build_sessions.integrity import anything_changed_since_the_watermark

    return await anything_changed_since_the_watermark(sandbox_client, handle)


async def _ask_the_container_what_it_is_showing(
    sandbox_client: SandboxClient, handle: SandboxHandle
) -> BaselineIdentity:
    """`integrity.baseline_identity`, reached through a function-scoped import.

    THE IMPORT IS IN HERE BECAUSE THE PACKAGES ARE CIRCULAR, and the cycle is real rather than
    incidental: `src.services.build_sessions.__init__` reaches `appdata` → `services.projects` →
    `agent.agent` → `services.orchestrator.__init__` → this module. A module-level import here
    therefore fails at interpreter start, not at call time. It is the ONE direction that is
    circular: `build_sessions` may import from here-adjacent leaves freely, and does — `manager`
    and `reaper` both import `integrity` at module level.

    The type comes from `integrity_types`, a leaf module with no imports of its own, so the
    signature is honest at import time and only the CALL is deferred."""
    from src.services.build_sessions.integrity import baseline_identity

    return await baseline_identity(sandbox_client, handle)


async def _verify_once(
    sandbox_client: SandboxClient,
    handle: SandboxHandle,
    *,
    log_cursor: int,
    max_polls: int,
    poll_s: float,
    app_id: uuid.UUID,
    had_prior_building_turns: bool,
    carried_reports: tuple[ClientErrorReport, ...] = (),
) -> tuple[VerifyOutcome, int]:
    """One pass of the cheap harness verify, returning `(outcome, new_log_cursor)`. Reads only the
    NEW dev logs since `log_cursor` so a crash from an earlier run is never re-reported.

    `carried_reports` are the browser crash reports an EARLIER pass of the same `verify` call
    already drained out of the store. A tuple, so the shared default cannot be mutated by anything
    and the usual mutable-default hazard does not arise."""
    run_command = sandbox_client.exec  # aliased to keep the call off the JS-oriented exec guard
    # Every sandbox hop gets the bounded transient-retry (`_try_try_again`): a blip here would
    # otherwise escalate the whole build as a hard internal_error.
    typecheck = await _try_try_again(
        lambda: run_command(handle, list(TYPECHECK_CMD), timeout_s=EXEC_TIMEOUT_S)
    )
    tsc_ok = typecheck.exit == 0

    # The dead-child rescue: `running=False` with nothing serving the port means the child is
    # genuinely down — restart it rather than diagnose it. Order matters: the last output and
    # exit code are captured BEFORE `dev_start`, because a successful start resets the C1 log
    # ring (and with it the cursor space).
    status = await _try_try_again(lambda: sandbox_client.dev_status(handle))
    dev_died = not status.running and not status.ready
    died_lines: list[str] = []
    restarted = False
    if dev_died:
        death_logs = await _try_try_again(
            lambda: sandbox_client.dev_logs(handle, since=log_cursor)
        )
        died_lines = death_logs.lines
        log_cursor = death_logs.next_cursor
        try:
            await _try_try_again(lambda: sandbox_client.dev_start(handle))
        except SandboxGoneError:
            raise  # terminal for the handle — the caller must restore, never retry
        except SandboxError:
            logger.warning(
                "dev_server_dead_restart_failed", exit_code=status.exit_code, app=handle.app_name
            )
        else:
            restarted = True
            # The restart reset the supervisor's log ring — old cursors point past it.
            log_cursor = 0
            logger.warning(
                "dev_server_dead_restarted", exit_code=status.exit_code, app=handle.app_name
            )

    readiness = await _try_try_again(
        lambda: where_are_we(sandbox_client, handle, max_polls=max_polls, poll_s=poll_s)
    )
    dev_ready = readiness is Readiness.READY

    # MAKE THE ERROR EXIST BEFORE WE GO LOOKING FOR IT (U4, R4). A whole class of Next compile
    # errors — a Server Component reaching for a client-only hook is the canonical one — passes
    # `tsc --noEmit` cleanly, writes NOTHING to `/dev/logs`, and leaves `/dev/status` reporting
    # ready. Every check above says green, and the citizen gets a blank page. Next only emits its
    # `⨯` diagnostic when the route is actually REQUESTED, so the request has to happen here:
    # after readiness (there is nothing to ask before that) and before the log read (or the
    # diagnostic lands outside the window `detect_server_crash` scans). One call, positioned;
    # `⨯` is already `_CRASH_MARKERS[0]` and the rest of the chain needs no change at all.
    #
    # It cannot fail this step — the helper swallows everything and returns a status nobody here
    # reads. When it cannot reach the route, no lines arrive and verify behaves exactly as before.
    #
    # Gated on `dev_ready` because "after readiness" is a precondition, not just an ordering: a
    # server that never came up has nothing to answer with, so asking spends the helper's whole
    # budget to learn what the poll above already established — up to three times per build, on
    # exactly the red path where the user is already waiting longest. The case U4 exists for is
    # the opposite one: ready is TRUE, `tsc` is clean, and the page is still blank.
    served: ServedPage | None = None
    baseline: BaselineIdentity | None = None
    if dev_ready:
        # ONE request does both jobs. It is the same GET at the same URL `someone_has_to_go_first`
        # used to make from here — so the route still gets requested and Next still emits its `⨯`
        # into the log before the read below — but this one keeps a bounded head of the answer,
        # and the status is now LOAD-BEARING rather than merely logged.
        #
        # THE PROMOTION IS THE POINT, and the note it replaces said what it would cost: the old
        # comment recorded that a root route answering 500 shipped green because the verdict was
        # five hard-coded text markers against the dev log, and left promoting it as "a
        # behavioural decision for the owner". R9 is that decision, made. It also explains why the
        # call could not simply stay as it was: `someone_has_to_go_first` is contractually
        # non-load-bearing (R6) and its docstring says no caller may make a decision on what it
        # returns, so reading a verdict off it would have converted a promise into a lie.
        served = await sandbox_client.what_is_it_serving(handle)
        if served is None:
            logger.warning("verify_serving_probe_unanswered", app=handle.app_name, tsc_ok=tsc_ok)
        elif not (200 <= served.status < 400):
            logger.warning(
                "verify_root_route_answered_badly",
                status=served.status,
                app=handle.app_name,
                tsc_ok=tsc_ok,
            )
        # THE CONTENT HALF, and it is asked only of an app that has been built before. A brand-new
        # project is SUPPOSED to be showing the starter template, so asking would manufacture an
        # accusation; `had_prior_building_turns` is resolved by the caller from the durable fact
        # the attach path already holds (see `integrity.has_ever_been_built`).
        if had_prior_building_turns:
            baseline = await _ask_the_container_what_it_is_showing(sandbox_client, handle)

    logs = await _try_try_again(lambda: sandbox_client.dev_logs(handle, since=log_cursor))
    # Bound the tail fed to crash detection + redaction: a single unbounded dev-log blob must not
    # reach the (linear-but-synchronous) redactor unbounded (LOG_TAIL_MAX_LINES, KD-10). The dead
    # child's captured lines stay in the window — a crash marker in its last words is the true
    # diagnostic even when the restarted child comes up clean (KD-6: the tail must be clean).
    tail = (died_lines + logs.lines)[-LOG_TAIL_MAX_LINES:]
    server_crash = detect_server_crash(tail)

    # U13 / R17 — THE RUNTIME HALF OF THE HEALTH VERDICT. Everything above this line is asked of
    # the SERVER; a Next app can pass all of it and still throw before it paints a single pixel,
    # and the only witness to that is the browser. The app's own error reporter has been relaying
    # those crashes to the framing portal since Stage 0 with nobody listening; the ingest route
    # parks what the portal forwards, and this is where it is collected.
    #
    # DRAINED unconditionally, including on the arms below where a compile error already outranks
    # it. A report counts against exactly one verdict: left parked, one browser crash would fail
    # every remaining verify of the build and burn the whole self-heal budget re-reporting itself
    # while the agent fixed it on the first pass. If the crash is still there after the repair,
    # the browser is still framing the app and says so again.
    # CARRIED, not merely drained. `drain_client_errors` is destructive and `verify` may run this
    # more than once — for its INDETERMINATE patience and for U9's re-check — so a report consumed
    # by a pass that is then discarded is gone from the pass that actually decides. That is a
    # browser crash flipping the verdict from UNHEALTHY to HEALTHY between two looks at the same
    # app: the false green this plan exists to remove, reintroduced by the fix for a different
    # one. "A report counts against exactly one verdict" is a statement about `verify`'s ANSWER,
    # never about each attempt at it.
    client_reports = [*carried_reports, *drain_client_errors(handle.app_name)]
    # Only a CRASH gates the verdict — see `NON_FATAL_CLIENT_SOURCES`. Both lists are kept: the
    # fatal ones decide, the full set is what the agent gets to read when they decide red.
    fatal_reports = [r for r in client_reports if r.source not in NON_FATAL_CLIENT_SOURCES]

    # ONE ORDERED CHAIN, and the order is the diagnosis. Each arm answers "what is the most
    # upstream thing that is wrong", so the model is handed the cause rather than a symptom of it:
    # a build that does not compile is not a build whose rendered output is worth arguing about.
    #
    # Only the arms that state a DEFECT set `error`. `INDETERMINATE` deliberately carries none —
    # a verdict we could not reach has nothing to tell the model, and synthesizing something for
    # it would hand it a misdiagnosis to chase, which is precisely the failure this state exists
    # to end. `VerifyOutcome`'s field docstring says the same thing from the other side.
    error: BuildError | None = None
    state = HealthState.HEALTHY
    rests_on_log_evidence = False
    unanswered: Unanswered | None = None
    if not tsc_ok:
        state = HealthState.UNHEALTHY
        error = from_tsc(f"{typecheck.stdout}\n{typecheck.stderr}")
    elif server_crash is not None:
        state = HealthState.UNHEALTHY
        error = from_server(server_crash)
        # RE-CHECKABLE ONLY WHILE THE APP IS ANSWERING. Next re-emits its diagnostic on every
        # request, so a second pass that requests the route either reproduces the marker or
        # proves it stale. Against a server that is not serving there is nothing to ask, and a
        # clean empty window would mean "we did not look", not "it is fixed".
        rests_on_log_evidence = dev_ready
    elif dev_died and not dev_ready:
        # NOT re-checkable, and the distinction is the whole safety of U9. A crash MARKER is
        # re-produced by requesting the route again, so a second pass can tell a stale one from a
        # current one. A dead child's last words cannot be: nothing re-emits them, so a re-check
        # would read an empty window and call the death fixed.
        state = HealthState.UNHEALTHY
        error = dev_died_error(
            exit_code=status.exit_code,
            restarted=restarted,
            last_output=died_lines[-_DEATH_TAIL_LINES:],
        )
    elif fatal_reports:
        # BEFORE the readiness and serving arms on purpose. A browser crash report is a POSITIVE
        # observation of a broken app; everything below is an absence of one. A build that does
        # not compile is not a build whose runtime is worth diagnosing — the browser is reporting
        # on whatever was last served, which is a different tree from the one the agent just wrote
        # — so this stays below the compile signals and above the "we could not tell" arms.
        state = HealthState.UNHEALTHY
        error = the_call_is_coming_from_inside_the_house(client_reports)
    elif readiness is Readiness.STILL_TRYING:
        # THE POLL BUDGET RAN OUT OVER A PROCESS STILL REPORTING `running`. Not a defect — we
        # stopped waiting, the app did not stop starting. This was red before U6 and cost a
        # repair run, a diagnostic the agent could not act on, and the user's tokens (AE8).
        state = HealthState.INDETERMINATE
        unanswered = Unanswered.READINESS
    elif not dev_ready:
        # Not ready, not still-trying and not caught by the died arm above: the process is down
        # and the restart did not take. A real defect with nothing else to say about it.
        state = HealthState.UNHEALTHY
        error = dev_not_ready_error()
    elif served is None:
        # THE APP MAY WELL BE SERVING; OUR REQUEST DID NOT COME BACK. Calling that broken is how
        # a working app gets told it did not come together, so it is a re-check, not a verdict.
        state = HealthState.INDETERMINATE
        unanswered = Unanswered.SERVING
    elif not (200 <= served.status < 400):
        # R9's serving half. A 3xx counts as serving: the route compiled and answered, which is
        # the whole question — an agent that replaced the root with a redirect built something.
        state = HealthState.UNHEALTHY
        error = served_badly_error(served.status)
    elif baseline is BaselineIdentity.UNANSWERABLE:
        # No root commit, more than one, or a baseline the repository never held. Never UNHEALTHY
        # and never HEALTHY: an app cannot be convicted of showing the template by a check that
        # could not find the template, and it cannot be cleared by one either.
        state = HealthState.INDETERMINATE
        unanswered = Unanswered.BASELINE
    elif baseline is BaselineIdentity.STILL_THE_BASELINE:
        # R9's content half, and the 2026-08-18 headline. Every server-side check above came back
        # clean and the citizen is looking at the golden template.
        state = HealthState.UNHEALTHY
        error = still_the_starter_page_error()

    logger.info(
        "verify_verdict",
        app=handle.app_name,
        app_id=str(app_id),
        state=state,
        tsc_ok=tsc_ok,
        dev_ready=dev_ready,
        readiness=readiness,
        server_crash=server_crash is not None,
        fatal_client_reports=len(fatal_reports),
        served_status=served.status if served else None,
        baseline=baseline,
        # THE RAW EVIDENCE BESIDE THE DERIVED VERDICT (the 2026-08-02 learning). Structlog is its
        # home until the counters table lands with the operational surface; a reader asking "but
        # what was it actually serving?" must not have to reproduce the run to find out.
        served_head=served.head if served else None,
    )
    preview_url = handle.preview_url if dev_ready else None
    return VerifyOutcome(
        state=state,
        dev_ready=dev_ready,
        error=error,
        preview_url=preview_url,
        served=served,
        baseline=baseline,
        rests_on_log_evidence=rests_on_log_evidence,
        client_reports=tuple(client_reports),
        unanswered=unanswered,
    ), logs.next_cursor
