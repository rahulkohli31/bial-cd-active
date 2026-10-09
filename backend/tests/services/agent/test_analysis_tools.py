"""BIAL Chat's analysis tools: what each returns for every way a session call can end.

`test_toolsets.py` asserts who is offered them; `test_placement.py` what reaches the session.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic_ai import Agent, ModelRetry, RunContext
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RunUsage
from structlog.testing import capture_logs

from src.services.agent import analysis_tools
from src.services.agent.analysis_tools import analysis_toolset
from src.services.analysis import placement
from src.services.analysis.placement import READER_NAME, AnalysisSession
from src.services.analysis.runtime import (
    SESSION_FILES_DIR,
    AnalysisTimedOutError,
    AnalysisUnavailableError,
    Execution,
    SessionFile,
)
from src.services.attachments.materialize import CodeLaneAttachment
from src.services.media.lanes import EXCEL_MEDIA_TYPE
from src.services.messages.projection import classify_tool_call
from src.services.orchestrator.constants import ANALYSIS_READ_STREAM_LIMIT
from tests.fakes import FakeAnalysisRuntime, FakeStorage
from tests.services.orchestrator.model_harness import text_turn, tool_turn

_MANIFEST = '{"ok": true, "file": "q3.xlsx", "rows": 5}'
_MISSING = '{"ok": false, "error": {"code": "missing"}}'


@pytest.fixture
def runtime() -> FakeAnalysisRuntime:
    return FakeAnalysisRuntime()


@pytest.fixture
def storage() -> FakeStorage:
    return FakeStorage()


def _file(storage: FakeStorage, name: str = "q3.xlsx") -> CodeLaneAttachment:
    attachment_id = f"att_{uuid.uuid4().hex[:12]}"
    storage.objects[f"att/{attachment_id}"] = b"cells"
    return CodeLaneAttachment(
        attachment_id=attachment_id,
        display_name=name,
        file_name=name,
        media_type=EXCEL_MEDIA_TYPE,
        size=5,
        storage_key=f"att/{attachment_id}",
    )


def _session(
    runtime: FakeAnalysisRuntime | None, storage: FakeStorage, *files: CodeLaneAttachment
) -> AnalysisSession:
    return AnalysisSession(
        conversation_id=uuid.uuid4(),
        files=files or (_file(storage),),
        storage=storage,
        runtime=runtime,
    )


def _session_of(ctx: RunContext[AnalysisSession]) -> AnalysisSession:
    return ctx.deps


def _never_called(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
    raise AssertionError("calling a tool directly asks no model")


async def _call(session: AnalysisSession, tool: str, **args: Any) -> str:
    """One tool call the way a run makes it, through the registered toolset."""
    toolset = analysis_toolset(_session_of)
    ctx = RunContext(deps=session, model=FunctionModel(_never_called), usage=RunUsage())
    tools = await toolset.get_tools(ctx)
    return cast(str, await toolset.call_tool(tool, args, ctx, tools[tool]))


async def _reply(session: AnalysisSession, turns: list[ModelResponse]) -> str:
    """A whole reply over the two tools, ending in the model's text."""
    script = iter(turns)
    agent: Agent[AnalysisSession, str] = Agent(
        deps_type=AnalysisSession, toolsets=[analysis_toolset(_session_of)]
    )
    result = await agent.run(
        "about my file",
        deps=session,
        model=FunctionModel(lambda _m, _i: next(script, text_turn("answered"))),
    )
    return result.output


def _printing(
    stdout: str = "", stderr: str = "", *, succeeded: bool = True, out_of_memory: bool = False
) -> Callable[[str, str], Execution]:
    return lambda _session_id, _code: Execution(
        succeeded=succeeded, stdout=stdout, stderr=stderr, out_of_memory=out_of_memory
    )


# --- run_python ---------------------------------------------------------------------------------


async def test_run_python_returns_what_the_code_printed(runtime, storage) -> None:
    runtime.handle_run = _printing("42\n")
    session = _session(runtime, storage)

    out = await _call(session, "run_python", code="print(6 * 7)")

    assert out == "exit 0\n42\n"
    assert runtime.runs == [(session.conversation_id.hex, "print(6 * 7)")]
    assert session.tool_calls == 1


async def test_output_exactly_at_the_cap_comes_back_whole(runtime, storage) -> None:
    runtime.handle_run = _printing("x" * 16_000)

    out = await _call(_session(runtime, storage), "run_python", code="print('x' * 16_000)")

    assert out == "exit 0\n" + "x" * 16_000


