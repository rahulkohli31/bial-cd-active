"""The five tools + the fail-closed write guard + 422→ModelRetry enrichment (U5, KD-4/5/9/10).

Driven through `build_agent` + a capturing `FunctionModel` + `FakeSandbox` — the real reflection
path, not a hand-called function."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models.function import AgentInfo, FunctionModel

from src.services.orchestrator import build_agent, constants
from src.services.orchestrator.deps import BuildDeps, SandboxSession
from src.services.orchestrator.progress import ProgressEmitter
from src.services.orchestrator.tools import _redact_command_output
from src.services.sandbox import (
    ExecResult,
    FileOp,
    FileResult,
    FileView,
    SandboxError,
    SandboxGoneError,
    SandboxHandle,
)
from tests.services.orchestrator.conftest import CollectingSink
from tests.services.orchestrator.fake_sandbox import FAKE_SUPERVISOR_TOKEN, FakeSandbox
from tests.services.orchestrator.model_harness import text_turn, tool_turn

_TOOL_NAMES = {
    "read_file",
    "write_file",
    "edit_file",
    "insert_lines",
    "declare_done",
    "run_command",
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
        # would be asserting on a string the model never sees (U18).
        captured["tool_descriptions"] = {t.name: t.description or "" for t in info.function_tools}
        captured.setdefault("incoming", []).append(_all_text(messages))
        return next(iterator, text_turn("done"))

    return FunctionModel(respond)


def _deps(fake: FakeSandbox, sink: CollectingSink) -> BuildDeps:
    emitter = ProgressEmitter(sink)
    return BuildDeps(
        sandbox=SandboxSession(
            sandbox_client=fake,
            handle=fake.handle(),
            app_id=uuid.uuid4(),
            emitter=emitter,
        ),
        emitter=emitter,
        user_id=uuid.uuid4(),
    )


async def _run(
    fake: FakeSandbox, sink: CollectingSink, turns: list[ModelResponse]
) -> dict[str, Any]:
    captured: dict[str, Any] = {}
    model = _capturing_model(turns, captured)
    result = await build_agent.run("build the app", deps=_deps(fake, sink), model=model)
    captured["output"] = result.output
    captured["all_incoming"] = "\n".join(captured.get("incoming", []))
    return captured


async def test_registered_tool_surface_is_the_open_sandbox_set(sink: CollectingSink) -> None:
    fake = FakeSandbox()
    captured = await _run(fake, sink, [text_turn("nothing to do")])
    # The open-sandbox surface: the five file tools + run_command (the vibe-coding pivot, R1).
    assert captured["tool_names"] == _TOOL_NAMES
    assert "run_command" in captured["tool_names"]


async def test_read_file_returns_numbered_lines(sink: CollectingSink) -> None:
    fake = FakeSandbox(seed_files={"app/records/page.tsx": "alpha\nbeta\ngamma"})
    captured = await _run(
        fake, sink, [tool_turn("read_file", {"path": "app/records/page.tsx"}), text_turn()]
    )
    assert "1\talpha" in captured["all_incoming"]
    assert "3\tgamma" in captured["all_incoming"]


async def test_read_file_refuses_the_ignore_set(sink: CollectingSink) -> None:
    fake = FakeSandbox()
    captured = await _run(
        fake, sink, [tool_turn("read_file", {"path": "node_modules/react/index.js"}), text_turn()]
    )
    assert "not readable" in captured["all_incoming"]


class _MalformedViewSandbox(FakeSandbox):
    """A C2 client whose `view` returns a FileResult MISSING the contractually-required `content`
    key (a malformed C1 response); every other op behaves normally."""

    async def files(self, handle: SandboxHandle, op: FileOp) -> FileResult:
        if isinstance(op, FileView):
            return FileResult(ok=True, detail={})  # no `content`
        return await super().files(handle, op)


async def test_read_file_surfaces_a_malformed_response(sink: CollectingSink) -> None:
    fake = _MalformedViewSandbox(seed_files={"app/x.tsx": "hi"})
    captured = await _run(fake, sink, [tool_turn("read_file", {"path": "app/x.tsx"}), text_turn()])
    # A missing `content` key surfaces as a retry — it must NOT masquerade as a legitimately-empty
    # file (fail-first: a malformed backend response should be loud).
    assert "returned no content" in captured["all_incoming"]


async def test_read_file_clamps_an_unbounded_view(sink: CollectingSink) -> None:
    big = "\n".join(f"row-{i}" for i in range(1, 1001))  # 1000 lines
    fake = FakeSandbox(seed_files={"app/big.tsx": big})
    captured = await _run(
        fake, sink, [tool_turn("read_file", {"path": "app/big.tsx"}), text_turn()]
    )
    # Bounded to VIEW_MAX_LINES (400) — line 500 is never surfaced.
    assert "400\trow-400" in captured["all_incoming"]
    assert "row-500" not in captured["all_incoming"]


async def test_read_file_minus_one_end_reads_to_end_of_file(sink: CollectingSink) -> None:
    # The docstring promise "-1 = end of file" must round-trip NON-EMPTY (the pre-fix
    # supervisor computed an empty range for -1; the fake mirrors the fixed semantics).
    fake = FakeSandbox(seed_files={"app/x.tsx": "alpha\nbeta\ngamma"})
    captured = await _run(
        fake,
        sink,
        [tool_turn("read_file", {"path": "app/x.tsx", "view_range": [1, -1]}), text_turn()],
    )
    assert "1\talpha" in captured["all_incoming"]
    assert "3\tgamma" in captured["all_incoming"]


async def test_read_file_minus_one_end_is_still_budget_clamped(sink: CollectingSink) -> None:
    # -1 must not become an unbounded read: the VIEW_MAX_LINES clamp applies to it too
    # (PR#33 #13 — previously only the end != -1 branch clamped).
    big = "\n".join(f"row-{i}" for i in range(1, 1001))  # 1000 lines
    fake = FakeSandbox(seed_files={"app/big.tsx": big})
    captured = await _run(
        fake,
        sink,
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
async def test_write_allowed_across_the_open_surface(sink: CollectingSink, path: str) -> None:
    fake = FakeSandbox()
    await _run(
        fake, sink, [tool_turn("write_file", {"path": path, "file_text": "export const x = 1;\n"})]
    )
    assert fake.workspace[path] == "export const x = 1;\n"


@pytest.mark.parametrize("path", [".git/config", ".git/hooks/pre-push"])
async def test_write_denied_paths_raise_model_retry_and_never_touch_files(
    sink: CollectingSink, path: str
) -> None:
    fake = FakeSandbox()
    captured = await _run(
        fake, sink, [tool_turn("write_file", {"path": path, "file_text": "PWNED"})]
    )
    # The guard fired before files(): nothing was written…
    assert path not in fake.workspace
    assert "PWNED" not in "".join(fake.workspace.values())
    # …and the model was told why (a ModelRetry, KD-9).
    assert "cannot be written" in captured["all_incoming"]


async def test_edit_file_bad_match_enriches_into_a_model_retry(sink: CollectingSink) -> None:
    fake = FakeSandbox(seed_files={"app/records/page.tsx": "one\ntwo\nthree"})
    captured = await _run(
        fake,
        sink,
        [
            tool_turn(
                "edit_file",
                {"path": "app/records/page.tsx", "old_str": "not-in-file", "new_str": "x"},
            ),
            text_turn(),
        ],
    )
    # The enrichment surfaces the current numbered file + the exactly-once rule (KD-5).
    assert "match EXACTLY ONCE" in captured["all_incoming"]
    assert "1\tone" in captured["all_incoming"]


async def test_str_replace_bad_then_fixed_recovers_in_run(sink: CollectingSink) -> None:
    fake = FakeSandbox(seed_files={"app/x.tsx": "aaa\nbbb\nccc\n"})
    captured = await _run(
        fake,
        sink,
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


async def test_no_tool_leaks_the_supervisor_token(sink: CollectingSink) -> None:
    fake = FakeSandbox()
    # A failing read (missing file) plus a denied write — neither must render handle.token.
    captured = await _run(
        fake,
        sink,
        [
            tool_turn("read_file", {"path": "app/missing.tsx"}),
            tool_turn("write_file", {"path": ".git/config", "file_text": "x"}),
            text_turn(),
        ],
    )
    assert FAKE_SUPERVISOR_TOKEN not in captured["all_incoming"]


async def test_declare_done_sets_the_signal_and_emits_a_step(sink: CollectingSink) -> None:
    fake = FakeSandbox()
    deps = _deps(fake, sink)
    captured: dict[str, Any] = {}
    model = _capturing_model(
        [tool_turn("declare_done", {"summary": "built it"}), text_turn()], captured
    )
    await build_agent.run("build", deps=deps, model=model)
    assert deps.sandbox.done_requested is True
    assert deps.sandbox.done_summary == "built it"
    assert any(getattr(e, "name", None) == "declare_done" for e in sink.events)


async def test_the_declare_done_description_the_model_reads_says_the_turn_ends(
    sink: CollectingSink,
) -> None:
    """★ U18/R30 — THE TOOL'S OWN DESCRIPTION IS HALF THE BEHAVIOUR.

    `declare_done` used to promise the opposite of what it now does ("This does NOT end the
    build on its own"), and a model that believes it gets one more turn keeps its closing
    message OUT of `summary` and saves it for prose the harness has just stopped rendering.
    That is the exact failure this unit exists to prevent, so the description is asserted with
    the same seriousness as the code.

    Asserted on the description the TOOLSET REGISTERS — the text pydantic-ai actually sends —
    rather than on `__doc__`, because the framework composes one from the other and only one of
    them reaches the model.

    Mutation check: restore either retired sentence and the two absence asserts go red; drop
    the diagnostic clause and the liveness assert does."""
    fake = FakeSandbox()
    captured = await _run(fake, sink, [text_turn("nothing to do")])
    description = captured["tool_descriptions"]["declare_done"]
    # Wrapped at 96 columns in the source, so every assertion below is made against the text
    # with its line breaks collapsed — otherwise a phrase straddling a wrap silently misses.
    lowered = " ".join(description.lower().split())

    # THE TERMINAL CONDITION, STATED — and stated as conditional on the check, which is what
    # keeps it true (ASM14: the conjunction with the verdict is untouched).
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

    # U20 GENERATES THE PROMPT'S TOOL-SURFACE LINE FROM THE FIRST SENTENCE, so the first
    # sentence has to stand alone as user-visible prompt copy.
    first_sentence = lowered.split(".")[0]
    assert "declare the build finished" in first_sentence
    assert "summary" in first_sentence


async def test_declare_done_tells_the_model_its_summary_is_the_last_word(
    sink: CollectingSink,
) -> None:
    """★ U18 — THE RETURN STRING MOVED WITH THE BEHAVIOUR TOO.

    It used to say "The harness will now type-check the app and confirm it renders", which
    reads as an invitation to stand by for a second act. It now says which of the two arms is
    terminal and which is not — both truthfully.

    Asserted on what the MODEL received back (the tool return in its next input), not on the
    function's return value, for the same reason as the description test above."""
    fake = FakeSandbox()
    deps = _deps(fake, sink)
    captured: dict[str, Any] = {}
    model = _capturing_model(
        [tool_turn("declare_done", {"summary": "You can add visitors and check them in."})],
        captured,
    )
    await build_agent.run("build", deps=deps, model=model)
    returned = "\n".join(captured["incoming"]).lower()

    assert "this turn ends here" in returned
    assert "nothing further is asked of you" in returned
    # The repair arm survives verbatim — a red check still hands over the diagnostic.
    assert "you will get the diagnostic to fix" in returned
    # The retired stand-by phrasing is gone.
    assert "will now type-check the app" not in returned


async def test_write_emits_a_step(sink: CollectingSink) -> None:
    fake = FakeSandbox()
    await _run(fake, sink, [tool_turn("write_file", {"path": "app/page.tsx", "file_text": "x\n"})])
    assert any(getattr(e, "name", None) == "edit" for e in sink.events)


# --- F3/U3: the LIVE feed emits friendly labels, never raw shell/argv/paths ---


def _steps(sink: CollectingSink) -> list[Any]:
    return [e for e in sink.events if getattr(e, "type", None) == "step"]


async def test_write_file_emits_the_friendly_area_not_the_raw_path(sink: CollectingSink) -> None:
    # The live file-tool emit routes through the shared classifier — the citizen sees an AREA.
    fake = FakeSandbox()
    await _run(fake, sink, [tool_turn("write_file", {"path": "app/page.tsx", "file_text": "x\n"})])
    step = next(e for e in _steps(sink) if e.name == "edit")
    assert step.label == "Building your app's main page"
    assert "app/page.tsx" not in step.label
    # A config write is hidden noise.
    sink.events.clear()
    await _run(
        fake, sink, [tool_turn("write_file", {"path": "package.json", "file_text": "{}\n"})]
    )
    assert next(e for e in _steps(sink) if e.name == "edit").hidden is True


async def test_run_command_emits_one_friendly_row_no_raw_shell(sink: CollectingSink) -> None:
    # started+done collapse to ONE terminal row; the visible label is friendly, never `$ argv`.
    fake = FakeSandbox()
    fake.queue_commands(ExecResult(stdout="added 1 package", stderr="", exit=0))
    await _run(
        fake, sink, [tool_turn("run_command", {"command": ["npm", "install", "zod"]}), text_turn()]
    )
    rc = [e for e in _steps(sink) if e.name == "run_command"]
    assert len(rc) == 1  # ONE row per command (no separate `started` emit)
    assert rc[0].state == "ok"
    assert rc[0].label == "Setting up the tools your app needs"
    for leaked in ("$ ", "npm", "install", "zod"):
        assert leaked not in rc[0].label


async def test_run_command_unrecognized_fails_closed_in_the_live_label(
    sink: CollectingSink,
) -> None:
    # The fail-closed property AT THE EMITTER: an arbitrary command never leaks its argv.
    fake = FakeSandbox()
    fake.queue_commands(ExecResult(stdout="", stderr="", exit=0))
    await _run(
        fake,
        sink,
        [tool_turn("run_command", {"command": ["bash", "-c", "rm -rf /tmp/x"]}), text_turn()],
    )
    rc = next(e for e in _steps(sink) if e.name == "run_command")
    assert rc.label == "Working on your app"
    for leaked in ("bash", "-c", "$ ", "rm -rf"):
        assert leaked not in rc.label


async def test_run_command_failed_transport_emits_a_friendly_failed_label(
    sink: CollectingSink,
) -> None:
    # Emit site 253 (SandboxError → failed): still friendly, still no `$ argv`.
    fake = FakeSandbox()
    fake.queue_exec_errors(SandboxError("exec timed out after 600s"))
    await _run(
        fake,
        sink,
        [
            tool_turn("run_command", {"command": ["npm", "install", "big-pkg"]}),
            text_turn("healed"),
        ],
    )
    rc = next(e for e in _steps(sink) if e.name == "run_command")
    assert rc.state == "failed"
    assert rc.label == "Setting up the tools your app needs — couldn't finish"
    assert "$ " not in rc.label


async def test_run_command_blocked_sql_emits_a_friendly_failed_label(sink: CollectingSink) -> None:
    # Emit site 240 (blocked destructive SQL): friendly base + human suffix, never the raw SQL.
    fake = FakeSandbox()
    await _run(
        fake,
        sink,
        [
            tool_turn("run_command", {"command": ["psql", "-c", "DELETE FROM visitors"]}),
            text_turn("understood"),
        ],
    )
    rc = next(e for e in _steps(sink) if e.name == "run_command")
    assert rc.state == "failed"
    assert rc.label == "Working on your app — blocked to protect your data"
    for leaked in ("psql", "DELETE", "visitors", "$ "):
        assert leaked not in rc.label


async def test_run_command_read_only_emits_a_hidden_step(sink: CollectingSink) -> None:
    fake = FakeSandbox()
    fake.queue_commands(ExecResult(stdout="app/page.tsx", stderr="", exit=0))
    await _run(fake, sink, [tool_turn("run_command", {"command": ["ls", "app"]}), text_turn()])
    rc = next(e for e in _steps(sink) if e.name == "run_command")
    assert rc.hidden is True


# --- run_command (U1 / U4 / R1 / R3 / R11) -----------------------------------


async def test_run_command_returns_exit_and_redacted_output(sink: CollectingSink) -> None:
    fake = FakeSandbox()
    fake.queue_commands(ExecResult(stdout="added 1 package in 2s", stderr="", exit=0))
    captured = await _run(
        fake,
        sink,
        [tool_turn("run_command", {"command": ["npm", "install", "zod"]}), text_turn()],
    )
    assert "exit code: 0" in captured["all_incoming"]
    assert "added 1 package in 2s" in captured["all_incoming"]
    assert fake.command_calls == [["npm", "install", "zod"]]


async def test_run_command_nonzero_exit_is_a_normal_result_not_an_exception(
    sink: CollectingSink,
) -> None:
    # An npm 404 / peer-dep conflict is exit != 0 — it must come back as a NORMAL tool result the
    # model can read and re-feed, never a raised exception (AE1).
    fake = FakeSandbox()
    fake.queue_commands(ExecResult(stdout="", stderr="npm ERR! 404 Not Found: nosuchpkg", exit=1))
    captured = await _run(
        fake,
        sink,
        [tool_turn("run_command", {"command": ["npm", "install", "nosuchpkg"]}), text_turn("ok")],
    )
    assert "exit code: 1" in captured["all_incoming"]
    assert "npm ERR! 404" in captured["all_incoming"]
    # The run continued to the next turn — the failure did not crash the build.
    assert captured["output"] == "ok"


async def test_run_command_sandbox_error_becomes_a_model_retry_in_loop(
    sink: CollectingSink,
) -> None:
    # A supervisor 504 (incl. an install-timeout) surfaces as SandboxError → converted to a
    # ModelRetry and re-fed in-loop, never a hard build failure (R11).
    fake = FakeSandbox()
    fake.queue_exec_errors(SandboxError("exec timed out after 600s"))
    captured = await _run(
        fake,
        sink,
        [
            tool_turn("run_command", {"command": ["npm", "install", "big-pkg"]}),
            text_turn("healed"),
        ],
    )
    assert "could not run" in captured["all_incoming"]
    assert captured["output"] == "healed"  # the loop recovered rather than crashing


async def test_run_command_sandbox_gone_escalates(sink: CollectingSink) -> None:
    # Only SandboxGoneError propagates out of the run (→ run_build's sandbox_gone escalation).
    fake = FakeSandbox()
    fake.queue_exec_errors(SandboxGoneError("the sandbox is gone"))
    with pytest.raises(SandboxGoneError):
        await _run(fake, sink, [tool_turn("run_command", {"command": ["npm", "install"]})])


# F4 — the bound depends on WHAT the command is, and one global value could not satisfy both
# halves of the fix. The observed wedge (a `drizzle-kit generate` blocking on an interactive
# prompt with no terminal to answer it) burned 249s, so catching it needs a bound well under
# that; but this repo's own constant documents that a cold-base `npm install` "routinely" takes
# the full 600s, so lowering a single global would kill healthy builds instead.


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
    sink: CollectingSink, command: list[str], slow: bool
) -> None:
    fake = FakeSandbox()
    fake.queue_commands(ExecResult(stdout="ok", stderr="", exit=0))
    await _run(fake, sink, [tool_turn("run_command", {"command": command})])
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
    # Both stay under C1's 900s hard cap, and neither is the tsc verify budget.
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
async def test_run_command_output_is_secret_redacted(
    sink: CollectingSink, secret_line: str
) -> None:
    # run_command is the first tool to egress captured stdout — a credential-shaped value must be
    # masked before it re-enters the model context (R3).
    fake = FakeSandbox()
    fake.queue_commands(ExecResult(stdout=secret_line, stderr="", exit=0))
    captured = await _run(
        fake, sink, [tool_turn("run_command", {"command": ["env"]}), text_turn()]
    )
    for leaked in (
        "bial_AbCdEf0123456789ghIjKlMnOpQr",
        "super-secret-value-do-not-leak",
        "hunter2",
    ):
        assert leaked not in captured["all_incoming"]
    assert "***" in captured["all_incoming"]


