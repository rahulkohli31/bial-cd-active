"""The between-runs self-heal verify (KD-5 / KD-6 / KD-7 / KD-8).

After each `agent.iter` run the harness runs a CHEAP, harness-driven verify — `tsc --noEmit` over
the C2 command op and the `dev_logs` cursor tail — and decides the gate. Completion is an OBJECTIVE
green signal (`tsc` clean AND the dev server ready AND a clean log tail), never the model's word
alone (KD-6). `next build` is NOT run in Wave-1 (the production build is a DEPLOY concern, D2). A
slow-but-healthy dev server is distinguished from a stuck one by a bounded readiness poll before
any run is burned (open-Q F). A red signal becomes a redacted `BuildError` the loop re-seeds as the
next run's prompt (KD-5).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from src.api.v1.build_sessions.schemas import BuildError
from src.services.orchestrator.constants import TYPECHECK_CMD
from src.services.orchestrator.errors import from_server, from_tsc
from src.services.sandbox import SandboxClient, SandboxHandle

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
    typecheck = await run_command(handle, list(TYPECHECK_CMD))
    tsc_ok = typecheck.exit == 0

    dev_ready = await are_we_there_yet(sandbox_client, handle, max_polls=max_polls, poll_s=poll_s)

    logs = await sandbox_client.dev_logs(handle, since=log_cursor)
    server_crash = detect_server_crash(logs.lines)

    error: BuildError | None = None
    if not tsc_ok:
        error = from_tsc(f"{typecheck.stdout}\n{typecheck.stderr}")
    elif server_crash is not None:
        error = from_server(server_crash)

    green = tsc_ok and dev_ready and server_crash is None
    preview_url = handle.preview_url if dev_ready else None
    return VerifyOutcome(
        green=green, dev_ready=dev_ready, error=error, preview_url=preview_url
    ), logs.next_cursor