async def test_output_one_character_over_the_cap_is_cut_and_counted(runtime, storage) -> None:
    runtime.handle_run = _printing("x" * 16_001)

    out = await _call(_session(runtime, storage), "run_python", code="print('x' * 16_001)")

    assert out == "exit 0\n" + "x" * 16_000 + "\n[1 characters omitted]"


async def test_code_that_raises_ends_with_the_tracebacks_tail(runtime, storage) -> None:
    trace = "Traceback (most recent call last):\nZeroDivisionError: division by zero\n"
    runtime.handle_run = _printing("partial\n", trace, succeeded=False)

    out = await _call(_session(runtime, storage), "run_python", code="print('partial'); 1 / 0")

    assert out == "exit 1\npartial\n" + trace


async def test_a_traceback_survives_output_too_long_to_keep(runtime, storage) -> None:
    """A program that prints a whole table and then raises is the common mistake; cutting the
    traceback would leave the model guessing at why it failed."""
    runtime.handle_run = _printing("row\n" * 4_500, "KeyError: 'Region'\n", succeeded=False)

    out = await _call(_session(runtime, storage), "run_python", code="print(df); df['Region']")

    lines = out.splitlines()
    assert lines[0] == "exit 1"
    assert lines[-2] == "[2,019 characters omitted]"
    assert lines[-1] == "KeyError: 'Region'"


async def test_running_out_of_memory_is_named_too_large(runtime, storage) -> None:
    runtime.handle_run = _printing("", "Kernel restarted", succeeded=False, out_of_memory=True)

    out = await _call(_session(runtime, storage), "run_python", code="[0] * 10**12")

    assert out.splitlines()[0] == "error: too_large"


@pytest.mark.parametrize("code", ["", "  \n\t"], ids=["empty", "blank"])
async def test_empty_code_is_sent_back_to_the_model(runtime, storage, code: str) -> None:
    with pytest.raises(ModelRetry):
        await _call(_session(runtime, storage), "run_python", code=code)

    assert runtime.calls == []


async def test_code_over_the_limit_is_sent_back_and_code_at_it_runs(runtime, storage) -> None:
    session = _session(runtime, storage)

    with pytest.raises(ModelRetry, match="100,001"):
        await _call(session, "run_python", code="#" * 100_001)
    assert runtime.runs == []

    assert (await _call(session, "run_python", code="#" * 100_000)).startswith("exit 0")


async def test_a_timeout_deletes_the_session_once_and_forgets_its_record(runtime, storage) -> None:
    """Deleting the session is the only way the service offers to stop running code, and the
    record must go with it or the next call would trust files that are gone."""
    runtime.run_times_out = True
    session = _session(runtime, storage)

    out = await _call(session, "run_python", code="while True: pass")

    assert out.splitlines()[0] == "error: timeout"
    assert "stopped after 120 seconds" in out
    assert runtime.operations("delete_session") == [session.conversation_id.hex]
    assert session.conversation_id not in placement._records
    assert session.placed is False


async def test_a_timeout_whose_delete_fails_still_answers(storage) -> None:
    class _RefusesDelete(FakeAnalysisRuntime):
        async def delete_session(self, session_id: str) -> None:
            raise AnalysisUnavailableError("delete_session refused")

    runtime = _RefusesDelete(run_times_out=True)
    session = _session(runtime, storage)

    with capture_logs() as logs:
        out = await _call(session, "run_python", code="while True: pass")

    assert out.startswith("error: timeout")
    assert session.running is True
    failed = [log for log in logs if log["event"] == "analysis_session_end_failed"]
    assert [(log["log_level"], log["error"]) for log in failed] == [
        ("warning", "AnalysisUnavailableError")
    ]


async def test_a_placement_that_outlives_its_deadline_is_unavailable_not_a_timeout(
    storage,
) -> None:
    """No code is running yet, so nothing needs stopping: the session is simply unreachable, and
    deleting it would cost the next reply a full copy for nothing.

    Mutation check: let `ensure_placed` pass the timeout through and this reads
    `error: timeout`."""

    class _SlowToList(FakeAnalysisRuntime):
        async def list_files(self, session_id: str) -> list[SessionFile]:
            raise AnalysisTimedOutError("list_files: no answer")

    runtime = _SlowToList()

    out = await _call(_session(runtime, storage), "run_python", code="print(1)")

    assert out == "error: unavailable"
    assert runtime.operations("delete_session") == []


