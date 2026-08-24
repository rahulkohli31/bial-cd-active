"""End-to-end `run_build` journeys over the fake (U9, KD-1..KD-13).

The full multi-run loop: happy path, self-heal re-seed, escalation, quota mid-loop — asserting the
envelope stream shape, seq monotonicity, and the returned `BuildResult`. BRAIN emits NO terminal
`ended` (R7): completion travels on the verdict, and SESSION-API renders the frame post-snapshot.
"""

from __future__ import annotations

import uuid
from typing import cast

from pydantic_ai import BinaryContent
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.anthropic import AnthropicModelSettings
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import RequestUsage
from sqlalchemy import select

from src.api.v1.build_sessions.schemas import BuildSessionStatus, ProgressEnvelope
from src.db.models.token_usage import TokenUsage
from src.services.orchestrator import constants, harness
from src.services.sandbox import (
    ExecResult,
    SandboxHandle,
    SandboxNotReadyError,
    ServedPage,
)
from tests.factories import UserFactory
from tests.services.orchestrator.conftest import CollectingSink, make_orchestrator
from tests.services.orchestrator.fake_sandbox import FakeSandbox
from tests.services.orchestrator.model_harness import scripted_model, text_turn, tool_turn


def _assert_seq_gap_free(events: list[ProgressEnvelope]) -> None:
    seqs = [e.seq for e in events]
    assert seqs == list(range(1, len(seqs) + 1)), f"seq not gap-free: {seqs}"


def _assert_no_terminal(events: list[ProgressEnvelope]) -> None:
    """BRAIN never emits `ended` on ANY path (R7) — the terminal frame is SESSION-API's, emitted
    after its C4 snapshot so it can carry a true `snapshot_committed`."""
    assert not [e for e in events if e.type == "ended"]


async def test_happy_path_scaffold_to_completed(db_session, billing_factory, sink) -> None:
    user = await UserFactory.create(db_session)
    fake = FakeSandbox()
    fake.dev_ready = True
    model = scripted_model(
        [
            tool_turn("write_file", {"path": "app/records/page.tsx", "file_text": "export {}\n"}),
            tool_turn("declare_done", {"summary": "records screen"}),
            text_turn("done"),
        ]
    )
    orchestrator, app_id = make_orchestrator(model, billing_factory)

    result = await orchestrator.run_build(uuid.uuid4(), user.id, fake, sink)

    _assert_seq_gap_free(sink.events)
    _assert_no_terminal(sink.events)
    types = [e.type for e in sink.events]
    assert "step" in types  # the write + declare_done steps
    assert "preview_ready" in types
    assert result.reason == "completed"
    assert result.status == BuildSessionStatus.ENDED
    assert result.app_id == app_id
    assert result.last_seq == sink.events[-1].seq  # the verdict hands the seq baton over
    assert result.snapshot_committed is False
    assert fake.workspace["app/records/page.tsx"] == "export {}\n"  # the feature file landed
    assert fake.dev_start_calls == 1
    assert fake.teardown_calls == 0


async def test_self_heal_reseed_then_completes(db_session, billing_factory, sink) -> None:
    user = await UserFactory.create(db_session)
    fake = FakeSandbox()
    fake.dev_ready = True
    fake.queue_commands(ExecResult(stdout="error TS2322: red once", stderr="", exit=2))  # run1 red
    fake.queue_commands(ExecResult(stdout="", stderr="", exit=0))  # run2 green
    model = scripted_model(
        [
            tool_turn("declare_done", {"summary": "attempt 1"}),
            text_turn(),
            tool_turn("declare_done", {"summary": "attempt 2"}),
            text_turn(),
        ]
    )
    orchestrator, _ = make_orchestrator(model, billing_factory)

    result = await orchestrator.run_build(uuid.uuid4(), user.id, fake, sink)

    _assert_seq_gap_free(sink.events)
    _assert_no_terminal(sink.events)
    assert len([e for e in sink.events if e.type == "error"]) == 1  # red once, one repair
    assert result.status == BuildSessionStatus.ENDED
    assert result.reason == "completed"


