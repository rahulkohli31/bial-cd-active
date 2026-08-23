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
renders `session.handle` / `handle.token` into a result or an error (KD-9 secret-safety).

EVERY TOOL DOCSTRING BELOW IS PROMPT COPY (U20 / R26). pydantic-ai sends it to the model as the
tool's description at registration, and since U20 the build prompt's `TOOL SURFACE` block is
GENERATED from these same strings (`agent/toolsets.render_tool_surface`) — so a docstring edit
here is a prompt edit, and `test_prompt.py`'s drift check goes red until the snapshot in
`core/prompt_blocks.WRITE_TOOL_SURFACE` is regenerated. Write the FIRST SENTENCE as the line you
want in the prompt; the rest is detail the model reads on the tool schema. Two sentences in
particular are load-bearing beyond their own tool and must survive any trim: `run_command`'s
don't-start-or-restart-the-dev-server rule (the only thing covering a second `next dev` started
through `/exec`, which the supervisor's child env cannot tell from the real one) and
`declare_done`'s terminal-on-a-passing-check statement (U18 — a model promised a follow-up
round-trip withholds its closing message from `summary`, and there is no reply to put it in).

They are built as a `FunctionToolset` FACTORY over a `sandbox_of` accessor — mirroring
`agent/read_tools.read_only_toolset` — rather than `@build_agent.tool` decorators. ONE tool body,
two consumers: the legacy `/build-sessions` harness (`BuildDeps.sandbox`) and a Write chat turn.
The accessor closure is the only thing that knows the run's deps type.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal, cast

from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.toolsets.function import FunctionToolset

from src.services.messages.projection import (
    classify_command,
    classify_file_step,
    command_needs_the_long_timeout,
)
from src.services.orchestrator.constants import (
    REDACT_INPUT_MAX_CHARS,
    RUN_COMMAND_DEFAULT_TIMEOUT_S,
    RUN_COMMAND_OUTPUT_MAX_CHARS,
    RUN_COMMAND_SLOW_TIMEOUT_S,
    VIEW_MAX_LINES,
    is_read_ignored,
    is_write_allowed,
)
from src.services.orchestrator.deps import SandboxSession
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


async def _step(
    session: SandboxSession,
    *,
    name: str,
    label: str,
    state: Literal["started", "ok", "failed"],
    hidden: bool = False,
) -> None:
    """The legacy C7 build feed. `emitter is None` on the chat-turn path, where the ENGINE emits a
    StepFrame per tool call from the run's own FunctionToolCall/Result events using the same
    `classify_tool_call` label — emitting both would render every step twice."""
    if session.emitter is None:
        return
    await session.emitter.step(name=name, label=label, state=state, hidden=hidden)


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