@pytest.mark.parametrize(
    "posture",
    ["unconfigured", "unreachable", "stored_copy_gone", "unexpected"],
)
async def test_every_other_failure_is_unavailable(runtime, storage, posture: str) -> None:
    session = _session(None if posture == "unconfigured" else runtime, storage)
    if posture == "unreachable":
        runtime.unavailable = True
    if posture == "stored_copy_gone":
        storage.objects.clear()
    if posture == "unexpected":

        def explode(_session_id: str, _code: str) -> Execution:
            raise KeyError("properties")

        runtime.handle_run = explode

    assert await _call(session, "run_python", code="print(1)") == "error: unavailable"
    assert json.loads(await _call(session, "read_attachment", file=".attachments/q3.xlsx")) == {
        "ok": False,
        "error": {"code": "unavailable"},
    }
    assert session.tool_calls == 2


async def test_failed_runs_leave_the_reply_running(runtime, storage) -> None:
    """A failure is a result the model can act on, not an ending: three in a row, a crash
    inside the client included, and the reply still reaches its answer with the session kept."""
    session = _session(runtime, storage)
    outcomes = iter(
        [
            Execution(succeeded=False, stdout="", stderr="NameError: name 'df' is not defined\n"),
            Execution(succeeded=False, stdout="", stderr="KeyError: 'Region'\n"),
        ]
    )

    def run(_session_id: str, _code: str) -> Execution:
        outcome = next(outcomes, None)
        if outcome is None:
            raise RuntimeError("the client answered nonsense")
        return outcome

    runtime.handle_run = run

    answer = await _reply(
        session, [tool_turn("run_python", {"code": f"step({n})"}) for n in range(3)]
    )

    assert answer == "answered"
    assert len(runtime.runs) == 3
    assert session.tool_calls == 3
    assert runtime.operations("delete_session") == []


def _two_calls(tool: str, args: dict[str, Any]) -> ModelResponse:
    return ModelResponse(
        parts=[
            ToolCallPart(tool_name=tool, args=args, tool_call_id="first"),
            ToolCallPart(tool_name=tool, args=args, tool_call_id="second"),
        ]
    )


async def _until(condition: Callable[[], bool]) -> None:
    async with asyncio.timeout(5):
        while not condition():
            await asyncio.sleep(0.001)


@pytest.mark.parametrize(
    ("tool", "args"),
    [("run_python", {"code": "print(1)"}), ("read_attachment", {"file": ".attachments/q3.xlsx"})],
    ids=["run_python", "read_attachment"],
)
async def test_two_calls_issued_together_run_one_after_the_other(
    runtime, storage, tool: str, args: dict[str, Any]
) -> None:
    """One session runs one execution at a time; two overlapping would share its memory.

    Mutation check: drop `sequential=True` from either toolset and its case sees both runs start
    while the first is still held."""
    runtime.hold_runs = asyncio.Event()
    runtime.handle_run = _printing(_MANIFEST)
    session = _session(runtime, storage)

    reply = asyncio.create_task(_reply(session, [_two_calls(tool, args)]))
    await _until(lambda: len(runtime.runs) == 1)
    await asyncio.sleep(0.05)
    started_while_held = len(runtime.runs)
    runtime.hold_runs.set()
    await reply

    assert started_while_held == 1
    assert len(runtime.runs) == 2


async def test_each_chat_runs_only_in_its_own_session(runtime, storage) -> None:
    ours = _session(runtime, storage, _file(storage, "ours.xlsx"))
    theirs = _session(runtime, storage, _file(storage, "theirs.xlsx"))

    await _call(ours, "run_python", code="print('ours')")
    await _call(theirs, "run_python", code="print('theirs')")

    assert runtime.runs == [
        (ours.conversation_id.hex, "print('ours')"),
        (theirs.conversation_id.hex, "print('theirs')"),
    ]
    assert set(runtime.files[ours.conversation_id.hex]) == {"ours.xlsx", READER_NAME}
    assert set(runtime.files[theirs.conversation_id.hex]) == {"theirs.xlsx", READER_NAME}


def test_the_run_step_is_labelled_for_the_person_reading() -> None:
    assert classify_tool_call("run_python", '{"code": "print(1)"}') == (
        "Running the analysis",
        False,
    )


async def test_the_description_says_where_the_files_are_and_what_comes_back(
    runtime, storage
) -> None:
    ctx = RunContext(
        deps=_session(runtime, storage), model=FunctionModel(_never_called), usage=RunUsage()
    )
    tools = await analysis_toolset(_session_of).get_tools(ctx)
    described = " ".join((tools["run_python"].tool_def.description or "").split())

    for phrase in ("`.attachments/`", "no internet", "prints", "text only"):
        assert phrase in described


# --- read_attachment ----------------------------------------------------------------------------