def test_redact_command_output_caps_raw_input_before_redacting() -> None:
    # ReDoS guard: raw output is sliced to REDACT_INPUT_MAX_CHARS BEFORE redact_secrets runs, so a
    # trailing secret beyond the cap is dropped (never scanned) and the result stays bounded.
    trailing_secret = "bial_ThisIsPastTheInputCap0123456789"
    raw = ("A" * (constants.REDACT_INPUT_MAX_CHARS + 5_000)) + trailing_secret
    out = _redact_command_output(raw)
    assert trailing_secret not in out
    assert "bial_ThisIsPast" not in out
    assert len(out) <= constants.RUN_COMMAND_OUTPUT_MAX_CHARS + 64  # bound + truncation marker


async def test_run_command_never_leaks_the_supervisor_token(sink: CollectingSink) -> None:
    fake = FakeSandbox()
    fake.queue_exec_errors(SandboxError("boom"))
    captured = await _run(
        fake,
        sink,
        [tool_turn("run_command", {"command": ["npm", "install"]}), text_turn()],
    )
    assert FAKE_SUPERVISOR_TOKEN not in captured["all_incoming"]


# --- U19 / R25: the commit reminder that no longer exists ------------------------------
#
# THIS REPLACES FOUR TESTS that pinned the reminder's cadence and its reset — the reminder fires
# on the third uncommitted write, names the exact action, stays non-binding, and a SUCCESSFUL
# `git commit` zeroes the count while a failed one does not. All four were correct, and all four
# enforced an instruction that no longer exists: the Write segment's COMMIT AS YOU WORK block is
# deleted, because the platform commits the tree itself at every turn boundary.
#
# THE ENFORCER HAD TO GO WITH THE INSTRUCTION, and that is the whole point of testing it here.
# `_note_write_and_maybe_remind` lived in `tools.py` — the toolset BOTH agents build from —
# while the instruction it enforced lived only in the Write segment. Delete one and not the
# other, and every third file write comes back carrying a `<system-reminder>` telling the model
# to commit a slice nothing ever asked it to commit.


