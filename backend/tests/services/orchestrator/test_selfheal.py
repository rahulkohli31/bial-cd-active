"""The self-heal state machine (U7, KD-5/6/7/8).

Two layers: the pure harness-verify primitives (no DB) and the full multi-run loop through
`run_build` (metered, so DB-backed). The loop tests assert the re-seed channel — a harness-observed
error becomes the next run's prompt — and the flat 3-run budget → escalation.
"""

from __future__ import annotations

import uuid

from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models.function import AgentInfo, FunctionModel

from src.api.v1.build_sessions.schemas import BuildSessionStatus, ErrorSource
from src.services.orchestrator import constants, selfheal
from src.services.orchestrator.selfheal import (
    are_we_there_yet,
    detect_server_crash,
    verify,
)
from src.services.sandbox import ExecResult, SandboxError, SandboxGoneError
from tests.factories import UserFactory
from tests.services.orchestrator.conftest import make_orchestrator
from tests.services.orchestrator.fake_sandbox import FakeSandbox
from tests.services.orchestrator.model_harness import scripted_model, text_turn, tool_turn

# =============================================================================
# Pure verify primitives — no DB
# =============================================================================


def test_detect_server_crash_matches_markers_not_benign_lines() -> None:
    assert detect_server_crash(["GET / 200 in 30ms", "compiled ok"]) is None
    crash = detect_server_crash(["  ⨯ unhandledRejection Error: boom", "  at RecordsPage"])
    assert crash is not None and "boom" in crash


async def test_are_we_there_yet_ready_after_polls() -> None:
    fake = FakeSandbox()
    await fake.dev_start(fake.handle())
    fake.become_ready_after(2)
    assert await are_we_there_yet(fake, fake.handle(), max_polls=5, poll_s=0.0) is True


async def test_are_we_there_yet_dead_process_is_not_slow() -> None:
    fake = FakeSandbox()  # dev_running False, never started
    assert await are_we_there_yet(fake, fake.handle(), max_polls=5, poll_s=0.0) is False


async def test_are_we_there_yet_believes_ready_over_a_dead_child() -> None:
    """U1's new `/dev/status` row (`running=False, ready=True`): the supervisor's child is dead
    but an agent-relaunched server answers the dev port — observed truth says serving. The
    `ready` check runs FIRST, so this is "there", never the dead-process fast-fail. Pins that
    ordering: swap the two checks and this goes red."""
    fake = FakeSandbox()
    fake.dev_running = False
    fake.dev_ready = True
    assert await are_we_there_yet(fake, fake.handle(), max_polls=5, poll_s=0.0) is True


async def test_verify_green_when_tsc_clean_and_dev_ready() -> None:
    fake = FakeSandbox()
    fake.dev_ready = True  # tsc default exit 0
    outcome, _ = await verify(fake, fake.handle(), log_cursor=0, max_polls=3, poll_s=0.0)
    assert outcome.green is True
    assert outcome.error is None
    assert outcome.dev_ready is True


async def test_verify_red_on_tsc_failure_builds_a_tsc_error() -> None:
    fake = FakeSandbox()
    fake.dev_ready = True
    fake.queue_commands(ExecResult(stdout="app/x.tsx(1,1): error TS2322: bad", stderr="", exit=2))
    outcome, _ = await verify(fake, fake.handle(), log_cursor=0, max_polls=3, poll_s=0.0)
    assert outcome.green is False
    assert outcome.error is not None and outcome.error.source == ErrorSource.TSC


async def test_verify_red_on_server_crash_builds_a_server_error() -> None:
    fake = FakeSandbox()
    fake.dev_ready = True  # tsc green, but the dev log tail shows a crash
    fake.push_dev_logs("⨯ unhandledRejection Error: cannot read properties of undefined")
    outcome, _ = await verify(fake, fake.handle(), log_cursor=0, max_polls=3, poll_s=0.0)
    assert outcome.green is False
    assert outcome.error is not None and outcome.error.source == ErrorSource.SERVER


async def test_verify_never_runs_next_build() -> None:
    fake = FakeSandbox()
    fake.dev_ready = True
    await verify(fake, fake.handle(), log_cursor=0, max_polls=3, poll_s=0.0)
    # Only `tsc` is ever run between runs — `next build` is a DEPLOY concern (D2/KD-6).
    assert fake.command_calls == [["npx", "tsc", "--noEmit"]]
    assert not any("build" in " ".join(cmd) for cmd in fake.command_calls)


async def test_verify_bounds_the_tsc_run_with_exec_timeout() -> None:
    fake = FakeSandbox()
    fake.dev_ready = True
    await verify(fake, fake.handle(), log_cursor=0, max_polls=3, poll_s=0.0)
    # The tsc run is bounded by EXEC_TIMEOUT_S (300s), NOT the ABC's 900s default (KD-8) — the
    # constant is actually threaded to the call, not merely defined.
    assert fake.command_timeouts == [constants.EXEC_TIMEOUT_S]


