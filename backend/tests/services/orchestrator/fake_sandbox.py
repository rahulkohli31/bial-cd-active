"""`FakeSandbox` — an in-memory `SandboxClient` honoring C2-OBSERVABLE semantics (U4).

This is a C2 ABC double, NOT a C1 HTTP fake: C1's 400-vs-422 status split lives BELOW BRAIN's
seam and is collapsed by C2 into an opaque `SandboxError` (open-Q E), so the fake raises a bare
`SandboxError` with no status attribute — exactly what BRAIN sees. It backs the workspace with a
flat dict, LF-normalizes writes, enforces the `str_replace` exactly-once rule (0 or N>1 → error),
returns a non-zero command `exit` as a NORMAL `ExecResult` (never an exception), keeps `dev_start`
idempotent, and tails `dev_logs` from a cursor.

Programmable hooks let a test drive the self-heal loop: `queue_commands` scripts the harness-driven
`tsc` results (fail then pass), `become_ready_after` delays dev readiness, `push_dev_logs` injects
a crash into the tail, and `attach_error` makes `attach_existing` raise. It also records
`command_calls` / `dev_start_calls` / `teardown_calls` so a test can assert BRAIN never ran `git`,
never restarted the dev server, and never tore down (KD-9/KD-11).

Mirrors `tests/fakes.py:FakeStorage`.
"""

from __future__ import annotations

from collections import deque
from typing import assert_never

from src.services.sandbox import (
    DevLogs,
    DevStatus,
    ExecResult,
    FileCreate,
    FileInsert,
    FileOp,
    FileResult,
    FileStrReplace,
    FileView,
    SandboxClient,
    SandboxError,
    SandboxHandle,
    SandboxNotReadyError,
    ServedPage,
)

# U6's baseline-identity probe, matched on a fragment of the real script rather than the whole of
# it: the script is a private constant whose wording may change, and a fake that matched all of it
# would go quietly inert the first time it did — answering the generic empty result, which parses
# as UNANSWERABLE.
_BASELINE_MARKER = "git rev-list --max-parents=0"

# U9's watermark, matched the same way and for the same reason.
_STAMP_MARKER = "touch /tmp/.bial-agent-watermark"  # noqa: S108 - a marker path, not a temp file
_CHANGED_MARKER = "-newer /tmp/.bial-agent-watermark"  # noqa: S108 - same

BASELINE_ROOT_SHA = "0" * 40
BASELINE_TEMPLATE_BLOB = "1" * 40
BASELINE_DIVERGED_STDOUT = f"{BASELINE_ROOT_SHA}@@{BASELINE_TEMPLATE_BLOB}@@{'2' * 40}"
"""A BUILT app: one root commit, and a root route the agent has since rewritten."""
BASELINE_UNTOUCHED_STDOUT = (
    f"{BASELINE_ROOT_SHA}@@{BASELINE_TEMPLATE_BLOB}@@{BASELINE_TEMPLATE_BLOB}"
)
"""THE 2026-08-18 SHAPE: every server-side check green, and `app/page.tsx` byte-identical to the
golden template the workspace was born with."""


# A recognizable secret so a "no secret leak" test can assert it never surfaces in a tool result
# or an error message (KD-9). Not a real credential — a test double.
FAKE_SUPERVISOR_TOKEN = "tok_supervisor_SECRET_never_leak_me"  # noqa: S105


