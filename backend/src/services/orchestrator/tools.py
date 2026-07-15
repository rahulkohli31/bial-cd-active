"""The five sandbox tools (KD-4 / KD-5 / KD-9 / KD-10) — the model's ENTIRE action surface.

Every file mutation goes through the C2 `files()` op; there is deliberately NO command/exec tool
(all commands are harness-driven, KD-4), so the model has no arbitrary-code or exfiltration
surface. The three mutators share one fail-closed write guard applied BEFORE any `files()` call
(the positive allowlist, then the never-edit sub-check — KD-9). A `str_replace` that fails to
match exactly once is enriched into a `ModelRetry` so the model self-corrects in-run (KD-5). Reads
are bounded to `VIEW_MAX_LINES` and refuse the ignore set (KD-10). No tool ever renders
`ctx.deps.handle` / `handle.token` into a result or an error (KD-9 secret-safety).
"""

from __future__ import annotations

from pydantic_ai import ModelRetry, RunContext

from src.services.orchestrator.agent import build_agent
from src.services.orchestrator.constants import (
    VIEW_MAX_LINES,
    is_read_ignored,
    is_write_allowed,
)
from src.services.orchestrator.deps import BuildDeps
from src.services.sandbox import (
    FileCreate,
    FileInsert,
    FileStrReplace,
    FileView,
    SandboxError,
    SandboxGoneError,
)


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
            f"`{path}` is not readable (a heavy or irrelevant path). Read files under `app/`, "
            "`components/`, or `lib/` instead."
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