async def test_read_attachment_returns_the_readers_manifest(runtime, storage) -> None:
    runtime.handle_run = _printing(_MANIFEST)
    session = _session(runtime, storage)

    out = await _call(session, "read_attachment", file=".attachments/q3.xlsx")

    assert json.loads(out)["rows"] == 5
    assert session.tool_calls == 1
    (_, code), *_ = runtime.runs
    assert '"-I"' in code
    assert json.dumps(f"{SESSION_FILES_DIR}/{READER_NAME}") in code
    assert json.dumps(f"{SESSION_FILES_DIR}/q3.xlsx") in code
    assert ".attachments/" not in code


@pytest.mark.parametrize(
    "file", ["q3.xlsx", "/mnt/data/q3.xlsx", ".attachments/../q3.xlsx"], ids=str
)
async def test_a_path_outside_the_attachments_gets_plans_correction(
    runtime, storage, file: str
) -> None:
    with pytest.raises(ModelRetry):
        await _call(_session(runtime, storage), "read_attachment", file=file)

    assert runtime.calls == []


@pytest.mark.parametrize("file", [".attachments/q2.xlsx", ".attachments/receipt.pdf"], ids=str)
async def test_a_file_the_session_does_not_hold_is_named_and_never_reaches_it(
    runtime, storage, file: str
) -> None:
    """A name the chat no longer has, or a PDF that reaches the model directly. Placing again
    cannot produce it, so retrying would end in the sentence about an outage."""
    session = _session(runtime, storage)

    out = await _call(session, "read_attachment", file=file)

    assert json.loads(out)["error"]["code"] == "not_attached"
    assert runtime.calls == []
    assert session.tool_calls == 1


async def test_run_python_in_a_chat_with_no_file_never_reaches_the_runtime(
    runtime, storage
) -> None:
    """Every BIAL Chat carries the tool, so a chat with nothing attached starts no session."""
    session = AnalysisSession(
        conversation_id=uuid.uuid4(), files=(), storage=storage, runtime=runtime
    )

    out = await _call(session, "run_python", code="print(6 * 7)")

    assert "No file this tool can open is attached" in out
    assert runtime.calls == []
    assert session.tool_calls == 1


async def test_a_long_description_comes_back_whole(runtime, storage) -> None:
    """A workbook of a dozen ordinary sheets describes itself in well over 20,000 characters."""
    manifest = json.dumps({"ok": True, "file": "q3.xlsx", "sheets": ["x" * 60] * 1_000})
    runtime.handle_run = _printing(manifest)

    out = await _call(_session(runtime, storage), "read_attachment", file=".attachments/q3.xlsx")

    assert out == manifest


async def test_a_description_the_service_cut_is_too_large_not_broken_json(
    runtime, storage
) -> None:
    """The service cuts output at the limit without a marker, and a cut description is not JSON."""
    runtime.handle_run = _printing('{"ok": true, "sheets": ["' + "x" * ANALYSIS_READ_STREAM_LIMIT)

    out = await _call(_session(runtime, storage), "read_attachment", file=".attachments/q3.xlsx")

    assert json.loads(out) == {"ok": False, "error": {"code": "too_large"}}


async def test_a_file_lost_mid_reply_is_placed_again_and_read(runtime, storage) -> None:
    """Azure can remove an idle session between two calls of one reply, and a call then quietly
    opens an empty one. The reader saying `missing` is how that shows, and placement repairs it."""
    lost: list[bool] = []

    def run(session_id: str, _code: str) -> Execution:
        if not lost:
            lost.append(True)
            runtime.remove(session_id)
            return Execution(succeeded=True, stdout=_MISSING, stderr="")
        held = "q3.xlsx" in runtime.files.get(session_id, {})
        return Execution(succeeded=True, stdout=_MANIFEST if held else _MISSING, stderr="")

    runtime.handle_run = run
    session = _session(runtime, storage)

    out = await _call(session, "read_attachment", file=".attachments/q3.xlsx")

    assert json.loads(out)["rows"] == 5
    assert len(runtime.runs) == 2
    assert session.tool_calls == 1


async def test_a_second_miss_is_unavailable_after_one_retry(runtime, storage) -> None:
    runtime.handle_run = _printing(_MISSING)

    out = await _call(_session(runtime, storage), "read_attachment", file=".attachments/q3.xlsx")

    assert json.loads(out) == {"ok": False, "error": {"code": "unavailable"}}
    assert len(runtime.runs) == 2


