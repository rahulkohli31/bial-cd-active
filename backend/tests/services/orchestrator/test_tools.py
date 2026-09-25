"""The five tools + the fail-closed write guard + 422→ModelRetry enrichment.

Driven through `conftest.build_tool_agent()` + a capturing `FunctionModel` + `FakeSandbox` — the
real reflection path, not a hand-called function. The agent is LOCAL because the module-level
`build_agent` was the deleted standalone harness's; `sandbox_toolset` — the thing actually under
test here — is the same factory a live Write chat turn composes its surface from, so the driver
still exercises the shipping tool bodies. See `build_tool_agent`'s docstring."""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    ModelMessage,
    ModelResponse,
    ToolCallPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from src.api.v1.conversations.schemas import StepFrame
from src.core import prompt_blocks
from src.db.models.conversation import ChatKind
from src.db.models.harness_counter import HarnessCounter
from src.services.messages.projection import long_operation_line
from src.services.orchestrator import constants
from src.services.orchestrator import tools as tools_module
from src.services.orchestrator.deps import HeldOutput, SandboxSession
from src.services.orchestrator.tools import (
    OUTPUT_NO_LONGER_HELD,
    _is_predictable_noise,
    _redact_command_output,
    _the_command_lied,
)
from src.services.sandbox import (
    ExecResult,
    FileOp,
    FileResult,
    FileView,
    SandboxError,
    SandboxGoneError,
    SandboxHandle,
)
from src.services.turns import engine as engine_module
from src.services.turns.engine import _TurnState
from tests.fakes import ToolDeps
from tests.services.orchestrator.conftest import build_tool_agent
from tests.services.orchestrator.fake_sandbox import FAKE_SUPERVISOR_TOKEN, FakeSandbox
from tests.services.orchestrator.model_harness import text_turn, tool_turn

_tool_agent = build_tool_agent()
"""One agent for the module, mirroring the singleton these tests used to drive: no bound model
(each run passes its own `FunctionModel`), so construction never needs a configured Foundry.

Named for what it is rather than for what it replaced — the deleted name is not resurrected as a
live identifier here."""

# Repo-root/sandbox — the real golden template and supervisor, read by the marker-drift pins
# below. This file is backend/tests/services/orchestrator/test_tools.py, so parents[4] is the
# repo root.
_TEMPLATE_ROOT = Path(__file__).resolve().parents[4] / "sandbox" / "template"
_SUPERVISOR_APP = Path(__file__).resolve().parents[4] / "sandbox" / "supervisor" / "app.py"

_TOOL_NAMES = {
    "read_file",
    "write_file",
    "edit_file",
    "insert_lines",
    "declare_done",
    "run_command",
    # The slice handle is a REGISTERED TOOL, not a capability described in a prompt — and it
    # is registered here, on the sandbox toolset, because Write is the only mode that runs
    # commands. `test_toolsets.py` asserts the mode half against `toolsets_for_kind` directly.
    "fetch_output_slice",
    # The composite is registered here too — one call for the generate + migrate sequence.
    "apply_schema_change",
}


def _all_text(messages: list[ModelMessage]) -> str:
    """Flatten every string content across the model input — user prompts, tool returns, and the
    retry-prompt parts a ModelRetry produces."""
    out: list[str] = []
    for message in messages:
        for part in getattr(message, "parts", []):
            content = getattr(part, "content", None)
            if isinstance(content, str):
                out.append(content)
            elif isinstance(content, list):
                out.extend(item for item in content if isinstance(item, str))
    return "\n".join(out)


def _capturing_model(turns: list[ModelResponse], captured: dict[str, Any]) -> FunctionModel:
    iterator = iter(turns)

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        captured["tool_names"] = {t.name for t in info.function_tools}
        # THE DESCRIPTIONS AS THE MODEL RECEIVES THEM, not the docstrings as written. The
        # framework builds one from the other, so a test that read `declare_done.__doc__`
        # would be asserting on a string the model never sees.
        captured["tool_descriptions"] = {t.name: t.description or "" for t in info.function_tools}
        captured.setdefault("incoming", []).append(_all_text(messages))
        return next(iterator, text_turn("done"))

    return FunctionModel(respond)


def _deps(fake: FakeSandbox) -> ToolDeps:
    return ToolDeps(
        sandbox=SandboxSession(
            sandbox_client=fake,
            handle=fake.handle(),
            app_id=uuid.uuid4(),
        ),
        user_id=uuid.uuid4(),
    )


async def _run(fake: FakeSandbox, turns: list[ModelResponse]) -> dict[str, Any]:
    captured: dict[str, Any] = {}
    model = _capturing_model(turns, captured)
    result = await _tool_agent.run("build the app", deps=_deps(fake), model=model)
    captured["output"] = result.output
    captured["all_incoming"] = "\n".join(captured.get("incoming", []))
    return captured


async def test_registered_tool_surface_is_the_open_sandbox_set() -> None:
    fake = FakeSandbox()
    captured = await _run(fake, [text_turn("nothing to do")])
    # The open-sandbox surface: the five file tools + run_command (the vibe-coding pivot).
    assert captured["tool_names"] == _TOOL_NAMES
    assert "run_command" in captured["tool_names"]


async def test_read_file_returns_numbered_lines() -> None:
    fake = FakeSandbox(seed_files={"app/records/page.tsx": "alpha\nbeta\ngamma"})
    captured = await _run(
        fake, [tool_turn("read_file", {"path": "app/records/page.tsx"}), text_turn()]
    )
    assert "1\talpha" in captured["all_incoming"]
    assert "3\tgamma" in captured["all_incoming"]


async def test_read_file_refuses_the_ignore_set() -> None:
    fake = FakeSandbox()
    captured = await _run(
        fake, [tool_turn("read_file", {"path": "node_modules/react/index.js"}), text_turn()]
    )
    assert "not readable" in captured["all_incoming"]


class _MalformedViewSandbox(FakeSandbox):
    """A sandbox client whose `view` returns a FileResult MISSING the contractually-required
    `content` key (a malformed supervisor response); every other op behaves normally."""

    async def files(self, handle: SandboxHandle, op: FileOp) -> FileResult:
        if isinstance(op, FileView):
            return FileResult(ok=True, detail={})  # no `content`
        return await super().files(handle, op)


async def test_read_file_surfaces_a_malformed_response() -> None:
    fake = _MalformedViewSandbox(seed_files={"app/x.tsx": "hi"})
    captured = await _run(fake, [tool_turn("read_file", {"path": "app/x.tsx"}), text_turn()])
    # A missing `content` key surfaces as a retry — it must NOT masquerade as a legitimately-empty
    # file (fail-first: a malformed backend response should be loud).
    assert "returned no content" in captured["all_incoming"]


async def test_read_file_clamps_an_unbounded_view() -> None:
    big = "\n".join(f"row-{i}" for i in range(1, 1001))  # 1000 lines
    fake = FakeSandbox(seed_files={"app/big.tsx": big})
    captured = await _run(fake, [tool_turn("read_file", {"path": "app/big.tsx"}), text_turn()])
    # Bounded to VIEW_MAX_LINES (400) — line 500 is never surfaced.
    assert "400\trow-400" in captured["all_incoming"]
    assert "row-500" not in captured["all_incoming"]