async def test_verify_bounds_the_dev_log_tail() -> None:
    fake = FakeSandbox()
    fake.dev_ready = True
    fake.push_dev_logs("EARLY_SENTINEL_LINE")  # oldest line, beyond the tail window
    fake.push_dev_logs(*[f"filler {i}" for i in range(constants.LOG_TAIL_MAX_LINES + 50)])
    fake.push_dev_logs("⨯ unhandledRejection Error: boom at the tail")  # crash at the very end
    outcome, _ = await verify(fake, fake.handle(), log_cursor=0, max_polls=3, poll_s=0.0)
    # The crash at the tail is still detected …
    assert outcome.error is not None and outcome.error.source == ErrorSource.SERVER
    # … but only the last LOG_TAIL_MAX_LINES lines are scanned — the oldest line is dropped.
    assert "EARLY_SENTINEL_LINE" not in outcome.error.cleaned_stack


# =============================================================================
# The full multi-run loop — DB-backed (metered per step)
# =============================================================================


def _seed_capturing_model(turns: list[ModelResponse], seeds: list[str]) -> FunctionModel:
    """Replays `turns` and records the newest user-prompt text seen at each model call into
    `seeds` — so a test can assert the redacted diagnostic re-seeds the next run (KD-1/KD-5)."""
    iterator = iter(turns)

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        for message in messages:
            for part in getattr(message, "parts", []):
                if getattr(part, "part_kind", "") == "user-prompt":
                    content = getattr(part, "content", None)
                    if isinstance(content, str):
                        seeds.append(content)
        return next(iterator, text_turn("done"))

    return FunctionModel(respond)


async def test_tsc_red_across_budget_escalates_with_reseed(
    db_session, billing_factory, sink
) -> None:
    user = await UserFactory.create(db_session)
    fake = FakeSandbox()
    fake.dev_ready = True
    # tsc red on every verify (initial + 3 repair runs = 4 verifies).
    for _ in range(4):
        fake.queue_commands(
            ExecResult(stdout="app/x.tsx(1,1): error TS2322: boom", stderr="", exit=2)
        )
    seeds: list[str] = []
    turns = [
        t for _ in range(4) for t in (tool_turn("declare_done", {"summary": "x"}), text_turn())
    ]
    model = _seed_capturing_model(turns, seeds)
    orchestrator, _ = make_orchestrator(model, billing_factory)

    result = await orchestrator.run_build(uuid.uuid4(), user.id, fake, sink)

    assert result.status == BuildSessionStatus.FAILED
    errors = [e for e in sink.events if e.type == "error"]
    assert len(errors) == 3  # 3 error envelopes before the 3 repair runs
    assert all(e.source == ErrorSource.TSC for e in errors)
    escalations = [e for e in sink.events if e.type == "escalation"]
    assert len(escalations) == 1 and escalations[0].reason == "self_heal_budget_exhausted"
    assert result.reason == "build_failed"  # on the verdict; BRAIN emits no terminal (R7)
    # The redacted diagnostic re-seeded the later runs (the harness→model feedback channel).
    assert any("error TS2322" in seed for seed in seeds[1:])


async def test_declare_done_while_red_is_rejected_then_green_completes(
    db_session, billing_factory, sink
) -> None:
    user = await UserFactory.create(db_session)
    fake = FakeSandbox()
    fake.dev_ready = True
    fake.queue_commands(ExecResult(stdout="error TS2322: nope", stderr="", exit=2))  # run 1 red
    fake.queue_commands(ExecResult(stdout="", stderr="", exit=0))  # run 2 green
    turns = [
        tool_turn("declare_done", {"summary": "premature"}),
        text_turn(),
        tool_turn("declare_done", {"summary": "fixed"}),
        text_turn(),
    ]
    model = _seed_capturing_model(turns, [])
    orchestrator, _ = make_orchestrator(model, billing_factory)

    result = await orchestrator.run_build(uuid.uuid4(), user.id, fake, sink)

    assert result.status == BuildSessionStatus.ENDED  # completed
    assert any(e.type == "error" and e.source == ErrorSource.TSC for e in sink.events)  # rejected
    assert any(e.type == "preview_ready" for e in sink.events)
    assert result.reason == "completed"  # on the verdict; BRAIN emits no terminal (R7)


async def test_server_arm_seeds_a_repair_run(db_session, billing_factory, sink) -> None:
    user = await UserFactory.create(db_session)
    fake = FakeSandbox()
    fake.dev_ready = True
    fake.push_dev_logs("⨯ unhandledRejection Error: boom in RecordsPage")  # a crash after run 1
    seeds: list[str] = []
    turns = [
        tool_turn("declare_done", {"summary": "x"}),
        text_turn(),
        tool_turn("declare_done", {"summary": "y"}),
        text_turn(),
    ]
    model = _seed_capturing_model(turns, seeds)
    orchestrator, _ = make_orchestrator(model, billing_factory)

    result = await orchestrator.run_build(uuid.uuid4(), user.id, fake, sink)

    # The server crash became an error envelope AND the next run's prompt.
    assert any(e.type == "error" and e.source == ErrorSource.SERVER for e in sink.events)
    assert any("boom in RecordsPage" in seed for seed in seeds[1:])
    assert result.status == BuildSessionStatus.ENDED  # the crash cleared after the repair run