async def test_escalation_after_budget(db_session, billing_factory, sink) -> None:
    user = await UserFactory.create(db_session)
    fake = FakeSandbox()
    fake.dev_ready = True
    for _ in range(4):
        fake.queue_commands(ExecResult(stdout="error TS2322: still red", stderr="", exit=2))
    turns = [
        t for _ in range(4) for t in (tool_turn("declare_done", {"summary": "x"}), text_turn())
    ]
    model = scripted_model(turns)
    orchestrator, _ = make_orchestrator(model, billing_factory)

    result = await orchestrator.run_build(uuid.uuid4(), user.id, fake, sink)

    _assert_seq_gap_free(sink.events)
    _assert_no_terminal(sink.events)
    assert any(e.type == "escalation" for e in sink.events)
    assert result.status == BuildSessionStatus.FAILED
    assert result.reason == "build_failed"


async def test_wall_clock_deadline_escalates_like_the_turn_ceiling(
    db_session, billing_factory, sink, monkeypatch
) -> None:
    # A build that blows its wall-clock deadline must escalate through the SAME funnel the
    # turn/self-heal ceilings use (escalation -> ended(build_failed)), never hold the container +
    # sandbox lock indefinitely. A negative deadline trips the check on loop entry.
    monkeypatch.setattr(harness, "RUN_WALL_CLOCK_DEADLINE_S", -1.0)
    user = await UserFactory.create(db_session)
    fake = FakeSandbox()
    fake.dev_ready = True
    model = scripted_model([tool_turn("declare_done", {"summary": "x"}), text_turn()])
    orchestrator, _ = make_orchestrator(model, billing_factory)

    result = await orchestrator.run_build(uuid.uuid4(), user.id, fake, sink)

    _assert_seq_gap_free(sink.events)
    _assert_no_terminal(sink.events)
    assert result.status == BuildSessionStatus.FAILED
    escalations = [e for e in sink.events if e.type == "escalation"]
    assert len(escalations) == 1 and escalations[0].reason == "wall_clock_deadline_exceeded"
    assert result.reason == "build_failed"
    assert fake.teardown_calls == 0  # BRAIN never tears down (KD-11)


async def test_attach_not_ready_twice_then_ready_still_builds(
    db_session, billing_factory, sink, monkeypatch
) -> None:
    # Cold-ACA ingress: the first two attach probes report NotReady; the bounded re-probe absorbs
    # them and the build runs to completion instead of a hard internal_error FAILED.
    monkeypatch.setattr(harness, "ATTACH_RETRY_BACKOFF_S", 0.0)
    user = await UserFactory.create(db_session)
    fake = FakeSandbox()
    fake.dev_ready = True
    fake.queue_attach_errors(
        SandboxNotReadyError("cold ingress"), SandboxNotReadyError("still waking")
    )
    model = scripted_model([tool_turn("declare_done", {"summary": "x"}), text_turn()])
    orchestrator, _ = make_orchestrator(model, billing_factory)

    result = await orchestrator.run_build(uuid.uuid4(), user.id, fake, sink)

    _assert_seq_gap_free(sink.events)
    _assert_no_terminal(sink.events)
    assert result.status == BuildSessionStatus.ENDED
    assert result.reason == "completed"
    assert not any(e.type == "escalation" for e in sink.events)
    assert fake.attach_calls == 3  # two cold probes + the attach that landed


async def test_attach_persistently_not_ready_fails_after_bounded_reprobe(
    db_session, billing_factory, sink, monkeypatch
) -> None:
    monkeypatch.setattr(harness, "ATTACH_RETRY_BACKOFF_S", 0.0)
    user = await UserFactory.create(db_session)
    fake = FakeSandbox()
    attempts = constants.ATTACH_NOT_READY_RETRIES + 1
    fake.queue_attach_errors(
        *(SandboxNotReadyError("ingress never woke") for _ in range(attempts))
    )
    model = scripted_model([text_turn()])
    orchestrator, _ = make_orchestrator(model, billing_factory)

    result = await orchestrator.run_build(uuid.uuid4(), user.id, fake, sink)

    _assert_seq_gap_free(sink.events)
    _assert_no_terminal(sink.events)
    assert result.status == BuildSessionStatus.FAILED
    escalations = [e for e in sink.events if e.type == "escalation"]
    assert len(escalations) == 1 and escalations[0].reason == "internal_error"
    assert fake.attach_calls == attempts  # the bounded window was spent, then escalated as today