async def test_read_file_minus_one_end_reads_to_end_of_file() -> None:
    # The docstring promise "-1 = end of file" must round-trip NON-EMPTY (the pre-fix
    # supervisor computed an empty range for -1; the fake mirrors the fixed semantics).
    fake = FakeSandbox(seed_files={"app/x.tsx": "alpha\nbeta\ngamma"})
    captured = await _run(
        fake,
        [tool_turn("read_file", {"path": "app/x.tsx", "view_range": [1, -1]}), text_turn()],
    )
    assert "1\talpha" in captured["all_incoming"]
    assert "3\tgamma" in captured["all_incoming"]


async def test_read_file_minus_one_end_is_still_budget_clamped() -> None:
    # -1 must not become an unbounded read: the VIEW_MAX_LINES clamp applies to it too
    # (previously only the end != -1 branch clamped).
    big = "\n".join(f"row-{i}" for i in range(1, 1001))  # 1000 lines
    fake = FakeSandbox(seed_files={"app/big.tsx": big})
    captured = await _run(
        fake,
        [tool_turn("read_file", {"path": "app/big.tsx", "view_range": [1, -1]}), text_turn()],
    )
    assert "400\trow-400" in captured["all_incoming"]
    assert "row-500" not in captured["all_incoming"]


@pytest.mark.parametrize(
    "path",
    [
        "app/records/page.tsx",
        "components/x/widget.tsx",
        "lib/util.ts",
        # The open-sandbox surface: config + schema + root files now land through the tool.
        "package.json",
        "next.config.ts",
        "db/schema.ts",
        "components/ui/button.tsx",
    ],
)
async def test_write_allowed_across_the_open_surface(path: str) -> None:
    fake = FakeSandbox()
    await _run(
        fake, [tool_turn("write_file", {"path": path, "file_text": "export const x = 1;\n"})]
    )
    assert fake.workspace[path] == "export const x = 1;\n"


@pytest.mark.parametrize("path", [".git/config", ".git/hooks/pre-push"])
async def test_write_denied_paths_raise_model_retry_and_never_touch_files(path: str) -> None:
    fake = FakeSandbox()
    captured = await _run(fake, [tool_turn("write_file", {"path": path, "file_text": "PWNED"})])
    # The guard fired before files(): nothing was written…
    assert path not in fake.workspace
    assert "PWNED" not in "".join(fake.workspace.values())
    # …and the model was told why (a ModelRetry).
    assert "cannot be written" in captured["all_incoming"]


async def test_edit_file_bad_match_enriches_into_a_model_retry() -> None:
    fake = FakeSandbox(seed_files={"app/records/page.tsx": "one\ntwo\nthree"})
    captured = await _run(
        fake,
        [
            tool_turn(
                "edit_file",
                {"path": "app/records/page.tsx", "old_str": "not-in-file", "new_str": "x"},
            ),
            text_turn(),
        ],
    )
    # The enrichment surfaces the current numbered file + the exactly-once rule.
    assert "match EXACTLY ONCE" in captured["all_incoming"]
    assert "1\tone" in captured["all_incoming"]


async def test_str_replace_bad_then_fixed_recovers_in_run() -> None:
    fake = FakeSandbox(seed_files={"app/x.tsx": "aaa\nbbb\nccc\n"})
    captured = await _run(
        fake,
        [
            tool_turn("edit_file", {"path": "app/x.tsx", "old_str": "zzz", "new_str": "q"}),  # bad
            tool_turn(
                "edit_file", {"path": "app/x.tsx", "old_str": "bbb", "new_str": "BBB"}
            ),  # fixed
            text_turn("done"),
        ],
    )
    # The middle ModelRetry was handled in-run and the corrected edit landed.
    assert fake.workspace["app/x.tsx"] == "aaa\nBBB\nccc\n"
    assert captured["output"] == "done"


async def test_no_tool_leaks_the_supervisor_token() -> None:
    fake = FakeSandbox()
    # A failing read (missing file) plus a denied write — neither must render handle.token.
    captured = await _run(
        fake,
        [
            tool_turn("read_file", {"path": "app/missing.tsx"}),
            tool_turn("write_file", {"path": ".git/config", "file_text": "x"}),
            text_turn(),
        ],
    )
    assert FAKE_SUPERVISOR_TOKEN not in captured["all_incoming"]


async def test_declare_done_sets_the_signal_and_summary() -> None:
    fake = FakeSandbox()
    deps = _deps(fake)
    captured: dict[str, Any] = {}
    model = _capturing_model(
        [tool_turn("declare_done", {"summary": "built it"}), text_turn()], captured
    )
    await _tool_agent.run("build", deps=deps, model=model)
    assert deps.sandbox.done_requested is True
    assert deps.sandbox.done_summary == "built it"


async def test_the_declare_done_description_the_model_reads_says_the_turn_ends() -> None:
    """★ THE TOOL'S OWN DESCRIPTION IS HALF THE BEHAVIOUR.

    `declare_done` used to promise the opposite of what it now does ("This does NOT end the
    build on its own"), and a model that believes it gets one more turn keeps its closing
    message out of `summary`, saved for prose the run has already stopped rendering. That is
    the exact failure this test exists to prevent, so the description is asserted with the same
    seriousness as the code.

    Asserted on the description the toolset registers — the text pydantic-ai actually sends —
    rather than on `__doc__`, because the framework composes one from the other and only one of
    them reaches the model.

    Mutation check: restore either retired sentence and the two absence asserts go red; drop
    the diagnostic clause and the liveness assert does."""
    fake = FakeSandbox()
    captured = await _run(fake, [text_turn("nothing to do")])
    description = captured["tool_descriptions"]["declare_done"]
    # Wrapped at 96 columns in the source, so every assertion below is made against the text
    # with its line breaks collapsed — otherwise a phrase straddling a wrap silently misses.
    lowered = " ".join(description.lower().split())

    # THE TERMINAL CONDITION, STATED — and stated as conditional on the check, which is what
    # keeps it true (the conjunction with the verdict is untouched).
    assert "ends the turn" in lowered
    assert "passing check" in lowered
    # …and the summary is named as what the user reads, not as a note for the record.
    assert "summary" in lowered and "the user reads" in lowered

    # THE RETIRED PROMISES OF A FOLLOW-UP ROUND-TRIP — zero hits.
    assert "does not end the build" not in lowered
    assert "type-check the app" not in lowered

    # THE REPAIR ARM'S PROMISE IS STILL TRUE AND MUST STILL BE MADE (the liveness half): a red
    # verdict really does hand the model the diagnostic and carry on.
    assert "diagnostic" in lowered

    # THE PROMPT'S TOOL-SURFACE LINE IS GENERATED FROM THE FIRST SENTENCE, so the first
    # sentence has to stand alone as user-visible prompt copy.
    first_sentence = lowered.split(".")[0]
    assert "declare the build finished" in first_sentence
    assert "summary" in first_sentence


async def test_declare_done_tells_the_model_its_summary_is_the_last_word() -> None:
    """★ THE RETURN STRING MOVED WITH THE BEHAVIOUR TOO.

    `declare_done` is terminal on the passing arm, so its return must say which of the two arms
    is terminal and which is not — both truthfully. Anything that reads as an invitation to stand
    by for a second act makes a model hold its closing message back for a reply it never gets.

    Asserted on what the MODEL received back (the tool return in its next input), not on the
    function's return value, for the same reason as the description test above."""
    fake = FakeSandbox()
    deps = _deps(fake)
    captured: dict[str, Any] = {}
    model = _capturing_model(
        [tool_turn("declare_done", {"summary": "You can add visitors and check them in."})],
        captured,
    )
    await _tool_agent.run("build", deps=deps, model=model)
    returned = "\n".join(captured["incoming"]).lower()

    assert "this turn ends here" in returned
    assert "nothing further is asked of you" in returned
    # The repair arm survives verbatim — a red check still hands over the diagnostic.
    assert "you will get the diagnostic to fix" in returned
    assert "will now type-check the app" not in returned


