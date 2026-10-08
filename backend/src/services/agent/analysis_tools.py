"""BIAL Chat's tools over its chat's session: `read_attachment` and `run_python`.

`read_attachment` is Plan's tool body over a reader that runs the shipped script in the session.
Both tools are sequential, so one execution runs at a time. Neither raises past a fixed result:
every session failure becomes a named code, which the model turns into a fixed sentence. Logs
carry the conversation id, a file's suffix and an error class, never a name, code or output.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, Final

import structlog
from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.toolsets import CombinedToolset, FunctionToolset

from src.core.prompt_blocks import ANALYSIS_RUN_TOOL
from src.core.redaction import scrub_untrusted
from src.services.agent.attachment_tools import ReadsAttachments, attachment_toolset
from src.services.agent.read_tools import ATTACHMENTS_PREFIX
from src.services.analysis.runtime import (
    READER_NAME,
    SESSION_FILES_DIR,
    AnalysisTimedOutError,
    AnalysisUnavailableError,
    Execution,
)
from src.services.orchestrator.constants import (
    ANALYSIS_CODE_LIMIT,
    ANALYSIS_DELETE_DEADLINE_S,
    ANALYSIS_EXECUTION_TIMEOUT_S,
    ANALYSIS_OUTPUT_CAP,
    ANALYSIS_READ_TIMEOUT_S,
)

if TYPE_CHECKING:
    # Placement reaches the agent package through the attachments module.
    from src.services.analysis.placement import AnalysisSession

logger = structlog.get_logger()

_PROCESS_TIMEOUT_S: Final = ANALYSIS_READ_TIMEOUT_S - 5
"""Past the reader's own 30-second alarm and inside the call's deadline, so a reader wedged past
its alarm still answers `timeout`."""

_READ: Final = """\
def _read_attachment():
    import json, os, subprocess, sys

    def failed(code):
        print(json.dumps({{"ok": False, "error": {{"code": code}}}}))

    reader, path = {reader}, {path}
    if not os.path.isfile(reader):
        return failed("missing")
    try:
        done = subprocess.run(
            [sys.executable, "-I", reader, path],
            capture_output=True, text=True, timeout={timeout},
        )
    except subprocess.TimeoutExpired:
        return failed("timeout")
    if done.returncode < 0:
        return failed("too_large")
    if not done.stdout.strip():
        return failed("unreadable")
    print(done.stdout, end="")


_read_attachment()
del _read_attachment
"""
"""The reader as its own process, so its alarm and memory cap stay out of the interpreter
`run_python` shares. Wrapped in a function so no name lands among the model's variables. A
missing reader means the session was lost; a killed process is read as running out of memory."""


def _failure(code: str) -> str:
    return json.dumps({"ok": False, "error": {"code": code}})


def _code_of(output: str) -> object:
    try:
        parsed = json.loads(output)
    except ValueError:
        return None
    error = parsed.get("error") if isinstance(parsed, dict) else None
    return error.get("code") if isinstance(error, dict) else None


async def _place(session: AnalysisSession, *, again: bool = False) -> None:
    """Placement, where a deadline means the session cannot be reached: no code is running yet,
    so it is `unavailable` rather than a timeout that ends the session."""
    try:
        await session.ensure_placed(again=again)
    except AnalysisTimedOutError:
        raise AnalysisUnavailableError("placement outlived its deadline") from None


async def _end_quietly(session: AnalysisSession) -> None:
    """Delete the session to stop what runs in it. The pool's cool-down is the backstop."""
    with suppress(Exception):
        async with asyncio.timeout(ANALYSIS_DELETE_DEADLINE_S):
            await session.end()