async def test_dev_never_ready_reseeds_a_diagnostic_not_the_done_nudge(
    db_session, billing_factory, sink
) -> None:
    # tsc clean, no crash marker, but the dev server never becomes ready: the loop must NOT
    # misread this as "green but forgot declare_done" (CONTINUE_PROMPT). It synthesizes an accurate
    # server diagnostic so the repair prompt is right AND a budget-exhausted escalation carries a
    # last_error (never a diagnostic-free failure).
    user = await UserFactory.create(db_session)
    fake = FakeSandbox()
    fake.dev_ready = False  # never becomes ready; default tsc exit 0; no crash logs
    seeds: list[str] = []
    turns = [
        t for _ in range(4) for t in (tool_turn("declare_done", {"summary": "x"}), text_turn())
    ]
    model = _seed_capturing_model(turns, seeds)
    orchestrator, _ = make_orchestrator(model, billing_factory)

    result = await orchestrator.run_build(uuid.uuid4(), user.id, fake, sink)

    assert result.status == BuildSessionStatus.FAILED
    # NOT the misdiagnosing done-nudge …
    assert not any("ended your turn without calling" in seed for seed in seeds)
    # … the accurate dev-not-ready diagnostic re-seeded a later run.
    assert any("did not report ready" in seed for seed in seeds[1:])
    escalations = [e for e in sink.events if e.type == "escalation"]
    assert len(escalations) == 1 and escalations[0].reason == "self_heal_budget_exhausted"
    assert escalations[0].last_error is not None  # never diagnostic-free
    assert result.error is not None


# =============================================================================
# Transient sandbox errors during verify — bounded retry, never a hard FAILED
# =============================================================================


async def test_verify_transient_blip_is_retried_not_escalated(
    db_session, billing_factory, sink, monkeypatch
) -> None:
    # One supervisor blip on the tsc hop must NOT escalate the whole build to internal_error —
    # the bounded retry absorbs it and the build completes normally.
    monkeypatch.setattr(selfheal, "VERIFY_RETRY_BACKOFF_S", 0.0)
    user = await UserFactory.create(db_session)
    fake = FakeSandbox()
    fake.dev_ready = True
    fake.queue_exec_errors(SandboxError("transient supervisor blip"))
    model = scripted_model([tool_turn("declare_done", {"summary": "x"}), text_turn()])
    orchestrator, _ = make_orchestrator(model, billing_factory)

    result = await orchestrator.run_build(uuid.uuid4(), user.id, fake, sink)

    assert result.status == BuildSessionStatus.ENDED
    assert result.reason == "completed"  # on the verdict; BRAIN emits no terminal (R7)
    assert not any(e.type == "escalation" for e in sink.events)
    assert len(fake.command_calls) == 2  # the blipped tsc attempt + the successful retry


async def test_verify_persistent_transient_errors_escalate_after_retries(
    db_session, billing_factory, sink, monkeypatch
) -> None:
    monkeypatch.setattr(selfheal, "VERIFY_RETRY_BACKOFF_S", 0.0)
    user = await UserFactory.create(db_session)
    fake = FakeSandbox()
    fake.dev_ready = True
    attempts = constants.VERIFY_TRANSIENT_RETRIES + 1
    fake.queue_exec_errors(*(SandboxError("supervisor still down") for _ in range(attempts)))
    model = scripted_model([tool_turn("declare_done", {"summary": "x"}), text_turn()])
    orchestrator, _ = make_orchestrator(model, billing_factory)

    result = await orchestrator.run_build(uuid.uuid4(), user.id, fake, sink)

    assert result.status == BuildSessionStatus.FAILED
    escalations = [e for e in sink.events if e.type == "escalation"]
    assert len(escalations) == 1 and escalations[0].reason == "internal_error"
    assert len(fake.command_calls) == attempts  # exhausted the budget, then escalated as today


async def test_verify_sandbox_gone_escalates_immediately_without_retry(
    db_session, billing_factory, sink
) -> None:
    # Gone is terminal for the handle (restore-needed): no retry may be burned on it, and it must
    # keep its dedicated sandbox_gone escalation — never be blurred into a transient retry.
    user = await UserFactory.create(db_session)
    fake = FakeSandbox()
    fake.dev_ready = True
    fake.queue_exec_errors(SandboxGoneError("container torn down mid-verify"))
    model = scripted_model([tool_turn("declare_done", {"summary": "x"}), text_turn()])
    orchestrator, _ = make_orchestrator(model, billing_factory)

    result = await orchestrator.run_build(uuid.uuid4(), user.id, fake, sink)

    assert result.status == BuildSessionStatus.FAILED
    escalations = [e for e in sink.events if e.type == "escalation"]
    assert len(escalations) == 1 and escalations[0].reason == "sandbox_gone"
    assert len(fake.command_calls) == 1  # no retry attempt followed the gone signal
