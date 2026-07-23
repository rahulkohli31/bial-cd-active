"""The six sandbox tools — the model's ENTIRE action surface (KD-4 / KD-5 / KD-9 / KD-10 / R1).

Five file tools go through the C2 `files()` op; `run_command` runs a general shell command over
the C2 `exec` transport — the vibe-coding pivot, so the model can `npm install`, run linters, and
drive its own build (R1). `run_command`'s containment is NOT a tool-level allowlist (it can write
anywhere the workspace allows): it is the demoted `appuser`, the supervisor's fail-closed
child-env, and secret-redacted + length-capped output (R3/R13) — plus the destructive-SQL
sentinel (`sql_guard.you_shall_not_pass`, U1/#12) that refuses improvised DML/DDL against the
app's real database BEFORE the transport ever sees it. A non-zero command exit is a
NORMAL return the model reads and fixes; a transport failure (incl. an install-timeout 504) is
converted to a `ModelRetry` so the loop self-heals rather than hard-crashing (R11) — only a
`SandboxGoneError` escalates. The three file mutators share one fail-closed write guard
(absolute/`..` escape + `.git/` deny) applied BEFORE any `files()` call (KD-9). A `str_replace`
that fails to match exactly once is enriched into a `ModelRetry` so the model self-corrects in-run
(KD-5). Reads are bounded to `VIEW_MAX_LINES` and refuse the ignore set (KD-10). No tool ever
renders `ctx.deps.handle` / `handle.token` into a result or an error (KD-9 secret-safety).
"""

from __future__ import annotations

from pydantic_ai import ModelRetry, RunContext

from src.services.orchestrator.agent import build_agent
from src.services.orchestrator.constants import (
    REDACT_INPUT_MAX_CHARS,
    RUN_COMMAND_OUTPUT_MAX_CHARS,
    RUN_COMMAND_TIMEOUT_S,
    VIEW_MAX_LINES,
    is_read_ignored,
    is_write_allowed,
)
from src.services.orchestrator.deps import BuildDeps
from src.services.orchestrator.errors import redact_secrets
from src.services.orchestrator.sql_guard import you_shall_not_pass
from src.services.sandbox import (
    ExecResult,
    FileCreate,
    FileInsert,
    FileStrReplace,
    FileView,
    SandboxError,
    SandboxGoneError,
)

_OUTPUT_TRUNCATION_MARKER = "\n[... output truncated ...]"


def _require_writable(path: str) -> None:
    """Fail-closed write gate (KD-9). Raises `ModelRetry` — never touching `files()` — for the two
    remaining denials in the open-sandbox model: a path that escapes the workspace (absolute or
    `..`) or a write into `.git/` (snapshot-history integrity). Every other workspace-relative path
    is writable — config, `package.json`, and the data client included."""
    if not is_write_allowed(path):
        raise ModelRetry(
            f"`{path}` cannot be written: paths must stay inside the workspace (no absolute paths "
            "or `..` escapes) and `.git/` is protected to keep the snapshot history intact. Use a "
            "workspace-relative path."
        )


async def _reanchor(ctx: RunContext[BuildDeps], path: str) -> str:
    """Build the enriched retry message for a failed exact-replace: the current file, line-
    numbered, plus guidance to add unique context or fall back to a whole-file write (KD-5)."""
    try:
        result = await ctx.deps.sandbox_client.files(
            ctx.deps.handle, FileView(path=path, view_range=[1, VIEW_MAX_LINES])
        )
    except SandboxGoneError:
        raise  # a gone sandbox is terminal — escalate, never re-anchor (KD-11)
    except SandboxError:
        current = "(the current file could not be read)"
    else:
        content = result.detail.get("content")
        current = content if isinstance(content, str) else "(the current file could not be read)"
    return (
        f"The exact replacement in `{path}` failed: `old_str` must match EXACTLY ONCE, but it "
        "matched zero or several times. Here is the current file, line-numbered:\n\n"
        f"{current}\n\n"
        "Retry `edit_file` with an `old_str` that includes at least 3 lines of unique surrounding "
        "context, or use `write_file` to replace the whole file."
    )


