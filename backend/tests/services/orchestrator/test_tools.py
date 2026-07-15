"""The five tools + the fail-closed write guard + 422→ModelRetry enrichment (U5, KD-4/5/9/10).

Driven through `build_agent` + a capturing `FunctionModel` + `FakeSandbox` — the real reflection
path, not a hand-called function."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models.function import AgentInfo, FunctionModel

from src.services.orchestrator import build_agent
from src.services.orchestrator.deps import BuildDeps
from src.services.orchestrator.progress import ProgressEmitter
from src.services.sandbox import FileOp, FileResult, FileView, SandboxHandle
from tests.services.orchestrator.conftest import CollectingSink
from tests.services.orchestrator.fake_sandbox import FAKE_SUPERVISOR_TOKEN, FakeSandbox
from tests.services.orchestrator.model_harness import text_turn, tool_turn

_TOOL_NAMES = {"read_file", "write_file", "edit_file", "insert_lines", "declare_done"}


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
    return BuildDeps(
        sandbox_client=fake,
        handle=fake.handle(),
        emitter=ProgressEmitter(sink),
        user_id=uuid.uuid4(),
        app_id=uuid.uuid4(),
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


async def test_registered_tool_surface_is_exactly_the_five(sink: CollectingSink) -> None:
    fake = FakeSandbox()
    captured = await _run(fake, sink, [text_turn("nothing to do")])
    # No run_command / exec tool — a guard against a regression re-adding one (KD-4).
    assert captured["tool_names"] == _TOOL_NAMES


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
    "path", ["app/records/page.tsx", "components/x/widget.tsx", "lib/util.ts"]
)
async def test_write_allowed_inside_the_surface(sink: CollectingSink, path: str) -> None:
    fake = FakeSandbox()
    await _run(
        fake, sink, [tool_turn("write_file", {"path": path, "file_text": "export const x = 1;\n"})]
    )
    assert fake.workspace[path] == "export const x = 1;\n"


@pytest.mark.parametrize(
    "path",
    [
        "lib/bial-data.ts",
        "components/ui/button.tsx",
        "package.json",
        ".git/config",
        ".git/hooks/pre-push",
    ],
)
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
    assert "outside the writable surface" in captured["all_incoming"]


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
    assert deps.done_requested is True
    assert deps.done_summary == "built it"
    assert any(getattr(e, "name", None) == "declare_done" for e in sink.events)


async def test_write_emits_a_step(sink: CollectingSink) -> None:
    fake = FakeSandbox()
    await _run(fake, sink, [tool_turn("write_file", {"path": "app/page.tsx", "file_text": "x\n"})])
    assert any(getattr(e, "name", None) == "edit" for e in sink.events)
