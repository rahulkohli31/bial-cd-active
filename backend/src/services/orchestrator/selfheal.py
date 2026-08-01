"""The between-runs self-heal verify (KD-5 / KD-6 / KD-7 / KD-8).

After each `agent.iter` run the harness runs a CHEAP, harness-driven verify — `tsc --noEmit` over
the C2 command op and the `dev_logs` cursor tail — and decides the gate. Completion is an OBJECTIVE
green signal (`tsc` clean AND the dev server ready AND a clean log tail), never the model's word
alone (KD-6). `next build` is NOT run in Wave-1 (the production build is a DEPLOY concern, D2). A
slow-but-healthy dev server is distinguished from a stuck one by a bounded readiness poll before
any run is burned (open-Q F). A red signal becomes a redacted `BuildError` the loop re-seeds as the
next run's prompt (KD-5).

A DEAD dev child gets the IT Crowd treatment first — "have you tried turning it off and on
again?": verify captures the child's last output + exit code (the ring resets on restart), calls
`dev_start` once, and only then polls readiness. Before this rescue, nothing in the system ever
restarted a dead child — not the supervisor, not the harness, and the agent is forbidden to — so
one startup crash burned the whole self-heal budget re-prompting the agent to fix a rendering bug
that did not exist (the 2026-07-30 calculator build: 3 repair runs, ~875k tokens, dead process).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import structlog

from src.api.v1.build_sessions.schemas import BuildError
from src.services.orchestrator.constants import (
    EXEC_TIMEOUT_S,
    LOG_TAIL_MAX_LINES,
    TYPECHECK_CMD,
    VERIFY_RETRY_BACKOFF_S,
    VERIFY_TRANSIENT_RETRIES,
)
from src.services.orchestrator.errors import from_server, from_tsc
from src.services.sandbox import SandboxClient, SandboxError, SandboxGoneError, SandboxHandle

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


@dataclass(frozen=True)
class VerifyOutcome:
    """The harness's read of the app's health after a run."""

    # tsc clean AND dev ready AND a clean log tail — the objective health signal (KD-6).
    green: bool
    # The dev server reports ready (drives the preview_ready transition).
    dev_ready: bool
    # A redacted tsc/server diagnostic when red, else None.
    error: BuildError | None
    # The framable preview URL once the dev server is ready.
    preview_url: str | None


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


async def are_we_there_yet(
    sandbox_client: SandboxClient, handle: SandboxHandle, *, max_polls: int, poll_s: float
) -> bool:
    """Poll `dev_status` until the dev server is `ready` (a slow-but-healthy startup), the process
    dies (`running=False` → not slow, genuinely down), or the poll budget is spent. Bounded, so a
    readiness wait never burns a repair run (open-Q F)."""
    for attempt in range(max_polls):
        status = await sandbox_client.dev_status(handle)
        if status.ready:
            return True
        if not status.running:
            return False
        if attempt < max_polls - 1:
            await asyncio.sleep(poll_s)
    return False


async def verify(
    sandbox_client: SandboxClient,
    handle: SandboxHandle,
    *,
    log_cursor: int,
    max_polls: int,
    poll_s: float,
) -> tuple[VerifyOutcome, int]:
    """Run the cheap harness verify and return `(outcome, new_log_cursor)`. Reads only the NEW dev
    logs since `log_cursor` so a crash from an earlier run is never re-reported."""
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

    dev_ready = await _try_try_again(
        lambda: are_we_there_yet(sandbox_client, handle, max_polls=max_polls, poll_s=poll_s)
    )

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
    await sandbox_client.someone_has_to_go_first(handle)

    logs = await _try_try_again(lambda: sandbox_client.dev_logs(handle, since=log_cursor))
    # Bound the tail fed to crash detection + redaction: a single unbounded dev-log blob must not
    # reach the (linear-but-synchronous) redactor unbounded (LOG_TAIL_MAX_LINES, KD-10). The dead
    # child's captured lines stay in the window — a crash marker in its last words is the true
    # diagnostic even when the restarted child comes up clean (KD-6: the tail must be clean).
    tail = (died_lines + logs.lines)[-LOG_TAIL_MAX_LINES:]
    server_crash = detect_server_crash(tail)

    error: BuildError | None = None
    if not tsc_ok:
        error = from_tsc(f"{typecheck.stdout}\n{typecheck.stderr}")
    elif server_crash is not None:
        error = from_server(server_crash)
    elif dev_died and not dev_ready:
        error = dev_died_error(
            exit_code=status.exit_code,
            restarted=restarted,
            last_output=died_lines[-_DEATH_TAIL_LINES:],
        )

    green = tsc_ok and dev_ready and server_crash is None
    preview_url = handle.preview_url if dev_ready else None
    return VerifyOutcome(
        green=green, dev_ready=dev_ready, error=error, preview_url=preview_url
    ), logs.next_cursor