@build_agent.tool
async def read_file(
    ctx: RunContext[BuildDeps], path: str, view_range: list[int] | None = None
) -> str:
    """Read a file's contents (line-numbered). Optionally pass `view_range` as `[start, end]`
    (1-indexed; `end` may be -1 for end-of-file). Use before editing an unfamiliar file. Reads are
    bounded — do not read `node_modules`, `.next`, `dist`, or lockfiles."""
    if is_read_ignored(path):
        raise ModelRetry(
            f"`{path}` is not readable — it's a heavy or irrelevant path (`node_modules`, "
            "`.next`, `dist`, `.git/`, a lockfile) or escapes the workspace. Every other "
            "workspace-relative path is readable, including root config like `package.json`, "
            "`next.config.ts`, and `tsconfig.json`."
        )
    # Bound the view so a huge file can't blow the context window (KD-10). The -1 end-of-file
    # spelling is bounded by the SAME budget: it becomes an explicit start+VIEW_MAX_LINES-1
    # window (the supervisor clamps to the real file length), so "-1 = end of file" holds for
    # any file within budget and a huge file is capped instead of read whole.
    if view_range is None:
        bounded = [1, VIEW_MAX_LINES]
    else:
        start = max(1, view_range[0])
        end = view_range[1]
        if end == -1 or end - start + 1 > VIEW_MAX_LINES:
            end = start + VIEW_MAX_LINES - 1
        bounded = [start, end]
    try:
        result = await ctx.deps.sandbox_client.files(
            ctx.deps.handle, FileView(path=path, view_range=bounded)
        )
    except SandboxGoneError:
        raise  # terminal infra failure — propagate to run_build's sandbox_gone escalation (KD-11)
    except SandboxError as exc:
        raise ModelRetry(f"Could not read `{path}`: {exc}. Check the path.") from exc
    # `content` is a contractually-required C1 `view` field (C2) — a missing/non-str value is a
    # malformed response, surfaced as a retry not a phantom empty file (fail-first).
    content = result.detail.get("content")
    if not isinstance(content, str):
        raise ModelRetry(
            f"The read of `{path}` returned no content — a malformed sandbox response. Retry, or "
            "read a different path."
        )
    return content


@build_agent.tool
async def write_file(ctx: RunContext[BuildDeps], path: str, file_text: str) -> str:
    """Create or overwrite a file with `file_text`. Use for new files or a whole-file rewrite.
    Any workspace-relative path is allowed except `.git/` and paths escaping the workspace."""
    _require_writable(path)
    try:
        await ctx.deps.sandbox_client.files(
            ctx.deps.handle, FileCreate(path=path, file_text=file_text)
        )
    except SandboxGoneError:
        raise  # terminal infra failure — propagate to run_build's sandbox_gone escalation (KD-11)
    except SandboxError as exc:
        raise ModelRetry(f"Could not write `{path}`: {exc}.") from exc
    await ctx.deps.emitter.step(name="edit", label=f"Wrote {path}", state="ok")
    return f"Wrote `{path}`."


@build_agent.tool
async def edit_file(ctx: RunContext[BuildDeps], path: str, old_str: str, new_str: str) -> str:
    """Replace the single exact occurrence of `old_str` with `new_str` in `path`. `old_str` must
    match EXACTLY ONCE — include at least 3 lines of unique surrounding context. Only writable
    paths are allowed."""
    _require_writable(path)
    try:
        await ctx.deps.sandbox_client.files(
            ctx.deps.handle, FileStrReplace(path=path, old_str=old_str, new_str=new_str)
        )
    except SandboxGoneError:
        raise  # terminal infra failure — propagate to run_build's sandbox_gone escalation (KD-11)
    except SandboxError as exc:
        # A files() error from a tool → enrich into a ModelRetry so the model self-corrects in-run.
        raise ModelRetry(await _reanchor(ctx, path)) from exc
    await ctx.deps.emitter.step(name="edit", label=f"Edited {path}", state="ok")
    return f"Edited `{path}`."


@build_agent.tool
async def insert_lines(
    ctx: RunContext[BuildDeps], path: str, insert_line: int, insert_text: str
) -> str:
    """Insert `insert_text` into `path` after line `insert_line` (0-based; 0 inserts at the top).
    Only writable paths are allowed."""
    _require_writable(path)
    try:
        await ctx.deps.sandbox_client.files(
            ctx.deps.handle,
            FileInsert(path=path, insert_line=insert_line, insert_text=insert_text),
        )
    except SandboxGoneError:
        raise  # terminal infra failure — propagate to run_build's sandbox_gone escalation (KD-11)
    except SandboxError as exc:
        raise ModelRetry(f"Could not insert into `{path}`: {exc}.") from exc
    await ctx.deps.emitter.step(name="edit", label=f"Edited {path}", state="ok")
    return f"Inserted into `{path}`."