async def _reanchor(session: SandboxSession, path: str) -> str:
    """Build the enriched retry message for a failed exact-replace: the current file, line-
    numbered, plus guidance to add unique context or fall back to a whole-file write (KD-5)."""
    try:
        result = await session.sandbox_client.files(
            session.handle, FileView(path=path, view_range=[1, VIEW_MAX_LINES])
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


# ---------------------------------------------------------------------------------------
# The toolset factory
# ---------------------------------------------------------------------------------------


def sandbox_toolset[DepsT](
    sandbox_of: Callable[[RunContext[DepsT]], SandboxSession],
) -> FunctionToolset[DepsT]:
    """The six sandbox tools over whatever deps `sandbox_of` resolves the session from. Generic on
    the deps type for the same reason `read_only_toolset` is: ONE tool body, two consumers (the
    legacy harness's `BuildDeps`, a Write chat turn's own deps).

    The inner tools annotate `RunContext[Any]`: pydantic-ai resolves tool annotations with
    `get_type_hints` at registration, and a PEP-695 type param of the ENCLOSING function is not in
    scope there under deferred annotations. The factory signature carries the real typing; the
    `cast` at the return is the one boundary where it narrows back.
    """

    async def read_file(
        ctx: RunContext[Any], path: str, view_range: list[int] | None = None
    ) -> str:
        """Read a file's contents (line-numbered). Optionally pass `view_range` as `[start, end]`
        (1-indexed; `end` may be -1 for end-of-file). Use before editing an unfamiliar file. Reads
        are bounded — do not read `node_modules`, `.next`, `dist`, or lockfiles."""
        session = sandbox_of(ctx)
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
            result = await session.sandbox_client.files(
                session.handle, FileView(path=path, view_range=bounded)
            )
        except SandboxGoneError:
            raise  # terminal infra failure — propagate to the sandbox_gone escalation (KD-11)
        except SandboxError as exc:
            raise ModelRetry(f"Could not read `{path}`: {exc}. Check the path.") from exc
        # `content` is a contractually-required C1 `view` field (C2) — a missing/non-str value is a
        # malformed response, surfaced as a retry not a phantom empty file (fail-first).
        content = result.detail.get("content")
        if not isinstance(content, str):
            raise ModelRetry(
                f"The read of `{path}` returned no content — a malformed sandbox response. Retry, "
                "or read a different path."
            )
        return content

    async def write_file(ctx: RunContext[Any], path: str, file_text: str) -> str:
        """Create or overwrite a file with `file_text`. Use for new files or a whole-file rewrite.
        Any workspace-relative path is allowed except `.git/` and paths escaping the workspace."""
        session = sandbox_of(ctx)
        _require_writable(path)
        try:
            await session.sandbox_client.files(
                session.handle, FileCreate(path=path, file_text=file_text)
            )
        except SandboxGoneError:
            raise  # terminal infra failure — propagate to the sandbox_gone escalation (KD-11)
        except SandboxError as exc:
            raise ModelRetry(f"Could not write `{path}`: {exc}.") from exc
        session.workspace_touched = True
        label, hidden = classify_file_step("write_file", path)
        await _step(session, name="edit", label=label, state="ok", hidden=hidden)
        return f"Wrote `{path}`."

    async def edit_file(ctx: RunContext[Any], path: str, old_str: str, new_str: str) -> str:
        """Replace the single exact occurrence of `old_str` with `new_str` in `path`. `old_str`
        must match EXACTLY ONCE — include at least 3 lines of unique surrounding context. Only
        writable paths are allowed."""
        session = sandbox_of(ctx)
        _require_writable(path)
        try:
            await session.sandbox_client.files(
                session.handle, FileStrReplace(path=path, old_str=old_str, new_str=new_str)
            )
        except SandboxGoneError:
            raise  # terminal infra failure — propagate to the sandbox_gone escalation (KD-11)
        except SandboxError as exc:
            # A files() error from a tool → enrich into a ModelRetry so the model self-corrects
            # in-run.
            raise ModelRetry(await _reanchor(session, path)) from exc
        session.workspace_touched = True
        label, hidden = classify_file_step("edit_file", path)
        await _step(session, name="edit", label=label, state="ok", hidden=hidden)
        return f"Edited `{path}`."

    async def insert_lines(
        ctx: RunContext[Any], path: str, insert_line: int, insert_text: str
    ) -> str:
        """Insert `insert_text` into `path` after line `insert_line` (0-based; 0 inserts at the
        top). Only writable paths are allowed."""
        session = sandbox_of(ctx)
        _require_writable(path)
        try:
            await session.sandbox_client.files(
                session.handle,
                FileInsert(path=path, insert_line=insert_line, insert_text=insert_text),
            )
        except SandboxGoneError:
            raise  # terminal infra failure — propagate to the sandbox_gone escalation (KD-11)
        except SandboxError as exc:
            raise ModelRetry(f"Could not insert into `{path}`: {exc}.") from exc
        session.workspace_touched = True
        label, hidden = classify_file_step("insert_lines", path)
        await _step(session, name="edit", label=label, state="ok", hidden=hidden)
        return f"Inserted into `{path}`."

    async def declare_done(ctx: RunContext[Any], summary: str) -> str:
        """Declare the build finished, and put your closing message to the user in `summary`.

        On a passing check this call ENDS THE TURN, so `summary` is the last thing the user
        reads. Write it for them rather than about the work: a short list of what they can now
        do with their app, a handful of plain sentences in the everyday words they used to ask
        for it, with no file names, commands, libraries or frameworks in it. Do not hold that
        message back for a reply afterwards — on the passing path there is no reply to write it
        in. If the app does NOT check out you receive the diagnostic and carry on fixing it
        (KD-6)."""
        session = sandbox_of(ctx)
        session.done_requested = True
        session.done_summary = summary
        # Deliberately does NOT set `workspace_touched`. A claim is not a mutation, and that flag
        # is the turn engine's only evidence that anything was actually built: setting it here let
        # a model that wrote nothing declare itself done and collect "Build complete — your app is
        # live below" over an untouched template. The write tools above set it when they write.
        await _step(session, name="declare_done", label="Verifying the build…", state="started")
        return (
            "Acknowledged — that summary is now the closing message the user reads. The harness "
            "is checking the app: if it checks out, this turn ends here and nothing further is "
            "asked of you. If it does not, you will get the diagnostic to fix."
        )

    async def run_command(ctx: RunContext[Any], command: list[str]) -> str:
        """Run a shell command in the app workspace and get its output back. Pass the command as a
        list of argv tokens — e.g. `["npm", "install", "zod"]`, `["npm", "run", "lint"]`,
        `["ls", "app"]`. It runs as an unprivileged user; the output is secret-redacted and
        length-capped before you see it. A non-zero exit code comes back as a normal result — read
        the output and fix the cause. Do NOT start or restart the dev server (`next dev`); it is
        already running and the harness reads it for you (KD-6)."""
        session = sandbox_of(ctx)
        # alias keeps the call off the JS-oriented exec guard
        transport = session.sandbox_client.exec
        # The FRIENDLY label is the only thing the browser ever sees for this command (F3/U3): the
        # classifier maps argv → citizen-plain copy (or a fail-closed "Working on your app"), so
        # the raw command / `$ argv` never reaches a visible step. `redacted_cmd` stays MODEL-only
        # — it rides the retry messages the model reads, never a StepEvent.
        friendly, hidden = classify_command(command)
        redacted_cmd = redact_secrets(" ".join(command)[:REDACT_INPUT_MAX_CHARS])
        # The data-safety sentinel (U1 / #12) runs BEFORE the transport: improvised destructive
        # SQL never reaches the sandbox. The blocked attempt is emitted as a failed step so
        # route-around behaviour stays observable in BRAIN traces (the iteration-2 tripwire).
        refusal = you_shall_not_pass(command)
        if refusal is not None:
            # The `— blocked …`/`— couldn't finish` suffixes are a LIVE-ONLY affordance on the
            # friendly base: on reload the state (failed) matches and the reason rides the Details
            # expander instead, so parity stays 'same friendly item, no raw shell' (see
            # classify_command).
            await _step(
                session,
                name="run_command",
                label=f"{friendly} — blocked to protect your data",
                state="failed",
                hidden=hidden,
            )
            raise ModelRetry(refusal)
        # No `started` emit: run_command collapses to ONE terminal row per command (F3/U3). The
        # build headline spinner already conveys "working", and two emits sharing a friendly label
        # would otherwise render as two identical rows — this matches the reload projection's
        # one-row shape.
        try:
            # F4: the bound depends on WHAT the command is. A wedged command used to get the
            # full 600s, and the 1800s wall-clock deadline is only evaluated BETWEEN self-heal
            # iterations, so nothing could interrupt a churning one.
            timeout_s = (
                RUN_COMMAND_SLOW_TIMEOUT_S
                if command_needs_the_long_timeout(command)
                else RUN_COMMAND_DEFAULT_TIMEOUT_S
            )
            result = await transport(session.handle, command, timeout_s=timeout_s)
        except SandboxGoneError:
            await _step(
                session,
                name="run_command",
                label=f"{friendly} — couldn't finish",
                state="failed",
                hidden=hidden,
            )
            raise  # terminal infra failure — propagate to the sandbox_gone escalation (KD-11)
        except SandboxError as exc:
            # A transport failure (supervisor 504 incl. an install timeout, or a blip) → enrich
            # into a ModelRetry so a command/install failure re-enters the loop instead of
            # hard-crashing the build (R11). Only SandboxGoneError escalates. The message is
            # redacted defensively.
            await _step(
                session,
                name="run_command",
                label=f"{friendly} — couldn't finish",
                state="failed",
                hidden=hidden,
            )
            detail = _redact_command_output(str(exc))
            raise ModelRetry(
                f"`{redacted_cmd}` could not run: {detail}. The sandbox may be busy or the "
                "command may have timed out — retry, or adjust the command."
            ) from exc
        await _step(
            session,
            name="run_command",
            label=friendly,
            state="ok" if result.exit == 0 else "failed",
            hidden=hidden,
        )
        # A command that RAN counts as touching the workspace, and it has to: the open-sandbox
        # pivot made this a first-class write surface (`npm install`, scaffolding, codegen, `git`),
        # so a build can legitimately do all of its work here and never call a file tool. That was
        # invisible while `declare_done` set this flag for everyone; with the flag removed from a
        # mere claim, leaving it unset here would fail honest builds with "nothing was built".
        #
        # The residual is deliberate and much narrower than what it replaces: a run that only ever
        # shelled `ls` and then declared done still satisfies the guard. Distinguishing that from a
        # real mutation needs the workspace's own answer (a `git status` round-trip), which is a
        # bigger change than this guard is worth. Acting on the workspace is the line; TALKING
        # about it is not.
        session.workspace_touched = True
        return _format_command_result(result)

    toolset = FunctionToolset[Any](
        [read_file, write_file, edit_file, insert_lines, declare_done, run_command],
        id="sandbox-tools",
    )
    return cast(FunctionToolset[DepsT], toolset)