def _lf(text: str) -> str:
    """LF-normalize like the C1 supervisor does before it touches a file."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


class FakeSandbox(SandboxClient):
    """An in-memory `SandboxClient` for BRAIN's parallel-track tests."""

    def __init__(
        self,
        *,
        app_name: str = "vip-tracker",
        fqdn: str = "app-xyz.westeurope.azurecontainerapps.io",
        seed_files: dict[str, str] | None = None,
    ) -> None:
        self.workspace: dict[str, str] = {
            path: _lf(text) for path, text in (seed_files or {}).items()
        }
        self._handle = SandboxHandle(
            fqdn=fqdn,
            token=FAKE_SUPERVISOR_TOKEN,
            app_name=app_name,
            preview_url=f"https://{fqdn}/",
            ready=False,
        )
        # dev-server state
        self.dev_running = False
        self.dev_ready = False
        self.dev_exit_code: int | None = None  # the dead child's post-mortem (see kill_dev)
        self._ready_countdown: int | None = None
        self._dev_log_lines: list[str] = []
        # programmable hooks
        self._command_queue: deque[ExecResult] = deque()
        self.default_result = ExecResult(stdout="", stderr="", exit=0)
        self.attach_error: SandboxError | None = None
        self.files_error: SandboxError | None = None  # raised by every files() op when set
        self.dev_start_error: SandboxError | None = None  # raised by every dev_start when set
        self._exec_error_queue: deque[SandboxError] = deque()
        self._attach_error_queue: deque[SandboxError] = deque()
        # The U3 warm request, and the thing that makes U4 possible: a Next compile error does
        # not EXIST in `/dev/logs` until somebody actually requests the route. `tsc` passes, the
        # log is empty, `/dev/status` says ready — and the build ships green over a blank page.
        # `warm_emits_lines` is how a test reproduces that ordering instead of asserting it.
        self.warm_status: int | None = 200
        self.warm_emits_lines: list[str] = []
        # U6/R9 — what the app's own root answers when the HEALTH VERDICT asks. The status is
        # `warm_status`, shared deliberately: production makes ONE request for both jobs, and two
        # independently scriptable statuses would let a test build a container that answers 200 to
        # one caller and 500 to the other, which no real app can do.
        self.served_head = "<!DOCTYPE html><html><body>an app</body></html>"
        self.serving_calls = 0
        # U6's baseline-identity probe. The default is a BUILT app — one root commit and a root
        # route the agent has since rewritten — because the empty default parses as UNANSWERABLE,
        # which would put every test that turns the content check on into the retry path.
        self.baseline_stdout = BASELINE_DIVERGED_STDOUT
        # U9 — has anything in the workspace been written since the watermark was stamped? The
        # DEFAULT IS FALSE, which is what keeps the stale-evidence re-check inert for every test
        # that predates it: `find` printing nothing is "nothing changed", so no test written
        # before U9 silently starts paying for a second verify pass.
        self.changed_since_watermark = False
        self.watermark_stamps = 0
        # call records (assertions)
        self.command_calls: list[list[str]] = []
        self.command_timeouts: list[int] = []  # the timeout_s each exec was invoked with
        self.attach_calls = 0
        self.dev_start_calls = 0
        self.teardown_calls = 0
        self.warm_calls = 0

    # --- programmable hooks --------------------------------------------------

    def queue_commands(self, *results: ExecResult) -> None:
        """Script the FIFO results the harness-driven `tsc` reads (fail then pass)."""
        self._command_queue.extend(results)

    def become_ready_after(self, polls: int) -> None:
        """The dev server reports `ready=False` for the next `polls` `dev_status` calls, then
        flips ready (models a slow-but-healthy startup — open-Q F)."""
        self._ready_countdown = polls

    def queue_exec_errors(self, *errors: SandboxError) -> None:
        """Script transient infra failures: each queued error is raised by ONE `exec` call (the
        attempt is still recorded), then exec returns to normal — a supervisor blip, distinct
        from a queued non-zero exit (which is a NORMAL return, never an exception)."""
        self._exec_error_queue.extend(errors)

    def queue_attach_errors(self, *errors: SandboxError) -> None:
        """Script cold-start attach failures: each queued error is raised by ONE
        `attach_existing` call, then attach succeeds (models cold-ACA ingress waking up).
        `attach_error` stays the every-call persistent variant."""
        self._attach_error_queue.extend(errors)

    def compile_error_appears_on_first_request(self, *lines: str) -> None:
        """The failure shape `tsc --noEmit` cannot see: a Server Component calling a client-only
        hook. It typechecks, the dev log stays empty, and readiness holds — right up until a
        request is made, at which point Next writes its `⨯` diagnostic. Script that here."""
        self.warm_emits_lines.extend(lines)
        self.warm_status = 500

    def push_dev_logs(self, *lines: str) -> None:
        """Append lines to the dev-server tail (a crash line is just stderr text the harness
        recognizes)."""
        self._dev_log_lines.extend(lines)

    def kill_dev(self, *, exit_code: int = 1) -> None:
        """Model the dev child dying (an OOM kill, a startup crash): `running` False,
        marker-`ready` False, and the exit code surfaced by `dev_status`. Lines already in the
        ring stay readable — C1 keeps a dead child's output until a restart resets the ring."""
        self.dev_running = False
        self.dev_ready = False
        self.dev_exit_code = exit_code

    def handle(self) -> SandboxHandle:
        """The current handle snapshot (for tests that need it directly)."""
        return self._handle

    # --- C2 lifecycle --------------------------------------------------------

    async def provision_new(
        self, user_id: str, app_name: str, *, app_env: dict[str, str]
    ) -> SandboxHandle:
        self._handle = SandboxHandle(
            fqdn=self._handle.fqdn,
            token=FAKE_SUPERVISOR_TOKEN,
            app_name=app_name,
            preview_url=self._handle.preview_url,
            ready=False,
        )
        return self._handle

    async def wait_ready(
        self, handle: SandboxHandle, *, timeout_s: float = 120.0
    ) -> SandboxHandle:
        if not self.dev_ready:
            raise SandboxNotReadyError("dev server not ready")
        return self._ready_handle()

    async def attach_existing(self, user_id: str) -> SandboxHandle:
        self.attach_calls += 1
        if self._attach_error_queue:
            raise self._attach_error_queue.popleft()
        if self.attach_error is not None:
            raise self.attach_error
        # readiness reflects the current dev state (a resumed, already-ready sandbox → ready=True).
        return SandboxHandle(
            fqdn=self._handle.fqdn,
            token=self._handle.token,
            app_name=self._handle.app_name,
            preview_url=self._handle.preview_url,
            ready=self.dev_ready,
        )

    async def restore_from_snapshot(
        self,
        user_id: str,
        app_name: str,
        *,
        app_env: dict[str, str],
        source_key: str | None = None,
    ) -> SandboxHandle:
        return await self.provision_new(user_id, app_name, app_env=app_env)

    # --- C2 command / files --------------------------------------------------

    async def exec(
        self,
        handle: SandboxHandle,
        cmd: list[str],
        *,
        cwd: str | None = None,
        timeout_s: int = 900,
    ) -> ExecResult:
        # A non-zero exit is a NORMAL return (C1), never an exception.
        self.command_calls.append(list(cmd))
        self.command_timeouts.append(timeout_s)
        # THE HARNESS'S OWN `sh -c` PROBES ANSWER FROM THEIR OWN FIELDS, ahead of both queues.
        # `queue_commands` and `queue_exec_errors` script the harness-driven `tsc` run — that is
        # what every caller means by them — and they are FIFO, so a probe consuming a queued entry
        # would hand the type-check the wrong answer while looking like it did nothing. A test that
        # wants a probe to fail says so with `exec_handler`, which still sees everything.
        scripted = self._answer_a_probe(cmd)
        if scripted is not None:
            return scripted
        if self._exec_error_queue:  # a scripted transient infra failure (never a non-zero exit)
            raise self._exec_error_queue.popleft()
        if self._command_queue:
            return self._command_queue.popleft()
        return self.default_result

    def _answer_a_probe(self, cmd: list[str]) -> ExecResult | None:
        """One of the harness's own container probes, or `None` for an ordinary command."""
        if len(cmd) != 3 or cmd[0] != "sh":
            return None
        script = cmd[2]
        if _STAMP_MARKER in script:  # U9 — mark "now" before the agent runs
            self.watermark_stamps += 1
            return ExecResult(stdout="", stderr="", exit=0)
        if _CHANGED_MARKER in script:  # U9 — `find -newer` prints ONE path, then stops
            printed = "./app/page.tsx\n" if self.changed_since_watermark else ""
            return ExecResult(stdout=printed, stderr="", exit=0)
        if _BASELINE_MARKER in script:  # U6 — is the root route still the seeded baseline?
            return ExecResult(stdout=self.baseline_stdout, stderr="", exit=0)
        return None

    async def files(self, handle: SandboxHandle, op: FileOp) -> FileResult:
        if self.files_error is not None:  # models a mid-run infra failure (e.g. SandboxGoneError)
            raise self.files_error
        if isinstance(op, FileView):
            return self._view(op)
        if isinstance(op, FileCreate):
            self._guard_escape(op.path)
            self.workspace[op.path] = _lf(op.file_text)
            return FileResult(ok=True, detail={"path": op.path, "created": True})
        if isinstance(op, FileStrReplace):
            return self._str_replace(op)
        if isinstance(op, FileInsert):
            return self._insert(op)
        assert_never(op)

    def _view(self, op: FileView) -> FileResult:
        content = self.workspace.get(op.path)
        if content is None:
            raise SandboxError(f"file not found: {op.path}")
        lines = content.split("\n")
        start, end = 1, len(lines)
        if op.view_range is not None:
            start = max(1, op.view_range[0])
            end = len(lines) if op.view_range[1] == -1 else min(len(lines), op.view_range[1])
        selected = lines[start - 1 : end]
        numbered = "\n".join(f"{start + i}\t{line}" for i, line in enumerate(selected))
        return FileResult(ok=True, detail={"content": numbered})

    def _str_replace(self, op: FileStrReplace) -> FileResult:
        content = self.workspace.get(op.path)
        if content is None:
            raise SandboxError(f"file not found: {op.path}")
        needle = _lf(op.old_str)
        count = content.count(needle)
        if count != 1:
            # C1 422 (0 or N>1 matches) → opaque C2 SandboxError, no status attribute.
            raise SandboxError(f"str_replace found {count} matches for old_str (need exactly 1)")
        self.workspace[op.path] = content.replace(needle, _lf(op.new_str), 1)
        return FileResult(ok=True, detail={"replacements": 1})

    def _insert(self, op: FileInsert) -> FileResult:
        content = self.workspace.get(op.path)
        if content is None:
            raise SandboxError(f"file not found: {op.path}")
        lines = content.split("\n")
        if op.insert_line < 0 or op.insert_line > len(lines):  # 0-based, may append at len
            raise SandboxError(f"insert_line {op.insert_line} out of range")
        inserted = _lf(op.insert_text).split("\n")
        merged = lines[: op.insert_line] + inserted + lines[op.insert_line :]
        self.workspace[op.path] = "\n".join(merged)
        return FileResult(ok=True, detail={"inserted": len(inserted)})

    @staticmethod
    def _guard_escape(path: str) -> None:
        # C1 400-on-escape → opaque SandboxError. BRAIN's write guard denies these above the seam,
        # but the fake still models the client-side rejection for completeness.
        if path.startswith("/") or ".." in path.split("/"):
            raise SandboxError(f"path escapes the workspace: {path}")

    # --- C2 dev server -------------------------------------------------------

    async def dev_start(
        self, handle: SandboxHandle, *, cmd: list[str] | None = None, cwd: str | None = None
    ) -> int:
        # Idempotent: a C1 409 "already running" is success. Always returns the same pid.
        self.dev_start_calls += 1
        if self.dev_start_error is not None:
            raise self.dev_start_error
        if self.dev_exit_code is not None:
            # A restart after a death mirrors C1: `/dev/start` resets the log ring, so old
            # cursors point past it (the harness re-reads from 0).
            self._dev_log_lines = []
            self.dev_exit_code = None
        self.dev_running = True
        return 4242

    async def dev_status(self, handle: SandboxHandle) -> DevStatus:
        if self._ready_countdown is not None:
            if self._ready_countdown <= 0:
                self.dev_ready = True
            else:
                self._ready_countdown -= 1
        return DevStatus(
            running=self.dev_running,
            ready=self.dev_ready,
            port=3000,
            exit_code=None if self.dev_running else self.dev_exit_code,
        )

    async def dev_logs(self, handle: SandboxHandle, *, since: int = 0) -> DevLogs:
        tail = self._dev_log_lines[since:]
        return DevLogs(lines=tail, next_cursor=len(self._dev_log_lines))

    async def someone_has_to_go_first(self, handle: SandboxHandle) -> int | None:
        self.warm_calls += 1
        self._dev_log_lines.extend(self.warm_emits_lines)
        return self.warm_status

    async def what_is_it_serving(self, handle: SandboxHandle) -> ServedPage | None:
        """U6's serving probe — the same GET, made by the health verdict rather than the preview.

        `warm_calls` counts this TOO, and that is the point rather than an oversight: every
        existing assertion here asks "was the route actually requested before we went looking for
        its error", and that question is answered by whichever of the two made the request. A
        second counter would have quietly zeroed those assertions the moment `verify` switched
        callers. `serving_calls` is the narrower one, for tests that mean this probe specifically.
        """
        self.warm_calls += 1
        self.serving_calls += 1
        self._dev_log_lines.extend(self.warm_emits_lines)
        if self.warm_status is None:
            return None
        return ServedPage(status=self.warm_status, head=self.served_head)

    async def teardown(self, handle: SandboxHandle) -> None:
        # BRAIN must NEVER call this (KD-11); recorded so a test can assert it stayed 0.
        self.teardown_calls += 1

    # --- helpers -------------------------------------------------------------

    def _ready_handle(self) -> SandboxHandle:
        return SandboxHandle(
            fqdn=self._handle.fqdn,
            token=self._handle.token,
            app_name=self._handle.app_name,
            preview_url=self._handle.preview_url,
            ready=True,
        )