async def test_quota_mid_loop_is_graceful(db_session, billing_factory, sink) -> None:
    user = await UserFactory.create(db_session)
    from src.db.models.user_limit import UserLimit

    db_session.add(UserLimit(user_id=user.id, daily_token_limit=100))
    await db_session.flush()
    fake = FakeSandbox()
    fake.dev_ready = True
    model = scripted_model(
        [
            tool_turn(
                "write_file",
                {"path": "app/page.tsx", "file_text": "x\n"},
                usage=RequestUsage(input_tokens=100, output_tokens=0),
            ),
            text_turn("blocked", usage=RequestUsage(input_tokens=500, output_tokens=500)),
        ]
    )
    orchestrator, _ = make_orchestrator(model, billing_factory)

    result = await orchestrator.run_build(uuid.uuid4(), user.id, fake, sink)

    _assert_seq_gap_free(sink.events)
    _assert_no_terminal(sink.events)
    assert result.status == BuildSessionStatus.ENDED  # graceful
    assert result.reason == "quota_exceeded"
    # Cumulative usage equals only the step that actually ran (the blocked step never billed).
    row = await db_session.scalar(select(TokenUsage).where(TokenUsage.user_id == user.id))
    assert row is not None and row.input_tokens == 100


async def _capture_model_settings(
    db_session, billing_factory, sink
) -> AnthropicModelSettings | None:
    """Drive one full build through a FunctionModel and return the settings the harness threaded
    into the model call (`AgentInfo.model_settings` — the real `agent.iter` seam, not a stub)."""
    user = await UserFactory.create(db_session)
    fake = FakeSandbox()
    fake.dev_ready = True
    captured: dict[str, ModelSettings | None] = {}
    scripted = iter([tool_turn("declare_done", {"summary": "x"}), text_turn()])

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        captured["settings"] = info.model_settings
        return next(scripted, text_turn("done"))

    orchestrator, _ = make_orchestrator(FunctionModel(respond), billing_factory)
    await orchestrator.run_build(uuid.uuid4(), user.id, fake, sink)
    # The harness passes an AnthropicModelSettings; AgentInfo types it as its ModelSettings base.
    return cast(AnthropicModelSettings | None, captured["settings"])


async def test_model_steps_are_clamped_with_output_and_temperature(
    db_session, billing_factory, sink
) -> None:
    settings = await _capture_model_settings(db_session, billing_factory, sink)

    assert settings is not None
    # The clamps are actually THREADED into the model call — without them pydantic-ai applies its
    # own max_tokens=4096 default, truncating a whole-file write (the wiring, not just the value).
    assert settings.get("max_tokens") == constants.MAX_OUTPUT_TOKENS
    assert settings.get("temperature") == constants.TEMPERATURE


async def test_model_steps_carry_anthropic_cache_breakpoints(
    db_session, billing_factory, sink
) -> None:
    # R1: the loop re-sends the system prompt + tool definitions verbatim on EVERY step, so all
    # three breakpoints must reach the model call — asserting the wiring, not just the constant.
    settings = await _capture_model_settings(db_session, billing_factory, sink)

    assert settings is not None
    assert settings.get("anthropic_cache_instructions") == constants.CACHE_TTL
    assert settings.get("anthropic_cache_tool_definitions") == constants.CACHE_TTL
    assert settings.get("anthropic_cache") == constants.CACHE_TTL  # automatic, for message growth
    # 1h, not the 5m that `True` would select: inter-step gaps (a 600s `run_command` npm install)
    # can outlive a 5m entry, making every step pay the write premium and read nothing back.
    assert constants.CACHE_TTL == "1h"
    # `anthropic_cache_messages` cannot be combined with `anthropic_cache` (pydantic-ai 2.5.0), and
    # 3 explicit breakpoints stay under Anthropic's 4-slot ceiling.
    assert "anthropic_cache_messages" not in settings


async def test_declare_done_started_step_is_resolved_not_left_hanging(
    db_session, billing_factory, sink
) -> None:
    user = await UserFactory.create(db_session)
    fake = FakeSandbox()
    fake.dev_ready = True
    model = scripted_model([tool_turn("declare_done", {"summary": "x"}), text_turn()])
    orchestrator, _ = make_orchestrator(model, billing_factory)

    await orchestrator.run_build(uuid.uuid4(), user.id, fake, sink)

    declare_steps = [e for e in sink.events if e.type == "step" and e.name == "declare_done"]
    states = [e.state for e in declare_steps]
    assert "started" in states  # the tool opened the "Verifying…" spinner …
    assert "ok" in states  # … and the harness resolved it once verify was green (no orphan)