# --- run_command -----------------------------------


async def test_run_command_returns_exit_and_redacted_output() -> None:
    fake = FakeSandbox()
    fake.queue_commands(ExecResult(stdout="added 1 package in 2s", stderr="", exit=0))
    captured = await _run(
        fake,
        [tool_turn("run_command", {"command": ["npm", "install", "zod"]}), text_turn()],
    )
    assert "exit code: 0" in captured["all_incoming"]
    assert "added 1 package in 2s" in captured["all_incoming"]
    assert fake.command_calls == [["npm", "install", "zod"]]


async def test_run_command_nonzero_exit_is_a_normal_result_not_an_exception() -> None:
    # An npm 404 / peer-dep conflict is exit != 0 — it must come back as a NORMAL tool result the
    # model can read and re-feed, never a raised exception.
    fake = FakeSandbox()
    fake.queue_commands(ExecResult(stdout="", stderr="npm ERR! 404 Not Found: nosuchpkg", exit=1))
    captured = await _run(
        fake,
        [tool_turn("run_command", {"command": ["npm", "install", "nosuchpkg"]}), text_turn("ok")],
    )
    assert "exit code: 1" in captured["all_incoming"]
    assert "npm ERR! 404" in captured["all_incoming"]
    # The run continued to the next turn — the failure did not crash the build.
    assert captured["output"] == "ok"


async def test_run_command_sandbox_error_becomes_a_model_retry_in_loop() -> None:
    # A supervisor 504 (incl. an install-timeout) surfaces as SandboxError → converted to a
    # ModelRetry and re-fed in-loop, never a hard build failure.
    fake = FakeSandbox()
    fake.queue_exec_errors(SandboxError("exec timed out after 600s"))
    captured = await _run(
        fake,
        [
            tool_turn("run_command", {"command": ["npm", "install", "big-pkg"]}),
            text_turn("healed"),
        ],
    )
    assert "could not run" in captured["all_incoming"]
    assert captured["output"] == "healed"  # the loop recovered rather than crashing


async def test_run_command_sandbox_gone_escalates() -> None:
    # Only SandboxGoneError propagates out of the run (→ run_build's sandbox_gone escalation).
    fake = FakeSandbox()
    fake.queue_exec_errors(SandboxGoneError("the sandbox is gone"))
    with pytest.raises(SandboxGoneError):
        await _run(fake, [tool_turn("run_command", {"command": ["npm", "install"]})])


# The bound depends on WHAT the command is, and one global value could not satisfy both halves.
# A `drizzle-kit generate` that blocks on an interactive prompt has to be caught in a fraction of
# the time a cold-base `npm install` needs — and this repo's own constant documents that install
# "routinely" takes the full 600s, so lowering a single global would kill healthy builds instead.


@pytest.mark.parametrize(
    ("command", "slow"),
    [
        (["npm", "install", "zod"], True),
        (["npm", "ci"], True),
        (["npx", "tsc", "--noEmit"], True),
        (["npm", "run", "build"], True),
        # …and the ones that must NOT get ten minutes to hang in:
        (["npx", "drizzle-kit", "generate", "--name", "add_visits"], False),
        (["npm", "run", "lint"], False),
        (["npm", "run", "db:migrate"], False),
        (["ls", "app"], False),
    ],
)
async def test_run_command_picks_its_timeout_by_command_class(
    command: list[str], slow: bool
) -> None:
    fake = FakeSandbox()
    fake.queue_commands(ExecResult(stdout="ok", stderr="", exit=0))
    await _run(fake, [tool_turn("run_command", {"command": command})])
    expected = (
        constants.RUN_COMMAND_SLOW_TIMEOUT_S if slow else constants.RUN_COMMAND_DEFAULT_TIMEOUT_S
    )
    assert fake.command_timeouts == [expected]


def test_the_short_bound_catches_the_observed_wedge_and_the_long_one_does_not() -> None:
    """The numbers are the whole point of splitting the bound, so pin them against the real
    observation rather than leaving them as two arbitrary constants."""
    observed_wedge_seconds = 249
    assert constants.RUN_COMMAND_DEFAULT_TIMEOUT_S < observed_wedge_seconds
    assert constants.RUN_COMMAND_SLOW_TIMEOUT_S > observed_wedge_seconds
    # Both stay under the supervisor's 900s hard cap, and neither is the tsc verify budget.
    assert constants.RUN_COMMAND_SLOW_TIMEOUT_S < 900
    assert constants.RUN_COMMAND_SLOW_TIMEOUT_S != constants.EXEC_TIMEOUT_S
    assert constants.RUN_COMMAND_DEFAULT_TIMEOUT_S != constants.EXEC_TIMEOUT_S


@pytest.mark.parametrize(
    "secret_line",
    [
        "your app key is bial_AbCdEf0123456789ghIjKlMnOpQr and nothing else",
        'FOO_SECRET="super-secret-value-do-not-leak"',
        "DATABASE_URL=postgres://user:hunter2@db:5432/app",
    ],
)
async def test_run_command_output_is_secret_redacted(secret_line: str) -> None:
    # run_command is the first tool to egress captured stdout — a credential-shaped value must be
    # masked before it re-enters the model context.
    fake = FakeSandbox()
    fake.queue_commands(ExecResult(stdout=secret_line, stderr="", exit=0))
    captured = await _run(fake, [tool_turn("run_command", {"command": ["env"]}), text_turn()])
    for leaked in (
        "bial_AbCdEf0123456789ghIjKlMnOpQr",
        "super-secret-value-do-not-leak",
        "hunter2",
    ):
        assert leaked not in captured["all_incoming"]
    assert "***" in captured["all_incoming"]


def test_redact_command_output_caps_raw_input_before_redacting() -> None:
    # ReDoS guard: raw output is sliced to REDACT_INPUT_MAX_CHARS BEFORE the redactor runs, so a
    # trailing secret beyond the cap is dropped (never scanned) and the result stays bounded. The
    # ordering is preserved and only the second cap has moved: the artifact is now head + notice +
    # tail, so the bound is the budget plus the notice rather than the budget plus a marker.
    trailing_secret = "bial_ThisIsPastTheInputCap0123456789"
    raw = ("A" * (constants.REDACT_INPUT_MAX_CHARS + 5_000)) + trailing_secret
    out = _redact_command_output(raw, budget=constants.RUN_COMMAND_OUTPUT_MAX_CHARS)
    assert trailing_secret not in out
    assert "bial_ThisIsPast" not in out
    assert len(out) <= constants.RUN_COMMAND_OUTPUT_MAX_CHARS + 400


async def test_run_command_never_leaks_the_supervisor_token() -> None:
    fake = FakeSandbox()
    fake.queue_exec_errors(SandboxError("boom"))
    captured = await _run(
        fake,
        [tool_turn("run_command", {"command": ["npm", "install"]}), text_turn()],
    )
    assert FAKE_SUPERVISOR_TOKEN not in captured["all_incoming"]


