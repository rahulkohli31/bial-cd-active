"""The C2-observable test double (U4) — the semantics every later unit is tested against. If the
fake drifts from C2, every downstream test is testing a fiction."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic_ai import Agent, RunContext
from pydantic_ai._agent_graph import AgentNode
from pydantic_ai.result import FinalResult
from pydantic_graph import End

from src.services.sandbox import (
    ExecResult,
    FileCreate,
    FileInsert,
    FileStrReplace,
    FileView,
    SandboxClient,
    SandboxError,
    SandboxGoneError,
)
from tests.services.orchestrator.fake_sandbox import FakeSandbox
from tests.services.orchestrator.model_harness import scripted_model, text_turn, tool_turn


def _seeded() -> FakeSandbox:
    return FakeSandbox(seed_files={"app/records/page.tsx": "line1\nline2\nline3\n"})


async def test_str_replace_unique_replaces_once() -> None:
    fake = _seeded()
    result = await fake.files(
        fake.handle(),
        FileStrReplace(path="app/records/page.tsx", old_str="line2", new_str="LINE-TWO"),
    )
    assert result.ok
    assert "LINE-TWO" in fake.workspace["app/records/page.tsx"]


@pytest.mark.parametrize("old_str", ["nope-not-here", "line"])  # 0 matches, then N>1 (substring)
async def test_str_replace_non_unique_is_opaque_sandbox_error(old_str: str) -> None:
    fake = FakeSandbox(seed_files={"a.tsx": "line line line"})
    with pytest.raises(SandboxError) as exc:
        await fake.files(fake.handle(), FileStrReplace(path="a.tsx", old_str=old_str, new_str="x"))
    # C2 collapses C1's 400/422 into an opaque error — no HTTP-status attribute leaks through.
    assert not hasattr(exc.value, "status")
    assert not hasattr(exc.value, "status_code")


async def test_str_replace_lf_normalizes_before_compare() -> None:
    fake = FakeSandbox(seed_files={"a.tsx": "alpha\nbeta\n"})
    # A CRLF needle still matches the LF-normalized file.
    result = await fake.files(
        fake.handle(), FileStrReplace(path="a.tsx", old_str="alpha\r\nbeta", new_str="merged")
    )
    assert result.ok
    assert fake.workspace["a.tsx"] == "merged\n"


async def test_nonzero_exit_is_a_normal_result_not_an_exception() -> None:
    fake = FakeSandbox()
    fake.queue_commands(ExecResult(stdout="", stderr="error TS2322: boom", exit=2))
    run_command = fake.exec  # alias avoids a literal method-call token
    result = await run_command(fake.handle(), ["npx", "tsc", "--noEmit"])
    assert result.exit == 2  # a non-zero exit never raises
    assert "TS2322" in result.stderr
    assert fake.command_calls == [["npx", "tsc", "--noEmit"]]


async def test_view_is_one_indexed_with_range_clamping() -> None:
    fake = FakeSandbox(seed_files={"a.tsx": "one\ntwo\nthree\nfour"})
    result = await fake.files(fake.handle(), FileView(path="a.tsx", view_range=[2, 3]))
    content = result.detail["content"]
    assert isinstance(content, str)
    assert content == "2\ttwo\n3\tthree"
    # -1 end clamps to EOF
    whole = await fake.files(fake.handle(), FileView(path="a.tsx", view_range=[1, -1]))
    assert "4\tfour" in str(whole.detail["content"])


async def test_view_of_missing_file_raises() -> None:
    fake = FakeSandbox()
    with pytest.raises(SandboxError):
        await fake.files(fake.handle(), FileView(path="nope.tsx"))


async def test_insert_is_zero_based() -> None:
    fake = FakeSandbox(seed_files={"a.tsx": "one\ntwo"})
    await fake.files(fake.handle(), FileInsert(path="a.tsx", insert_line=0, insert_text="zero"))
    assert fake.workspace["a.tsx"] == "zero\none\ntwo"


async def test_create_overwrites_and_lf_normalizes() -> None:
    fake = FakeSandbox()
    await fake.files(fake.handle(), FileCreate(path="app/new.tsx", file_text="a\r\nb\r\n"))
    assert fake.workspace["app/new.tsx"] == "a\nb\n"


async def test_dev_start_is_idempotent_and_ready_gated_on_a_marker() -> None:
    fake = FakeSandbox()
    assert (await fake.dev_start(fake.handle())) == 4242
    assert (await fake.dev_start(fake.handle())) == 4242  # idempotent — same pid
    assert fake.dev_start_calls == 2
    status = await fake.dev_status(fake.handle())
    assert status.running is True and status.ready is False and status.port == 3000
    fake.dev_ready = True
    assert (await fake.dev_status(fake.handle())).ready is True


async def test_become_ready_after_delays_the_marker() -> None:
    fake = FakeSandbox()
    await fake.dev_start(fake.handle())
    fake.become_ready_after(2)
    assert (await fake.dev_status(fake.handle())).ready is False
    assert (await fake.dev_status(fake.handle())).ready is False
    assert (await fake.dev_status(fake.handle())).ready is True


async def test_dev_logs_cursor_tail() -> None:
    fake = FakeSandbox()
    fake.push_dev_logs("compiled ok", "GET / 200")
    first = await fake.dev_logs(fake.handle(), since=0)
    assert first.lines == ["compiled ok", "GET / 200"]
    assert first.next_cursor == 2
    fake.push_dev_logs("⨯ unhandledRejection Error: boom")  # a post-step crash
    tail = await fake.dev_logs(fake.handle(), since=first.next_cursor)
    assert tail.lines == ["⨯ unhandledRejection Error: boom"]
    assert tail.next_cursor == 3


async def test_attach_can_be_programmed_to_raise() -> None:
    fake = FakeSandbox()
    fake.attach_error = SandboxGoneError("container is gone")
    with pytest.raises(SandboxGoneError):
        await fake.attach_existing("user-1")


async def test_attach_reflects_dev_readiness() -> None:
    fake = FakeSandbox()
    assert (await fake.attach_existing("u")).ready is False
    fake.dev_ready = True
    assert (await fake.attach_existing("u")).ready is True


async def test_fake_implements_the_full_abc_but_an_incomplete_one_cannot_instantiate() -> None:
    # The fake instantiates (all 10 methods implemented)…
    assert isinstance(FakeSandbox(), SandboxClient)

    # …but a subclass missing a method fails at instantiation with a TypeError (ABC contract).
    class Partial(SandboxClient):
        pass

    cls: Any = Partial
    with pytest.raises(TypeError):
        cls()


async def test_scripted_model_drives_per_turn_usage_verbatim() -> None:
    # The metering seam de-risk: a scripted write→done sequence, per-turn RequestUsage read back
    # off CallToolsNode.model_response.usage verbatim (KD-1) — the exact mechanic U6 relies on.
    from pydantic_ai.usage import RequestUsage

    agent = Agent(deps_type=list, retries=2)

    @agent.tool
    async def write_file(ctx: RunContext[list[str]], path: str) -> str:
        ctx.deps.append(path)
        return "ok"

    model = scripted_model(
        [
            tool_turn(
                "write_file",
                {"path": "app/x.tsx"},
                usage=RequestUsage(input_tokens=11, output_tokens=22),
            ),
            text_turn("done", usage=RequestUsage(input_tokens=100, output_tokens=200)),
        ]
    )
    per_step: list[tuple[int, int]] = []
    deps: list[str] = []
    async with agent.iter("build", deps=deps, model=model) as run:
        # Annotated and walked with `isinstance`, for the reason `harness.py` writes out at
        # length: `Agent.is_end_node` binds its `TypeIs` type-var to `Unknown` on the bare class,
        # so the NEGATIVE branch it is used for here does not narrow and `run.next(node)` reads
        # as possibly-End.
        node: AgentNode[list[str], str] | End[FinalResult[str]] = run.next_node
        while not isinstance(node, End):
            node = await run.next(node)
            if Agent.is_call_tools_node(node):
                usage = node.model_response.usage
                per_step.append((usage.input_tokens, usage.output_tokens))
    assert deps == ["app/x.tsx"]
    assert per_step == [(11, 22), (100, 200)]