async def test_no_reminder_rides_a_write_result_any_more(sink: CollectingSink) -> None:
    """★ Asserted at THREE writes — `COMMIT_REMINDER_AFTER_WRITES`, the count at which the
    reminder used to fire — so this is a real observation rather than a test that never reached
    the trigger. A fourth and fifth write follow, because the retired counter reset itself on
    firing and a cadence bug could otherwise hide in the second cycle.

    Mutation check: restore `_note_write_and_maybe_remind` on `write_file` and this goes red."""
    fake = FakeSandbox()
    captured = await _run(
        fake,
        sink,
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


async def test_a_git_commit_through_run_command_is_still_an_ordinary_command(
    sink: CollectingSink,
) -> None:
    """The agent is no longer TOLD to commit; `run_command` is still a real shell, so a commit it
    chooses to run must behave like any other command. The retired `_is_git_commit` sniffer is
    gone with the counter it reset, and nothing replaces it — no special-casing of `git` on the
    exec path."""
    fake = FakeSandbox()
    captured = await _run(
        fake,
        sink,
        [
            tool_turn("write_file", {"path": "app/one.tsx", "file_text": "a"}),
            tool_turn("run_command", {"command": ["git", "commit", "-m", "whatever"]}),
            tool_turn("write_file", {"path": "app/two.tsx", "file_text": "b"}),
            text_turn(),
        ],
    )
    assert "<system-reminder>" not in captured["all_incoming"]
    assert ["git", "commit", "-m", "whatever"] in fake.command_calls