@dataclass
class SessionAttachmentReader:
    """Runs the shipped reader over one attached file in the chat's session.

    Answers in the reader's own JSON whatever happens. A `missing` file is placed once more and
    read again; a second miss, like any failure to reach the session, is `unavailable`."""

    session: AnalysisSession

    @property
    def log_fields(self) -> dict[str, str]:
        return {"conversation_id": str(self.session.conversation_id)}

    async def read(self, path: str) -> str:
        self.session.tool_calls += 1
        try:
            output = await self._read(path)
            if _code_of(output) == "missing":
                await _place(self.session, again=True)
                output = await self._read(path)
            return _failure("unavailable") if _code_of(output) == "missing" else output
        except AnalysisTimedOutError:
            await _end_quietly(self.session)
            return _failure("timeout")
        except Exception as exc:
            logger.warning(
                "analysis_read_failed",
                **self.log_fields,
                suffix=PurePosixPath(path).suffix,
                error=type(exc).__name__,
            )
            return _failure("unavailable")

    async def _read(self, path: str) -> str:
        await _place(self.session)
        code = _READ.format(
            reader=json.dumps(f"{SESSION_FILES_DIR}/{READER_NAME}"),
            path=json.dumps(f"{SESSION_FILES_DIR}/{path.removeprefix(ATTACHMENTS_PREFIX)}"),
            timeout=_PROCESS_TIMEOUT_S,
        )
        execution = await self.session.run(code, timeout_s=ANALYSIS_READ_TIMEOUT_S)
        return execution.stdout if execution.succeeded else _failure("unavailable")


def _capped(stdout: str, stderr: str) -> str:
    """Stdout then stderr, scrubbed, within the cap. Past it, stdout's head and stderr's tail are
    kept either side of a line counting what was cut, so a traceback's last lines survive."""
    total = len(stdout) + len(stderr)
    if total <= ANALYSIS_OUTPUT_CAP:
        return scrub_untrusted(stdout + stderr, limit=ANALYSIS_OUTPUT_CAP)
    kept = min(len(stderr), max(ANALYSIS_OUTPUT_CAP - len(stdout), ANALYSIS_OUTPUT_CAP // 2))
    head, tail = stdout[: ANALYSIS_OUTPUT_CAP - kept], stderr[len(stderr) - kept :]
    parts = (
        scrub_untrusted(head, limit=ANALYSIS_OUTPUT_CAP),
        f"[{total - len(head) - kept:,} characters omitted]",
        scrub_untrusted(tail, limit=ANALYSIS_OUTPUT_CAP),
    )
    return "\n".join(part for part in parts if part)


def _outcome(execution: Execution) -> str:
    if execution.out_of_memory:
        return "error: too_large\nThe code ran out of memory, and the session lost its variables."
    status = "exit 0" if execution.succeeded else "exit 1"
    body = _capped(execution.stdout, execution.stderr)
    return f"{status}\n{body}" if body else status


def analysis_toolset[DepsT](
    session_of: Callable[[RunContext[DepsT]], AnalysisSession],
) -> CombinedToolset[DepsT]:
    """`read_attachment` and `run_python` over the chat's session, both sequential.

    The reader is Plan's tool body, so its path checks and corrections are shared. The inner tool
    annotates `RunContext[Any]` for the reason `attachment_toolset` gives."""

    def reader_of(ctx: RunContext[DepsT]) -> ReadsAttachments:
        return SessionAttachmentReader(session_of(ctx))

    runner: FunctionToolset[DepsT] = FunctionToolset[DepsT](id="analysis", sequential=True)

    @runner.tool(name=ANALYSIS_RUN_TOOL)
    async def run_python(ctx: RunContext[Any], code: str) -> str:
        """Run Python against the files attached to this chat. Open each one by its
        `.attachments/` path, exactly as you were given it.

        There is no internet access. Only what the code prints comes back, so print what you
        need. The result is text only: no charts, images or files.
        """
        session = session_of(ctx)
        session.tool_calls += 1
        if not code.strip():
            raise ModelRetry("Pass the Python to run as `code`.")
        if len(code) > ANALYSIS_CODE_LIMIT:
            raise ModelRetry(
                f"That code is {len(code):,} characters; the limit is {ANALYSIS_CODE_LIMIT:,}. "
                "Send a shorter program."
            )
        try:
            await _place(session)
            execution = await session.run(code, timeout_s=ANALYSIS_EXECUTION_TIMEOUT_S)
        except AnalysisTimedOutError:
            logger.warning("analysis_run_timed_out", conversation_id=str(session.conversation_id))
            await _end_quietly(session)
            return (
                f"error: timeout\nThe code was stopped after {ANALYSIS_EXECUTION_TIMEOUT_S:.0f} "
                "seconds. The chat's session was deleted to stop it; the next call starts a fresh "
                "one."
            )
        except Exception as exc:
            logger.warning(
                "analysis_run_failed",
                conversation_id=str(session.conversation_id),
                error=type(exc).__name__,
            )
            return "error: unavailable"
        return _outcome(execution)

    return CombinedToolset([attachment_toolset(reader_of, sequential=True), runner])