# --- the commit reminder that no longer exists -----------------------------------------
#
# Who commits the tree, and when, now lives in `services/build_sessions/snapshot.py`, not here.
# The enforcer had to go WITH the instruction: `_note_write_and_maybe_remind` lived in
# `tools.py`, the toolset both agents build from, while the instruction it enforced lived only
# in the Write segment — deleting one and not the other means a `<system-reminder>` telling the
# model to commit a slice nothing ever asked it to commit rides every third write.


async def test_no_reminder_rides_a_write_result_any_more() -> None:
    """★ Asserted at THREE writes — `COMMIT_REMINDER_AFTER_WRITES`, the count at which the
    reminder used to fire — so this is a real observation rather than a test that never reached
    the trigger. A fourth and fifth write follow, because the retired counter reset itself on
    firing and a cadence bug could otherwise hide in the second cycle.

    Mutation check: restore `_note_write_and_maybe_remind` on `write_file` and this goes red."""
    fake = FakeSandbox()
    captured = await _run(
        fake,
        [
            tool_turn("write_file", {"path": f"app/{n}.tsx", "file_text": "x"})
            for n in ("one", "two", "three", "four", "five")
        ]
        + [text_turn()],
    )
    for view in captured["incoming"]:
        assert "<system-reminder>" not in view
    assert "commit" not in captured["all_incoming"].lower()
    # LIVENESS — the write results themselves still say what happened, so the absence above is
    # the reminder being gone and not the tool returning nothing.
    assert "Wrote `app/three.tsx`." in captured["all_incoming"]


async def test_a_git_commit_through_run_command_is_still_an_ordinary_command() -> None:
    """The agent is no longer TOLD to commit; `run_command` is still a real shell, so a commit it
    chooses to run must behave like any other command. The retired `_is_git_commit` sniffer is
    gone with the counter it reset, and nothing replaces it — no special-casing of `git` on the
    exec path."""
    fake = FakeSandbox()
    captured = await _run(
        fake,
        [
            tool_turn("write_file", {"path": "app/one.tsx", "file_text": "a"}),
            tool_turn("run_command", {"command": ["git", "commit", "-m", "whatever"]}),
            tool_turn("write_file", {"path": "app/two.tsx", "file_text": "b"}),
            text_turn(),
        ],
    )
    assert "<system-reminder>" not in captured["all_incoming"]
    assert ["git", "commit", "-m", "whatever"] in fake.command_calls


# ═══════════════════════════════════════════════════════════════════════════════════════
# cap tool output by usefulness, not by a fixed head
# ═══════════════════════════════════════════════════════════════════════════════════════
#
# A head-only cap threw away a failure's message while keeping its irrelevant top. Fixed
# exit-code-conditional (summarise a success, dump a failure), keeping both ends and handing
# back a handle to what it cut.
#
# Mutation target: `test_a_secret_inside_the_elided_middle_is_never_retrievable` — delete the
# `scrub_untrusted` call in `_redacted_lines` and it goes red. Distinct from the boundary test
# below (a cut splitting a credential): this one is a secret sitting entirely inside an elided
# middle, the ordinary case, not an edge one.

_ERROR_AT_THE_VERY_END = "error TS2322: Type 'string' is not assignable to type 'number'."
_TRACE_TITLE_AT_THE_TOP = "FATAL: the migration runner refused to start"
_SECRET_IN_THE_MIDDLE = "DATABASE_PASSWORD=hunter2-super-secret"