async def test_metering_sum_matches_scripted_usage(db_session, billing_factory) -> None:
    user = await UserFactory.create(db_session)
    fake = FakeSandbox()
    fake.dev_ready = True
    sink = CollectingSink()
    # A NON-TERMINAL first step: a passing `declare_done` ends the run outright (U18/R30), so a
    # script that led with it would only ever buy ONE model step and this sum would measure one
    # usage rather than the folding of two.
    model = scripted_model(
        [
            tool_turn(
                "write_file",
                {"path": "app/page.tsx", "file_text": "export {}\n"},
                usage=RequestUsage(input_tokens=7, output_tokens=8),
            ),
            tool_turn(
                "declare_done",
                {"summary": "x"},
                usage=RequestUsage(input_tokens=9, output_tokens=10),
            ),
        ]
    )
    orchestrator, _ = make_orchestrator(model, billing_factory)

    await orchestrator.run_build(uuid.uuid4(), user.id, fake, sink)

    row = await db_session.scalar(select(TokenUsage).where(TokenUsage.user_id == user.id))
    assert row is not None
    assert row.input_tokens == 16  # 7 + 9
    assert row.output_tokens == 18  # 8 + 10


async def test_a_green_declare_done_ends_the_legacy_harness_run_too(
    db_session, billing_factory, sink
) -> None:
    """★ U18/R30 ON THE OTHER CONSUMER OF THE SAME TOOL.

    `declare_done` is ONE tool body serving two harnesses, and its return text now tells the model
    "this turn ends here and nothing further is asked of you" on a passing check. The Write engine
    was taught to mean it; this loop was not, so it walked into one more real, paid model request
    whose entire premise the model had just been told was false — and the closing paragraph that
    request bought is rendered nowhere.

    Mutation check: delete the `done_requested` break in `_run_one`'s node loop and the request
    count goes to 3."""
    user = await UserFactory.create(db_session)
    fake = FakeSandbox()
    fake.dev_ready = True
    requests = {"n": 0}
    turns = iter(
        [
            tool_turn("write_file", {"path": "app/page.tsx", "file_text": "export {}\n"}),
            tool_turn("declare_done", {"summary": "the visitor list is live"}),
            text_turn("nobody should ever ask for this"),
        ]
    )

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        requests["n"] += 1
        return next(turns)

    orchestrator, _ = make_orchestrator(FunctionModel(respond), billing_factory)

    result = await orchestrator.run_build(uuid.uuid4(), user.id, fake, sink)

    # LIVENESS FIRST: the build really did run to a green completion. A cut that ended the run by
    # breaking it would satisfy the count below and mean the opposite of what this asserts.
    assert result.reason == "completed"
    assert fake.workspace["app/page.tsx"] == "export {}\n"
    assert requests["n"] == 2, "a third request means declare_done still buys a round-trip here"


async def test_multimodal_prompt_reaches_the_model(db_session, billing_factory, sink) -> None:
    """R3 — the whole point of the unit: what SESSION-API materialized actually arrives at the
    model. Captures the first request's parts off a `FunctionModel` and asserts the instruction
    text, the fenced office markdown, and the image BYTES are all there.

    Before R3 this content never left the database: the build ran on `prompt` alone and the user's
    spreadsheet was silently ignored.
    """
    user = await UserFactory.create(db_session)
    fake = FakeSandbox()
    fake.dev_ready = True
    png = bytes([0x89, 0x50, 0x4E, 0x47]) + b"\x00 pretend png"
    captured: list[ModelMessage] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if not captured:
            captured.extend(messages)
        if len(captured) and not any(
            isinstance(p, ToolCallPart) for m in messages for p in m.parts
        ):
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="declare_done", args={"summary": "s"}, tool_call_id="c1"
                    )
                ],
                usage=RequestUsage(input_tokens=1, output_tokens=1),
            )
        return ModelResponse(
            parts=[TextPart(content="done")], usage=RequestUsage(input_tokens=1, output_tokens=1)
        )

    orchestrator, _ = make_orchestrator(
        FunctionModel(respond),
        billing_factory,
        prompt=[
            "build me a dashboard",
            '<attachment name="sales.xlsx" type="excel">\n| Q1 | 100 |\n</attachment>',
            BinaryContent(data=png, media_type="image/png"),
        ],
    )

    await orchestrator.run_build(uuid.uuid4(), user.id, fake, sink)

    user_parts = [p for m in captured for p in m.parts if isinstance(p, UserPromptPart)]
    assert user_parts, "the model never received a user prompt"
    content = user_parts[0].content
    assert isinstance(content, list)
    # Passthrough: BRAIN hands `BuildSpec.prompt` to the model in the order it was given, and
    # does not reorder. The canonical ORDER is decided by the run-context provider and pinned in
    # `tests/api/v1/build_sessions/test_run_context_spec.py` — not here.
    assert content[0] == "build me a dashboard"
    assert "| Q1 | 100 |" in str(content[1])
    binaries = [c for c in content if isinstance(c, BinaryContent)]
    assert len(binaries) == 1
    assert binaries[0].data == png  # the actual bytes, not a reference the model can't read
    assert binaries[0].media_type == "image/png"