async def test_a_reader_run_that_raises_in_the_session_is_unavailable(runtime, storage) -> None:
    runtime.handle_run = _printing(
        "", "OSError: [Errno 12] Cannot allocate memory\n", succeeded=False
    )

    out = await _call(_session(runtime, storage), "read_attachment", file=".attachments/q3.xlsx")

    assert json.loads(out) == {"ok": False, "error": {"code": "unavailable"}}


async def test_a_read_that_outlives_its_deadline_deletes_the_session(runtime, storage) -> None:
    runtime.run_times_out = True
    session = _session(runtime, storage)

    out = await _call(session, "read_attachment", file=".attachments/q3.xlsx")

    assert json.loads(out) == {"ok": False, "error": {"code": "timeout"}}
    assert runtime.operations("delete_session") == [session.conversation_id.hex]


async def test_a_failed_read_is_logged_by_chat_and_suffix_never_by_name(runtime, storage) -> None:
    def explode(_session_id: str, _code: str) -> Execution:
        raise KeyError("properties")

    runtime.handle_run = explode
    session = _session(runtime, storage, _file(storage, "salaries-2026.xlsx"))

    with capture_logs() as logs:
        await _call(session, "read_attachment", file=".attachments/salaries-2026.xlsx")

    failed = [entry for entry in logs if entry["event"] == "analysis_read_failed"]
    named = [entry for entry in logs if entry["event"] == "attachment_read_named_failure"]
    assert [(e["conversation_id"], e["suffix"], e["error"]) for e in failed] == [
        (str(session.conversation_id), ".xlsx", "KeyError")
    ]
    assert [(e["conversation_id"], e["code"]) for e in named] == [
        (str(session.conversation_id), "unavailable")
    ]
    assert "salaries" not in repr(logs)


# --- the reader as it runs in the session -------------------------------------------------------
#
# The code `read_attachment` sends is only ever executed by the session, so these run it here,
# against a stand-in reader in a local directory, to prove what each way the process ends prints.


_STAND_INS = {
    "manifest": (
        "import json, sys\n"
        "print(json.dumps({'ok': True, 'argv': sys.argv[1:], 'isolated': sys.flags.isolated}))\n"
    ),
    "killed": "import os, signal\nos.kill(os.getpid(), signal.SIGKILL)\n",
    "silent": "",
}


def _locally(directory: Path) -> Callable[[str, str], Execution]:
    def run(_session_id: str, code: str) -> Execution:
        done = subprocess.run(
            [sys.executable, "-c", code],
            cwd=directory,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        return Execution(succeeded=done.returncode == 0, stdout=done.stdout, stderr=done.stderr)

    return run


@pytest.mark.parametrize(
    ("stand_in", "code"),
    [("killed", "too_large"), ("silent", "unreadable"), (None, "unavailable")],
    ids=["killed", "silent", "no-reader"],
)
async def test_a_reader_that_dies_answers_with_a_named_code(
    runtime, storage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stand_in, code: str
) -> None:
    monkeypatch.setattr(analysis_tools, "SESSION_FILES_DIR", str(tmp_path))
    if stand_in is not None:
        (tmp_path / READER_NAME).write_text(_STAND_INS[stand_in])
    runtime.handle_run = _locally(tmp_path)

    out = await _call(_session(runtime, storage), "read_attachment", file=".attachments/q3.xlsx")

    assert json.loads(out)["error"]["code"] == code


async def test_the_reader_runs_isolated_over_the_sessions_copy(
    runtime, storage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(analysis_tools, "SESSION_FILES_DIR", str(tmp_path))
    (tmp_path / READER_NAME).write_text(_STAND_INS["manifest"])
    runtime.handle_run = _locally(tmp_path)

    out = await _call(_session(runtime, storage), "read_attachment", file=".attachments/q3.xlsx")

    assert json.loads(out) == {"ok": True, "argv": [f"{tmp_path}/q3.xlsx"], "isolated": 1}


async def test_the_reader_leaves_no_name_among_the_models_variables(
    runtime, storage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The session's interpreter keeps the model's variables between calls, so a read that left
    `path` or `json` behind would change what the model's next code sees."""
    monkeypatch.setattr(analysis_tools, "SESSION_FILES_DIR", str(tmp_path))
    (tmp_path / READER_NAME).write_text(_STAND_INS["manifest"])
    runtime.handle_run = _printing(_MANIFEST)
    await _call(_session(runtime, storage), "read_attachment", file=".attachments/q3.xlsx")
    (_, code), *_ = runtime.runs

    left = _locally(tmp_path)("", code + "\nprint([n for n in dir() if not n.startswith('__')])")

    assert left.stdout.splitlines()[-1] == "[]"