def _long_output(*, lines: int = 140, middle: str | None = None) -> str:
    """A capture with a recognisable head and tail and an optional planted middle line."""
    body = [f"  at frame {n:03d} of a long and tedious stack ({'x' * 30})" for n in range(lines)]
    if middle is not None:
        body[len(body) // 2] = middle
    return "\n".join([_TRACE_TITLE_AT_THE_TOP, *body, _ERROR_AT_THE_VERY_END])


def _slice_call_in(text: str) -> re.Match[str] | None:
    """The `fetch_output_slice(...)` call a truncation notice printed, parsed as the model would
    copy it — the whole point of naming the tool and the handle INLINE."""
    return re.search(
        r'fetch_output_slice\(handle="([^"]+)", start_line=(\d+), end_line=(\d+)\)', text
    )


def _following_model(command: list[str], captured: dict[str, Any]) -> FunctionModel:
    """A model that runs `command` and then FOLLOWS whatever the truncation notice told it to
    call — no memory of any capability from the system prompt, just the line in front of it."""
    step = {"n": 0}

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        captured.setdefault("incoming", []).append(_all_text(messages))
        captured["tool_names"] = {t.name for t in info.function_tools}
        current, step["n"] = step["n"], step["n"] + 1
        if current == 0:
            return tool_turn("run_command", {"command": command})
        if current == 1:
            match = _slice_call_in(captured["incoming"][-1])
            assert match is not None, f"no slice call in:\n{captured['incoming'][-1]}"
            captured["notice_call"] = match.group(0)
            return tool_turn(
                "fetch_output_slice",
                {
                    "handle": match.group(1),
                    "start_line": int(match.group(2)),
                    "end_line": int(match.group(3)),
                },
            )
        return text_turn("done")

    return FunctionModel(respond)


async def _run_following(fake: FakeSandbox, command: list[str]) -> dict[str, Any]:
    captured: dict[str, Any] = {}
    result = await _tool_agent.run(
        "build the app", deps=_deps(fake), model=_following_model(command, captured)
    )
    captured["output"] = result.output
    captured["all_incoming"] = "\n".join(captured.get("incoming", []))
    captured["slice_result"] = captured["incoming"][-1]
    return captured


def test_a_failing_commands_output_is_dumped_and_a_succeeding_ones_is_summarised() -> None:
    """★ The asymmetric-budget rule, both arms measured against ONE capture.

    A success's shape is already known — it is a confirmation. A failure's payload is unknown by
    construction, which is what makes it a failure, so it gets four times the budget."""
    raw = _long_output(lines=900)
    failed = _redact_command_output(raw, budget=constants.output_budget_for_exit(1))
    passed = _redact_command_output(raw, budget=constants.output_budget_for_exit(0))
    assert len(failed) > len(passed)
    assert len(passed) <= constants.RUN_COMMAND_SUMMARY_MAX_CHARS + 400  # + the notice
    assert len(failed) <= constants.RUN_COMMAND_OUTPUT_MAX_CHARS + 400
    # And both were cut — a test where neither truncated would prove nothing about either.
    assert "elided" in failed
    assert "elided" in passed


def test_the_head_and_the_tail_both_survive_truncation() -> None:
    """★ THE WHOLE DEFECT, in one assertion pair. Head-only kept the first and lost the second."""
    out = _redact_command_output(_long_output(), budget=constants.RUN_COMMAND_SUMMARY_MAX_CHARS)
    assert out.startswith(_TRACE_TITLE_AT_THE_TOP)  # the message, at the TOP of a trace
    assert out.endswith(_ERROR_AT_THE_VERY_END)  # the failing assertion, at the very END
    assert "elided" in out


def test_the_truncation_notice_states_the_loss_and_names_the_tool_and_handle_inline() -> None:
    out = _redact_command_output(
        _long_output(), budget=constants.RUN_COMMAND_SUMMARY_MAX_CHARS, handle="out_deadbeef"
    )
    match = _slice_call_in(out)
    assert match is not None, out
    assert match.group(1) == "out_deadbeef"
    # HOW MUCH was lost, in both units, and where it sat in the whole.
    assert re.search(r"\[\.\.\. [\d,]+ lines \([\d,]+ characters\) elided — lines \d+-\d+ of", out)
    # The range the notice names is the range that is actually missing. Line 1 is the title, so
    # capture line `k` is `frame k-2` — the offset is spelled out rather than fudged, because a
    # notice naming a range that is off by one is worse than no notice at all.
    first, last = int(match.group(2)), int(match.group(3))
    assert f"  at frame {first - 3:03d} " in out  # the last line still SHOWN
    assert f"  at frame {first - 2:03d} " not in out  # the FIRST line elided
    assert f"  at frame {last - 2:03d} " not in out  # the LAST line elided


async def test_the_handle_fetches_the_named_middle_region_in_one_call() -> None:
    """★ ONE round-trip, driven by a model that knows nothing but the notice it was handed."""
    marker = "  at frame 070 of a long and tedious stack (the one the model came back for)"
    fake = FakeSandbox()
    fake.queue_commands(ExecResult(stdout=_long_output(middle=marker), stderr="", exit=0))
    captured = await _run_following(fake, ["npm", "run", "build"])
    assert marker not in captured["incoming"][1]  # elided from the run_command result …
    assert marker in captured["slice_result"]  # … and recovered by the handle it named


async def test_a_secret_inside_the_elided_middle_is_never_retrievable() -> None:
    """★ THE MUTATION TARGET (delete the `scrub_untrusted` call in `_redacted_lines` → red).

    The returned artifact only ever exposed an already-redacted head. A handle retains a SECOND
    artifact, and the part it holds is precisely the part no human read — so a buffer built from
    raw stdout is a direct path to a credential that was never shown and never masked. This is
    the ORDINARY case for a secret in a long capture, not a boundary one, which is why it is
    written separately from the boundary test below."""
    fake = FakeSandbox()
    fake.queue_commands(
        ExecResult(stdout=_long_output(middle=_SECRET_IN_THE_MIDDLE), stderr="", exit=0)
    )
    captured = await _run_following(fake, ["npm", "run", "env-dump"])
    # It really was in the elided middle: the slice fetched the region that held it …
    assert "DATABASE_PASSWORD" in captured["slice_result"]
    # … and what came back through the handle is masked, in the slice AND in every other thing
    # the model ever saw this run.
    assert "hunter2-super-secret" not in captured["all_incoming"]
    assert "hunter2" not in captured["all_incoming"]
    assert "DATABASE_PASSWORD=***" in captured["slice_result"]


def test_a_secret_spanning_the_truncation_boundary_is_not_re_exposed() -> None:
    """★ DISTINCT FROM THE ONE ABOVE: this is about the CUT, not the buffer.

    Redaction runs once over the whole capture BEFORE anything is sliced. Cut first and redact
    the pieces afterwards and a credential straddling the cut becomes two fragments that match
    none of the redactor's shapes — half a password, in the clear, in the head."""
    budget = constants.RUN_COMMAND_SUMMARY_MAX_CHARS
    # One pathological line, with the credential's VALUE sitting exactly across the head cut.
    spanning = ("A" * (budget // 2 - 31)) + f" {_SECRET_IN_THE_MIDDLE} " + ("B" * 5_000)
    out = _redact_command_output(spanning, budget=budget)
    assert "hunter2" not in out
    assert "hunter2-sup" not in out  # the fragment a cut-then-redact order would have left
    assert "DATABASE_PASSWORD=***" in out


async def test_an_unknown_handle_returns_the_plain_instruction_not_an_exception() -> None:
    """A `ModelRetry` here would spend the round-trip the tool exists to save, and there is
    nothing to self-correct: the buffer is gone because the turn moved on."""
    fake = FakeSandbox()
    captured = await _run(
        fake,
        [
            tool_turn(
                "fetch_output_slice",
                {"handle": "out_neverexisted", "start_line": 5, "end_line": 9},
            ),
            text_turn(),
        ],
    )
    assert OUTPUT_NO_LONGER_HELD in captured["all_incoming"]
    assert "retry" not in captured["all_incoming"].lower()


async def test_a_handle_from_a_previous_run_is_no_longer_held() -> None:
    """★ THE STATED LIFETIME, exercised rather than asserted about. The turn engine builds a
    fresh `SandboxSession` per turn (`turns/engine.py`, at `state.sandbox = SandboxSession(`), so
    the buffer dies with the turn —
    nothing here is persisted to the database or to blob."""
    fake = FakeSandbox()
    fake.queue_commands(ExecResult(stdout=_long_output(), stderr="", exit=0))
    first = await _run_following(fake, ["npm", "run", "build"])
    handle = _slice_call_in(first["incoming"][1])
    assert handle is not None

    # A SECOND run, with its own session — exactly what the engine does at the start of a turn.
    second = await _run(
        fake,
        [
            tool_turn(
                "fetch_output_slice",
                {"handle": handle.group(1), "start_line": 40, "end_line": 60},
            ),
            text_turn(),
        ],
    )
    assert OUTPUT_NO_LONGER_HELD in second["all_incoming"]


def test_the_held_output_ring_is_bounded() -> None:
    """★ The ring's cap, asserted as a number rather than trusted to a comment: the oldest handle
    is evicted, never the newest, because the newest is the one the model was just handed."""
    session = SandboxSession(
        sandbox_client=FakeSandbox(), handle=FakeSandbox().handle(), app_id=uuid.uuid4()
    )
    handles = [f"out_{n:08x}" for n in range(constants.OUTPUT_SLICE_HANDLES_PER_TURN + 3)]
    for name in handles:
        session.hold_output(name, HeldOutput(command="npm run build", lines=("a", "b")))
    assert len(session.held_outputs) == constants.OUTPUT_SLICE_HANDLES_PER_TURN
    assert handles[0] not in session.held_outputs  # oldest evicted
    assert handles[-1] in session.held_outputs  # newest kept


async def test_a_very_large_capture_does_not_retain_unbounded_memory() -> None:
    """The other half of the bound: ONE entry is capped too, by the same ReDoS input cap the
    redactor runs under. 8 handles x 32k is the whole ceiling a live turn can reach."""
    fake = FakeSandbox()
    huge = "\n".join(f"line {n} {'y' * 200}" for n in range(5_000))  # ~1MB
    fake.queue_commands(ExecResult(stdout=huge, stderr="", exit=1))
    deps = _deps(fake)
    await _tool_agent.run(
        "build",
        deps=deps,
        model=_capturing_model(
            [tool_turn("run_command", {"command": ["npm", "run", "build"]}), text_turn()], {}
        ),
    )
    held = list(deps.sandbox.held_outputs.values())
    assert len(held) == 1
    assert len("\n".join(held[0].lines)) <= constants.REDACT_INPUT_MAX_CHARS


def test_predictable_noise_goes_but_vulnerability_and_deprecation_signal_stays() -> None:
    """★ WHERE THE NOISE BOUNDARY IS DRAWN. Solicitations and progress frames are dropped because
    nothing a model can do about them exists; anything naming a deprecation, a vulnerability, an
    audit or a CVE is kept, because deciding a citizen's app may keep a deprecated or vulnerable
    dependency is not a decision the output formatter gets to make."""
    noise = [
        "npm notice New major version of npm available! 10.8.2 -> 11.0.0",
        "npm notice To update run: npm install -g npm@11.0.0",
        "12 packages are looking for funding",
        "  run `npm fund` for details",
        "Progress: resolved 812, reused 800, downloaded 0, added 0",
        "⠙ idealTree:app: sill idealTree buildDeps",
        "[####______]",
    ]
    signal = [
        "npm warn deprecated request@2.88.2: request has been deprecated",
        "3 vulnerabilities (1 moderate, 2 high)",
        "# npm audit report",
        "severity: high",
        "(node:71) [DEP0040] DeprecationWarning: The `punycode` module is deprecated.",
        "GHSA-1234: see the advisory for CVE-2026-0001",
    ]
    for line in noise:
        assert _is_predictable_noise(line), line
    for line in signal:
        assert not _is_predictable_noise(line), line
    kept = _redact_command_output("\n".join(noise + signal), budget=16_000)
    assert kept == "\n".join(signal)


# --- adoption instrumentation ------------------------------------------------------
#
# THE QUESTION THESE ANSWER is not "does the tool work" but "did it change anything": a slice
# fetch is the round-trip the handle SAVED, and an identical command re-run inside one turn is
# the round-trip it did not. They are only readable as a pair, so they are emitted as a pair.
# `count` owns its own session and swallows everything, so patching its module attribute is
# the honest seam — the tool-boundary call sites are what is under test, not the writer.


@pytest.fixture
def counted(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    recorded: list[str] = []

    async def _record(counter: Any, **_kwargs: Any) -> None:
        recorded.append(str(counter))

    monkeypatch.setattr("src.services.build_sessions.counters.count", _record)
    return recorded


async def test_truncation_is_counted_and_only_when_output_is_actually_cut(
    counted: list[str],
) -> None:
    fake = FakeSandbox()
    fake.queue_commands(ExecResult(stdout="all good", stderr="", exit=0))
    await _run(fake, [tool_turn("run_command", {"command": ["npm", "test"]}), text_turn()])
    assert counted == []  # nothing was cut, so nothing is counted

    fake.queue_commands(ExecResult(stdout=_long_output(lines=900), stderr="", exit=1))
    await _run(fake, [tool_turn("run_command", {"command": ["npm", "test"]}), text_turn()])
    assert counted == [HarnessCounter.OUTPUT_TRUNCATED]


async def test_a_slice_fetch_is_counted_and_a_dead_handle_is_not(counted: list[str]) -> None:
    fake = FakeSandbox()
    fake.queue_commands(ExecResult(stdout=_long_output(), stderr="", exit=0))
    await _run_following(fake, ["npm", "run", "build"])
    assert counted == [HarnessCounter.OUTPUT_TRUNCATED, HarnessCounter.OUTPUT_SLICE_FETCHED]

    counted.clear()
    await _run(
        fake,
        [
            tool_turn(
                "fetch_output_slice", {"handle": "out_gone", "start_line": 1, "end_line": 2}
            ),
            text_turn(),
        ],
    )
    assert counted == []  # a handle that resolved to nothing fetched nothing


async def test_a_repeat_run_is_counted_on_the_repeat_only(counted: list[str]) -> None:
    """★ The other half of the adoption pair, and the one that has to be exact: counted on the
    SECOND identical command, never on the first, and never on a merely similar one."""
    fake = FakeSandbox()
    for _ in range(3):
        fake.queue_commands(ExecResult(stdout="ok", stderr="", exit=0))
    await _run(
        fake,
        [
            tool_turn("run_command", {"command": ["npm", "run", "lint"]}),
            tool_turn("run_command", {"command": ["npm", "run", "build"]}),
            tool_turn("run_command", {"command": ["npm", "run", "lint"]}),
            text_turn(),
        ],
    )
    assert counted == [HarnessCounter.COMMAND_RERUN_IN_TURN]


# ═══════════════════════════════════════════════════════════════════════════════════════
# One operation for applying a database change
# ═══════════════════════════════════════════════════════════════════════════════════════
#
# THE DEFECT THESE PIN is not a slow loop, it is a LIE: both halves of the sequence this one call
# replaced exit 0 after failing, which `sandbox/template/db/schema.ts` sets out in full.
#
# So every test below is really one assertion in two halves: the operation NEVER reports success
# when a step failed, and when it reports failure it says which step, what the output actually
# said, and what state that left the workspace and the database in.

# The three exit-zero-after-failing shapes, spelled as the programs themselves spell them.
_THE_TTY_REFUSAL = "Error: Interactive prompts require a TTY terminal"
_THE_SWALLOWED_ERROR = (
    '[db] migrations failed — starting the app anyway: relation "visitors" already exists'
)
_THE_ABANDONED_MIGRATION = (
    "[db] migrations still running after 20000ms — starting the app without them."
)
_THE_SKIPPED_MIGRATION = (
    "[db] BIAL_DATABASE_URL is not set — skipping migrations. The app will still start."
)
# …and the two success lines, which must NOT read as failures (the liveness half).
_A_MIGRATION_WAS_WRITTEN = "[✓] Your SQL migration file ➜ drizzle/0001_add_visitors_table.sql"
_MIGRATIONS_APPLIED = "[db] migrations up to date."

_THE_GENERATE = ["npx", "drizzle-kit", "generate", "--name", "add_visitors_table"]
_THE_MIGRATE = ["npm", "run", "db:migrate"]

_A_REPORT = re.compile(r"apply_schema_change (?:SUCCEEDED|FAILED)[\s\S]*")


def _report_in(captured: dict[str, Any]) -> str:
    """The composite's own answer, sliced out of everything the model was handed."""
    match = _A_REPORT.search(captured["all_incoming"])
    assert match is not None, f"no composite report in:\n{captured['all_incoming'][-3000:]}"
    return match.group(0)


async def _apply(fake: FakeSandbox, *, what_changed: str = "add visitors table") -> dict[str, Any]:
    return await _run(
        fake,
        [tool_turn("apply_schema_change", {"what_changed": what_changed}), text_turn("ok")],
    )


def test_the_zero_exit_lie_detector_reads_a_marker_anywhere_in_a_huge_capture() -> None:
    """The detector is handed the RAW `ExecResult` and nothing upstream caps it — the supervisor
    runs `subprocess.run(capture_output=True)` with no ceiling and the client adds none. It is
    left uncapped on purpose: the head+tail window every regex here uses has a MIDDLE it discards,
    and a marker dropped there reports a failed schema change as a success. This pins that
    direction, and the wall-clock bound alongside it pins the reason it is affordable —
    `.lower()` plus a substring search is memchr-speed, ~0.6 ms/MB."""
    filler = ("A" * 79 + "\n") * 40_000  # ~3.2 MB either side of the marker
    markers = ("interactive prompts require a tty",)
    buried = ExecResult(
        stdout=filler + "Interactive prompts require a TTY terminal\n" + filler, stderr="", exit=0
    )

    started = time.perf_counter()
    assert _the_command_lied(buried, markers) is True
    # LIVENESS for the negative: the same blob with no marker in it answers the other way, so the
    # assertion above is about the marker and not about the function saying yes to everything.
    assert _the_command_lied(ExecResult(stdout=filler, stderr="", exit=0), markers) is False
    assert time.perf_counter() - started < 1.5


async def test_a_step_that_exits_zero_after_failing_is_reported_as_a_failure() -> None:
    """★ THE HEADLINE. drizzle-kit reached the rename resolver: it printed the refusal to
    stderr, wrote no migration, and exited 0. The operation must report FAILURE anyway, name the
    step, say what state the workspace was left in, and say out loud that it is overriding the
    exit code — a verdict that silently contradicts a zero the model can see is a verdict the
    model argues with.

    Mutation-check: drop `_the_command_lied` from the `ok` expression and this goes red while
    every exit-code assertion in this file stays green, which is the whole point of the unit."""
    fake = FakeSandbox()
    fake.queue_commands(ExecResult(stdout="", stderr=_THE_TTY_REFUSAL, exit=0))
    report = _report_in(await _apply(fake))

    assert report.startswith("apply_schema_change FAILED at step 1 of 2 — generate the migration.")
    # WHAT STATE IT LEFT THINGS IN — the half a bare "it failed" leaves the model guessing at.
    assert "NO migration file was written and the database was not touched" in report
    assert "has NOT been applied" in report
    # THE OVERRIDE, said out loud rather than merely applied.
    assert "it exited 0 — that exit code is WRONG" in report
    assert "STEP 1 of 2 — generate the migration: FAILED" in report
    # …and the underlying output is right there, so the model can see the cause for itself.
    assert _THE_TTY_REFUSAL in report


async def test_every_step_succeeding_reports_success_with_a_per_step_outcome() -> None:
    """The other terminal state, and the liveness guard on every failure marker above: a real
    generate and a real migrate print lines that must NOT be read as failures."""
    fake = FakeSandbox()
    fake.queue_commands(
        ExecResult(stdout=_A_MIGRATION_WAS_WRITTEN, stderr="", exit=0),
        ExecResult(stdout=_MIGRATIONS_APPLIED, stderr="", exit=0),
    )
    report = _report_in(await _apply(fake))

    assert report.startswith("apply_schema_change SUCCEEDED — all 2 steps ran.")
    assert "the migration is applied — the database now matches `db/schema.ts`" in report
    # A PER-STEP OUTCOME FOR EACH — not one verdict for the pair.
    assert "STEP 1 of 2 — generate the migration: OK" in report
    assert "STEP 2 of 2 — apply the migration to the database: OK" in report
    assert "FAILED" not in report
    # Both commands really ran, in order, and the model's words became the migration's name.
    assert fake.command_calls == [_THE_GENERATE, _THE_MIGRATE]


async def test_a_failed_first_step_stops_the_second_and_the_report_says_so() -> None:
    """★ Applying half a schema change is worse than applying none: the migrate step would have
    re-applied whatever was already pending under a name the model thinks describes its new edit.
    So step two does not run — and "did not run" is REPORTED, because the state it implies
    (nothing reached the database) is different from "ran and failed"."""
    fake = FakeSandbox()
    fake.queue_commands(ExecResult(stdout="", stderr=_THE_TTY_REFUSAL, exit=0))
    report = _report_in(await _apply(fake))

    assert fake.command_calls == [_THE_GENERATE], "the migrate step ran after a failed generate"
    assert "STEP 2 of 2 — apply the migration to the database: NOT RUN" in report
    assert "step 1 failed, so this step never started" in report


@pytest.mark.parametrize(
    "printed",
    [_THE_SWALLOWED_ERROR, _THE_ABANDONED_MIGRATION, _THE_SKIPPED_MIGRATION],
    ids=["swallowed", "abandoned", "skipped"],
)
async def test_the_migrator_always_exits_zero_so_its_output_is_what_gets_read(
    printed: str,
) -> None:
    """★ THE SAME HEADLINE AGAIN, on the second step and all three of its shapes. `db-migrate.mjs`
    is non-fatal BY DESIGN — its own header explains why — so a caught error, a migration
    abandoned after its 20-second timer, and a run with no DSN to connect to all end in
    `process.exit(0)`. Each is a schema change that did not happen wearing a clean exit code."""
    fake = FakeSandbox()
    fake.queue_commands(
        ExecResult(stdout=_A_MIGRATION_WAS_WRITTEN, stderr="", exit=0),
        ExecResult(stdout=printed, stderr="", exit=0),
    )
    report = _report_in(await _apply(fake))

    assert report.startswith(
        "apply_schema_change FAILED at step 2 of 2 — apply the migration to the database."
    )
    # The state is the OTHER one — a migration file exists, and the database has not taken it.
    assert "the migration file IS written under `drizzle/`" in report
    assert "the tables still do not match `db/schema.ts`" in report
    # …and step one is still reported honestly as the success it was.
    assert "STEP 1 of 2 — generate the migration: OK" in report
    assert "it exited 0 — that exit code is WRONG" in report


def test_the_migrate_failure_markers_match_the_script_that_prints_them() -> None:
    """★ THE PAIR THAT DRIFTS SILENTLY: a detector, and the program whose output it reads.

    Every marker is a literal from `sandbox/template/scripts/db-migrate.mjs` on a path that ends
    in `process.exit(0)`. Reword one of those `console.error` lines and this composite goes
    quietly blind — reporting success on a migration that failed, which is precisely the defect
    it exists to remove. Nothing else in either repo half would notice."""
    script = (_TEMPLATE_ROOT / "scripts" / "db-migrate.mjs").read_text(encoding="utf-8")
    for marker in tools_module._MIGRATE_FAILED_MARKERS:
        assert marker in script.lower(), f"`{marker}` is no longer what the migrator prints"
    # LIVENESS — the success line is in the same file and must match NO marker, or the composite
    # would report every healthy migration as a failure.
    assert _MIGRATIONS_APPLIED.lower() in script.lower()
    assert not any(
        marker in _MIGRATIONS_APPLIED.lower() for marker in tools_module._MIGRATE_FAILED_MARKERS
    )


async def test_the_interactive_resolver_fails_fast_with_a_plain_explanation() -> None:
    """The wedge, asserted the only honest way: on the BOUND and the measured signature, never by
    waiting one out.

    Under this sandbox's real conditions the resolver cannot wait: the supervisor sets `CI=1`,
    closes stdin, and refuses a manufactured pty (`test_prompt.py` pins all three), so drizzle-kit
    fails immediately with the signature below. What this test owns is what the composite does
    with those seconds: it takes the SHORT bound rather than the ten-minute install class, and it
    hands back an explanation in words rather than a wedged command and a timeout."""
    fake = FakeSandbox()
    fake.queue_commands(ExecResult(stdout="", stderr=_THE_TTY_REFUSAL, exit=0))
    captured = await _apply(fake)

    # A migration generate should take seconds. Ten minutes of waiting for a terminal that does
    # not exist is ten minutes of the citizen's build.
    assert fake.command_timeouts == [constants.RUN_COMMAND_DEFAULT_TIMEOUT_S]
    report = _report_in(captured)
    # THE MEASURED SIGNATURE, pinned against the two other places the same measurement is
    # written down. It is drizzle-kit's string, so no test can keep it TRUE — but a re-measurement
    # that lands in one place and not the others is a drift this catches.
    marker = tools_module._GENERATE_FAILED_MARKERS[0]
    assert marker in _THE_TTY_REFUSAL.lower()
    for recorded in (_SUPERVISOR_APP, Path(prompt_blocks.__file__)):
        assert marker in recorded.read_text(encoding="utf-8").lower(), recorded
    # THE PLAIN EXPLANATION: what to do differently, not just what broke.
    assert "make ONE kind of schema change and call this again" in report
    assert "Nothing needs undoing" in report
    # …and the loop carried on rather than crashing — a failure here is a normal tool result.
    assert captured["output"] == "ok"


async def test_the_composites_output_is_capped_by_its_verdict_not_by_the_commands_exit_code() -> (
    None
):
    """★ The output cap, reached through the composite's own override. A step that failed while
    exiting 0 would be SUMMARISED if the budget were read off `result.exit` — the misleading
    zero deciding how much of the failure the model gets to see. The budget is asked about
    the OPERATION's verdict instead, so a failing composite dumps and a succeeding one
    summarises, and both still hand back the slice handle to whatever was cut."""
    failing = FakeSandbox()
    failing.queue_commands(
        ExecResult(stdout=_long_output(lines=900), stderr=_THE_TTY_REFUSAL, exit=0)
    )
    failed = _report_in(await _apply(failing))

    passing = FakeSandbox()
    passing.queue_commands(
        ExecResult(stdout=_long_output(lines=900), stderr="", exit=0),
        ExecResult(stdout=_MIGRATIONS_APPLIED, stderr="", exit=0),
    )
    passed = _report_in(await _apply(passing))

    assert "FAILED" in failed and "SUCCEEDED" in passed
    # THE TWO BUDGETS, NAMED — not merely "one is bigger". A `len(failed) > len(passed)` pair
    # survives a mutant that sizes BOTH from the underlying exit code, because the failure report
    # carries extra words of its own; these two do not.
    assert len(failed) > constants.RUN_COMMAND_OUTPUT_MAX_CHARS, "the failure was summarised"
    assert len(passed) < constants.RUN_COMMAND_OUTPUT_MAX_CHARS, "the success was dumped"
    assert len(passed) > constants.RUN_COMMAND_SUMMARY_MAX_CHARS  # it kept its summary budget
    # Both were cut — a comparison where neither truncated would prove nothing about either.
    assert "elided" in failed and "elided" in passed
    # …and the elided middle is still recoverable in one call, exactly as `run_command`'s is.
    assert _slice_call_in(failed) is not None
    assert _slice_call_in(passed) is not None


async def test_a_transport_failure_names_the_step_and_the_state_and_re_enters_the_loop() -> None:
    """A supervisor blip is not a step verdict — the step never returned one — so it comes back as
    a `ModelRetry` like `run_command`'s rather than a fabricated failure report. It still has to
    say which step and what state, because "something went wrong somewhere in there" is exactly
    the answer this tool exists to stop giving."""
    fake = FakeSandbox()
    fake.queue_exec_errors(SandboxError("exec timed out after 180s"))
    captured = await _apply(fake)

    assert "The `generate the migration` step could not run" in captured["all_incoming"]
    assert "NO migration file was written" in captured["all_incoming"]
    assert captured["output"] == "ok"  # the loop healed rather than crashing


async def test_a_gone_sandbox_still_escalates_from_the_composite() -> None:
    """Only `SandboxGoneError` leaves the tool — the restore-needed escalation is terminal for the
    handle and must not be dressed up as a step outcome."""
    fake = FakeSandbox()
    fake.queue_exec_errors(SandboxGoneError("the sandbox is gone"))
    with pytest.raises(SandboxGoneError):
        await _apply(fake)


async def test_the_model_writes_the_migration_name_in_words() -> None:
    """`what_changed` is prose in the prompt and a slug on the command line, and the tool does the
    conversion rather than bouncing the model for a formatting quibble — a `ModelRetry` here would
    spend the exact round trip the whole unit exists to save. Only an input with nothing usable in
    it is refused."""
    fake = FakeSandbox()
    fake.queue_commands(
        ExecResult(stdout=_A_MIGRATION_WAS_WRITTEN, stderr="", exit=0),
        ExecResult(stdout=_MIGRATIONS_APPLIED, stderr="", exit=0),
    )
    await _apply(fake, what_changed="Add a Visitors table!")
    assert fake.command_calls[0] == [
        "npx",
        "drizzle-kit",
        "generate",
        "--name",
        "add_a_visitors_table",
    ]

    nameless = FakeSandbox()
    captured = await _apply(nameless, what_changed="   !!!   ")
    assert "has to describe the schema edit in words" in captured["all_incoming"]
    assert nameless.command_calls == [], "a nameless call still ran the generate"


async def test_the_composite_gets_the_long_operation_status_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ The stillness narrator exists for exactly this tool: the composite removes the per-step
    narration that used to fill the gap, so a citizen watching a schema change would otherwise
    watch a row that stopped changing when the generate started. Driven at `_on_event`;
    threshold and cadence are compressed rather than waited out, since the property under test
    is "past the threshold, and repeatedly", not the number of seconds.

    Mutation-check: classify the composite as `hidden` and this goes red, because
    `_start_long_operation` refuses to narrate a step that renders nowhere."""
    engine = engine_module.TurnEngine()
    monkeypatch.setattr(engine_module, "LONG_OPERATION_THRESHOLD_MS", 20)
    monkeypatch.setattr(engine_module, "LONG_OPERATION_REFRESH_MS", 20)
    state = _TurnState(
        turn_id=uuid.uuid7(),
        conversation_id=uuid.uuid7(),
        user_id=uuid.uuid7(),
        kind=ChatKind.BUILD,
    )

    engine._on_event(
        state,
        FunctionToolCallEvent(
            part=ToolCallPart(
                tool_name="apply_schema_change",
                args='{"what_changed": "add visitors table"}',
                tool_call_id="c1",
            )
        ),
    )
    await asyncio.sleep(0.12)

    labels = [
        frame.item.label
        for frame in state.ring
        if isinstance(frame, StepFrame) and frame.phase == "started"
    ]
    assert labels, "no step frame at all — the seam under test never ran"
    announced, refreshes = labels[0], labels[1:]
    assert announced == "Setting up where your app stores information"
    assert refreshes, "the composite ran past the threshold and said nothing"
    assert set(refreshes) == {long_operation_line(announced)}
    assert "Still setting up where your app stores information" in refreshes[0]
    # Still the platform's language, not the shell's — the narrator restates the step's own label.
    assert "drizzle" not in " ".join(labels).lower()
    await engine._drain_long_operations(state)


async def test_the_adoption_pair_tells_the_composite_from_the_hand_rolled_sequence(
    counted: list[str],
) -> None:
    """★ The behavioural bet, counted. The open question is whether the agent actually REACHES
    for the composite, and neither number answers it alone: "40 composite calls" is a fact about
    traffic until you know how many hand-rolled sequences ran beside it.

    The by-hand half counts the GENERATE only — the head of the sequence — so one hand-rolled
    sequence scores one, exactly as one composite call does, and the two are comparable without a
    correction factor. A lone `db:migrate` is legitimately re-applying an existing migration."""
    fake = FakeSandbox()
    fake.queue_commands(
        ExecResult(stdout=_A_MIGRATION_WAS_WRITTEN, stderr="", exit=0),
        ExecResult(stdout=_MIGRATIONS_APPLIED, stderr="", exit=0),
    )
    await _apply(fake)
    assert counted == [HarnessCounter.SCHEMA_CHANGE_COMPOSED]

    counted.clear()
    fake.queue_commands(
        ExecResult(stdout=_A_MIGRATION_WAS_WRITTEN, stderr="", exit=0),
        ExecResult(stdout=_MIGRATIONS_APPLIED, stderr="", exit=0),
    )
    await _run(
        fake,
        [
            tool_turn("run_command", {"command": _THE_GENERATE}),
            tool_turn("run_command", {"command": _THE_MIGRATE}),
            text_turn(),
        ],
    )
    # DISTINGUISHABLE: a different name, and counted ONCE for the two-command sequence.
    assert counted == [HarnessCounter.SCHEMA_CHANGE_BY_HAND]

    counted.clear()
    fake.queue_commands(ExecResult(stdout="ok", stderr="", exit=0))
    await _run(fake, [tool_turn("run_command", {"command": ["npm", "run", "lint"]})])
    assert counted == [], "an ordinary command was counted as a schema change"