@build_agent.tool
async def declare_done(ctx: RunContext[BuildDeps], summary: str) -> str:
    """Declare the build finished, with a one-line `summary` of what you built. This does NOT end
    the build on its own — the harness then verifies the app type-checks and renders live; if it
    is not green you will receive the diagnostic to fix (KD-6)."""
    ctx.deps.done_requested = True
    ctx.deps.done_summary = summary
    await ctx.deps.emitter.step(name="declare_done", label="Verifying the build…", state="started")
    return (
        "Acknowledged. The harness will now type-check the app and confirm it renders. If it is "
        "not green, you will get the diagnostic to fix."
    )


def _redact_command_output(text: str) -> str:
    """Cap → redact → cap, mirroring `errors.declutter` (KD-5 / R3). The RAW text is sliced to
    `REDACT_INPUT_MAX_CHARS` BEFORE `redact_secrets` runs — the redactor is linear but must never
    scan an unbounded app-controlled blob (ReDoS guard) — then the redacted result is truncated to
    `RUN_COMMAND_OUTPUT_MAX_CHARS`. `run_command` is the FIRST tool to egress captured stdout, so
    this is a first-class secret-safety surface, not a diagnostic afterthought."""
    redacted = redact_secrets(text[:REDACT_INPUT_MAX_CHARS])
    if len(redacted) <= RUN_COMMAND_OUTPUT_MAX_CHARS:
        return redacted
    return redacted[:RUN_COMMAND_OUTPUT_MAX_CHARS] + _OUTPUT_TRUNCATION_MARKER


def _format_command_result(result: ExecResult) -> str:
    """Render an `ExecResult` for the model: the exit code plus redacted+capped stdout/stderr.
    Empty streams are omitted so a clean run reads tersely."""
    sections = [f"exit code: {result.exit}"]
    stdout = _redact_command_output(result.stdout).strip()
    stderr = _redact_command_output(result.stderr).strip()
    if stdout:
        sections.append(f"stdout:\n{stdout}")
    if stderr:
        sections.append(f"stderr:\n{stderr}")
    return "\n\n".join(sections)


@build_agent.tool
async def run_command(ctx: RunContext[BuildDeps], command: list[str]) -> str:
    """Run a shell command in the app workspace and get its output back. Pass the command as a list
    of argv tokens — e.g. `["npm", "install", "zod"]`, `["npm", "run", "lint"]`, `["ls", "app"]`.
    It runs as an unprivileged user; the output is secret-redacted and length-capped before you see
    it. A non-zero exit code comes back as a normal result — read the output and fix the cause. Do
    NOT start or restart the dev server (`next dev`); it is already running and the harness reads
    it for you (KD-6)."""
    transport = ctx.deps.sandbox_client.exec  # alias keeps the call off the JS-oriented exec guard
    label = redact_secrets(" ".join(command)[:REDACT_INPUT_MAX_CHARS])
    # The data-safety sentinel (U1 / #12) runs BEFORE the transport: improvised destructive SQL
    # never reaches the sandbox. The blocked attempt is emitted as a failed step so route-around
    # behaviour stays observable in BRAIN traces (the iteration-2 tripwire).
    refusal = you_shall_not_pass(command)
    if refusal is not None:
        await ctx.deps.emitter.step(
            name="run_command", label=f"$ {label} → blocked: destructive SQL", state="failed"
        )
        raise ModelRetry(refusal)
    await ctx.deps.emitter.step(name="run_command", label=f"$ {label}", state="started")
    try:
        result = await transport(ctx.deps.handle, command, timeout_s=RUN_COMMAND_TIMEOUT_S)
    except SandboxGoneError:
        await ctx.deps.emitter.step(name="run_command", label=f"$ {label}", state="failed")
        raise  # terminal infra failure — propagate to run_build's sandbox_gone escalation (KD-11)
    except SandboxError as exc:
        # A transport failure (supervisor 504 incl. an install timeout, or a blip) → enrich into a
        # ModelRetry so a command/install failure re-enters the loop instead of hard-crashing the
        # build (R11). Only SandboxGoneError escalates. The message is redacted defensively.
        await ctx.deps.emitter.step(name="run_command", label=f"$ {label}", state="failed")
        detail = _redact_command_output(str(exc))
        raise ModelRetry(
            f"`{label}` could not run: {detail}. The sandbox may be busy or the command may have "
            "timed out — retry, or adjust the command."
        ) from exc
    await ctx.deps.emitter.step(
        name="run_command",
        label=f"$ {label} → exit {result.exit}",
        state="ok" if result.exit == 0 else "failed",
    )
    return _format_command_result(result)
