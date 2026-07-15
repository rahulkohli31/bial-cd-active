"""End-to-end `run_build` journeys over the fake (U9, KD-1..KD-13).

The full multi-run loop: happy path, self-heal re-seed, escalation, quota mid-loop — asserting the
envelope stream shape, seq monotonicity, the single terminal `ended`, and BuildResult agreement.
"""

from __future__ import annotations

import uuid

from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import RequestUsage
from sqlalchemy import select

from src.api.v1.build_sessions.schemas import BuildSessionStatus, ProgressEnvelope
from src.db.models.token_usage import TokenUsage
from src.services.orchestrator import constants, harness
from src.services.sandbox import ExecResult, SandboxNotReadyError
from tests.factories import UserFactory
from tests.services.orchestrator.conftest import CollectingSink, make_orchestrator
from tests.services.orchestrator.fake_sandbox import FakeSandbox
from tests.services.orchestrator.model_harness import scripted_model, text_turn, tool_turn


def _assert_seq_gap_free(events: list[ProgressEnvelope]) -> None:
    seqs = [e.seq for e in events]
    assert seqs == list(range(1, len(seqs) + 1)), f"seq not gap-free: {seqs}"


def _assert_one_terminal(events: list[ProgressEnvelope]) -> None:
    ended = [e for e in events if e.type == "ended"]
    assert len(ended) == 1
    assert ended[0] is events[-1]  # always last (highest seq)


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
    _assert_one_terminal(sink.events)
    types = [e.type for e in sink.events]
    assert "step" in types  # the write + declare_done steps
    assert "preview_ready" in types
    assert sink.events[-1].reason == "completed"
    assert result.status == BuildSessionStatus.ENDED
    assert result.app_id == app_id
    assert result.last_seq == sink.events[-1].seq
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
    _assert_one_terminal(sink.events)
    assert len([e for e in sink.events if e.type == "error"]) == 1  # red once, one repair
    assert result.status == BuildSessionStatus.ENDED
    assert sink.events[-1].reason == "completed"


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
    _assert_one_terminal(sink.events)
    assert any(e.type == "escalation" for e in sink.events)
    assert result.status == BuildSessionStatus.FAILED
    assert sink.events[-1].reason == "build_failed"


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
    _assert_one_terminal(sink.events)
    assert result.status == BuildSessionStatus.ENDED
    assert sink.events[-1].reason == "completed"
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
    _assert_one_terminal(sink.events)
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
    _assert_one_terminal(sink.events)
    assert result.status == BuildSessionStatus.ENDED  # graceful
    assert sink.events[-1].reason == "quota_exceeded"
    # Cumulative usage equals only the step that actually ran (the blocked step never billed).
    row = await db_session.scalar(select(TokenUsage).where(TokenUsage.user_id == user.id))
    assert row is not None and row.input_tokens == 100


async def test_model_steps_are_clamped_with_output_and_temperature(
    db_session, billing_factory, sink
) -> None:
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

    settings = captured["settings"]
    assert settings is not None
    # The clamps are actually THREADED into the model call — without them pydantic-ai applies its
    # own max_tokens=4096 default, truncating a whole-file write (the wiring, not just the value).
    assert settings.get("max_tokens") == constants.MAX_OUTPUT_TOKENS
    assert settings.get("temperature") == constants.TEMPERATURE


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
    model = scripted_model(
        [
            tool_turn(
                "declare_done",
                {"summary": "x"},
                usage=RequestUsage(input_tokens=7, output_tokens=8),
            ),
            text_turn(usage=RequestUsage(input_tokens=9, output_tokens=10)),
        ]
    )
    orchestrator, _ = make_orchestrator(model, billing_factory)

    await orchestrator.run_build(uuid.uuid4(), user.id, fake, sink)

    row = await db_session.scalar(select(TokenUsage).where(TokenUsage.user_id == user.id))
    assert row is not None
    assert row.input_tokens == 16  # 7 + 9
    assert row.output_tokens == 18  # 8 + 10
