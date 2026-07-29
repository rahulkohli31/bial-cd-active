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


async def test_run_command_uses_the_longer_install_timeout_not_the_tsc_budget(
    sink: CollectingSink,
) -> None:
    # U4: run_command gets RUN_COMMAND_TIMEOUT_S, distinct from the tsc path's EXEC_TIMEOUT_S.
    fake = FakeSandbox()
    fake.queue_commands(ExecResult(stdout="ok", stderr="", exit=0))
    await _run(fake, sink, [tool_turn("run_command", {"command": ["npm", "run", "lint"]})])
    assert fake.command_timeouts == [constants.RUN_COMMAND_TIMEOUT_S]
    assert constants.RUN_COMMAND_TIMEOUT_S != constants.EXEC_TIMEOUT_S


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


# --- W1: the commit reminder on the write tool RESULT ---------------------------------
#
# The agent commits as it works so it can `git diff` what it changed and revert its own mistakes,
# and so a future code-review agent inherits a history that reads as intent. The nudge rides the
# TOOL RESULT rather than the system prompt, because that is what makes it land at the moment of
# the edit. Each property below is one of the five that make such a reminder work — drop any and
# it either becomes wallpaper or fragments the history it exists to produce.


async def test_the_commit_reminder_fires_on_the_third_write_not_the_first(
    sink: CollectingSink,
) -> None:
    fake = FakeSandbox()
    captured = await _run(
        fake,
        sink,
        [
            tool_turn("write_file", {"path": "app/one.tsx", "file_text": "a"}),
            tool_turn("write_file", {"path": "app/two.tsx", "file_text": "b"}),
            tool_turn("write_file", {"path": "app/three.tsx", "file_text": "c"}),
            text_turn(),
        ],
    )
    seen = captured["incoming"]
    # The model's view AFTER the first write carries no reminder; after the third it does.
    assert "<system-reminder>" not in seen[1]
    assert "<system-reminder>" in seen[3]


async def test_the_reminder_names_the_action_stays_optional_and_forbids_quoting_itself(
    sink: CollectingSink,
) -> None:
    fake = FakeSandbox()
    captured = await _run(
        fake,
        sink,
        [
            tool_turn("write_file", {"path": f"app/{n}.tsx", "file_text": "x"})
            for n in ("one", "two", "three")
        ]
        + [text_turn()],
    )
    reminder = captured["incoming"][3]
    assert "commit them now with a message" in reminder  # the exact action, not "remember to"
    assert "Ignore this if" in reminder  # non-binding, or it commits mid-slice
    # N9's lesson, carried forward: the existing <system-note> was narrated to the citizen twice
    # in one conversation, so a platform note leaking into the transcript is proven here.
    assert "Do not mention this note" in reminder


async def test_a_successful_commit_resets_the_count_so_the_next_slice_starts_clean(
    sink: CollectingSink,
) -> None:
    fake = FakeSandbox()
    captured = await _run(
        fake,
        sink,
        [
            tool_turn("write_file", {"path": "app/one.tsx", "file_text": "a"}),
            tool_turn("write_file", {"path": "app/two.tsx", "file_text": "b"}),
            tool_turn("run_command", {"command": ["git", "commit", "-m", "add two pages"]}),
            tool_turn("write_file", {"path": "app/three.tsx", "file_text": "c"}),
            text_turn(),
        ],
    )
    # Without the reset this third write would be the third UNCOMMITTED one and would nag —
    # which is the difference between "uncommitted writes" and merely "recent writes".
    assert "<system-reminder>" not in captured["incoming"][4]


async def test_a_failed_commit_does_not_reset_the_count(sink: CollectingSink) -> None:
    """The arm that would silently suppress the reminder exactly when it is most needed: a commit
    that failed (nothing staged, a hook refusing) left the work uncommitted."""
    fake = FakeSandbox()
    fake.queue_commands(ExecResult(exit=1, stdout="", stderr="nothing added to commit"))
    captured = await _run(
        fake,
        sink,
        [
            tool_turn("write_file", {"path": "app/one.tsx", "file_text": "a"}),
            tool_turn("write_file", {"path": "app/two.tsx", "file_text": "b"}),
            tool_turn("run_command", {"command": ["git", "commit", "-m", "nope"]}),
            tool_turn("write_file", {"path": "app/three.tsx", "file_text": "c"}),
            text_turn(),
        ],
    )
    assert "<system-reminder>" in captured["incoming"][4]