async def test_text_only_prompt_still_reaches_the_model_as_a_bare_string(
    db_session, billing_factory, sink
) -> None:
    """Back-compat: no attachments → the pre-R3 shape (a plain string), not a 1-element list."""
    user = await UserFactory.create(db_session)
    fake = FakeSandbox()
    fake.dev_ready = True
    captured: list[ModelMessage] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if not captured:
            captured.extend(messages)
        return ModelResponse(
            parts=[TextPart(content="done")], usage=RequestUsage(input_tokens=1, output_tokens=1)
        )

    orchestrator, _ = make_orchestrator(
        FunctionModel(respond), billing_factory, prompt="just build it"
    )

    await orchestrator.run_build(uuid.uuid4(), user.id, fake, sink)

    user_parts = [p for m in captured for p in m.parts if isinstance(p, UserPromptPart)]
    assert user_parts[0].content == "just build it"


class _WarmOrderSandbox(FakeSandbox):
    """A `FakeSandbox` that records how many `preview_ready` envelopes the sink had ALREADY seen
    at the moment the warm request was made.

    Asserting that both the warm and the frame occurred proves nothing — the unit is entirely
    about which came first. A subclass rather than a monkeypatched attribute because assigning
    over a bound method is a type error under `ty`, and the fakes here are subclassed anyway.

    It IS the fake, deliberately — an earlier cut took an `inner: FakeSandbox` and copied two
    attributes off it, which silently discarded any `queue_commands` / `push_dev_logs` /
    `compile_error_appears_on_first_request` programming the caller had done. Two fakes where
    only one is driven is the green-for-the-wrong-reason trap `.claude/rules/testing.md` names."""

    def __init__(self, sink: CollectingSink) -> None:
        super().__init__()
        self._sink = sink
        self.frames_when_warmed: list[int] = []

    async def someone_has_to_go_first(self, handle: SandboxHandle) -> int | None:
        self._record()
        return await super().someone_has_to_go_first(handle)

    async def what_is_it_serving(self, handle: SandboxHandle) -> ServedPage | None:
        # BOTH doors are recorded, because both are requests at the app root and the unit is
        # about which of them happened before the frame. U6 moved the verify-side request from
        # `someone_has_to_go_first` to this one; recording only the first would have left this
        # test asserting a single warm and quietly stopped covering the second.
        self._record()
        return await super().what_is_it_serving(handle)

    def _record(self) -> None:
        framed = [e for e in self._sink.events if e.type == "preview_ready"]
        self.frames_when_warmed.append(len(framed))


async def test_the_legacy_build_warms_the_route_before_it_frames_the_preview(
    db_session, billing_factory, sink
) -> None:
    """★ U3 (R3) on the legacy path. Four sites here can emit `preview_ready` — the warm-resume,
    verify's fallback, and the watcher's two arms — and a warm request wired into three of them
    is a preview that still mounts onto an uncompiled route on the fourth. `_frame_the_preview`
    is the single door; this asserts the ordering it exists to guarantee, not merely that both
    calls happened."""
    user = await UserFactory.create(db_session)
    fake = _WarmOrderSandbox(sink)
    fake.dev_ready = True
    model = scripted_model(
        [
            tool_turn("write_file", {"path": "app/page.tsx", "file_text": "export {}\n"}),
            tool_turn("declare_done", {"summary": "home"}),
            text_turn("done"),
        ]
    )
    orchestrator, _ = make_orchestrator(model, billing_factory)

    await orchestrator.run_build(uuid.uuid4(), user.id, fake, sink)

    frames = [e for e in sink.events if e.type == "preview_ready"]
    assert len(frames) == 1, "one frame, claimed once across the warm-resume/verify/watcher sites"
    assert fake.frames_when_warmed[0] == 0, (
        "the first route was compiled before the citizen's iframe was told to mount"
    )
    # A build requests the root TWICE and both are deliberate: U3 pays the compile before the
    # frame, and `verify` asks again (through U6's serving probe) so a Next-only compile error
    # lands in the log window `detect_server_crash` reads — and so the health verdict has a status
    # of its own to decide on. The one that matters for R3 is the one before the frame.
    assert fake.frames_when_warmed == [0, 1]
