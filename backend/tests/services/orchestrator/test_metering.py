"""Per-model-step metering across the multi-run loop (U6, KD-1/KD-3, C7 §6).

enforce-before / record-after, one billing session per step, BRAIN owns the commit — charged to the
session owner (ADR-0025), written into the rolled-back test transaction.
"""

from __future__ import annotations

import uuid

from pydantic_ai.usage import RequestUsage
from sqlalchemy import select

from src.api.v1.build_sessions.schemas import BuildSessionStatus
from src.db.models.token_usage import TokenUsage
from src.db.models.user_limit import UserLimit
from src.services.sandbox import FileOp, FileResult, SandboxGoneError, SandboxHandle
from tests.factories import UserFactory
from tests.services.orchestrator.conftest import make_orchestrator
from tests.services.orchestrator.fake_sandbox import FakeSandbox
from tests.services.orchestrator.model_harness import scripted_model, text_turn, tool_turn


async def _usage_row(db_session, user_id: uuid.UUID) -> TokenUsage | None:
    return await db_session.scalar(select(TokenUsage).where(TokenUsage.user_id == user_id))


def _ready_fake() -> FakeSandbox:
    fake = FakeSandbox()
    fake.dev_ready = True  # tsc default exit 0 + dev ready + clean logs → a green gate
    return fake


async def test_two_model_steps_fold_two_per_step_usages(db_session, billing_factory, sink) -> None:
    user = await UserFactory.create(db_session)
    fake = _ready_fake()
    model = scripted_model(
        [
            tool_turn(
                "declare_done",
                {"summary": "built it"},
                usage=RequestUsage(
                    input_tokens=11, output_tokens=22, cache_read_tokens=3, cache_write_tokens=4
                ),
            ),
            text_turn(
                "done",
                usage=RequestUsage(
                    input_tokens=100, output_tokens=200, cache_read_tokens=5, cache_write_tokens=6
                ),
            ),
        ]
    )
    orchestrator, _ = make_orchestrator(model, billing_factory)

    result = await orchestrator.run_build(uuid.uuid4(), user.id, fake, sink)

    assert result.status == BuildSessionStatus.ENDED  # completed cleanly
    row = await _usage_row(db_session, user.id)
    assert row is not None
    # Both per-step RequestUsages folded verbatim (no subtraction) — charged to the owner.
    assert row.input_tokens == 111
    assert row.output_tokens == 222
    assert row.cache_read_tokens == 8
    assert row.cache_write_tokens == 10


async def test_enforce_raises_on_second_step_so_that_request_never_fires(
    db_session, billing_factory, sink
) -> None:
    user = await UserFactory.create(db_session)
    db_session.add(UserLimit(user_id=user.id, daily_token_limit=100))
    await db_session.flush()
    fake = _ready_fake()
    model = scripted_model(
        [
            # Step 1 spends exactly the cap; step 2's pre-request enforce then trips.
            tool_turn(
                "declare_done",
                {"summary": "x"},
                usage=RequestUsage(input_tokens=100, output_tokens=0),
            ),
            text_turn("should never run", usage=RequestUsage(input_tokens=999, output_tokens=999)),
        ]
    )
    orchestrator, _ = make_orchestrator(model, billing_factory)

    result = await orchestrator.run_build(uuid.uuid4(), user.id, fake, sink)

    # Graceful quota end, not a failure.
    assert result.status == BuildSessionStatus.ENDED
    assert any(e.type == "quota_exceeded" for e in sink.events)
    assert sink.events[-1].type == "ended" and sink.events[-1].reason == "quota_exceeded"
    # The second step's request never fired: only step 1's 100 input tokens were ever recorded
    # (the 999/999 usage would appear if the blocked request had run).
    row = await _usage_row(db_session, user.id)
    assert row is not None
    assert row.input_tokens == 100
    assert row.output_tokens == 0


async def test_build_result_app_id_comes_from_the_run_context(
    db_session, billing_factory, sink
) -> None:
    user = await UserFactory.create(db_session)
    fake = _ready_fake()
    expected_app_id = uuid.uuid4()
    model = scripted_model([tool_turn("declare_done", {"summary": "x"}), text_turn()])
    orchestrator, app_id = make_orchestrator(model, billing_factory, app_id=expected_app_id)

    result = await orchestrator.run_build(uuid.uuid4(), user.id, fake, sink)

    assert app_id == expected_app_id
    assert result.app_id == expected_app_id  # from the KD-13 provider, not derived from app_name


async def test_attach_gone_escalates_and_never_raises(db_session, billing_factory, sink) -> None:
    user = await UserFactory.create(db_session)
    fake = FakeSandbox()
    fake.attach_error = SandboxGoneError("container gone")
    model = scripted_model([text_turn()])
    orchestrator, app_id = make_orchestrator(model, billing_factory)

    result = await orchestrator.run_build(uuid.uuid4(), user.id, fake, sink)  # must NOT raise

    assert result.status == BuildSessionStatus.FAILED
    assert result.snapshot_committed is False
    assert result.app_id == app_id  # known from the run-context, even though attach failed
    assert any(e.type == "escalation" and e.reason == "sandbox_gone" for e in sink.events)
    assert sink.events[-1].type == "ended" and sink.events[-1].status == BuildSessionStatus.FAILED
    assert fake.dev_start_calls == 0  # never got past attach


async def test_dev_start_once_and_resumed_ready_emits_initial_preview(
    db_session, billing_factory, sink
) -> None:
    user = await UserFactory.create(db_session)
    fake = FakeSandbox()
    fake.dev_ready = True  # already ready at attach → resumed session
    model = scripted_model([tool_turn("declare_done", {"summary": "x"}), text_turn()])
    orchestrator, _ = make_orchestrator(model, billing_factory)

    await orchestrator.run_build(uuid.uuid4(), user.id, fake, sink)

    assert fake.dev_start_calls == 1  # started exactly once (idempotent, never restarted)
    assert sink.events[0].type == "preview_ready"  # the resumed-already-ready initial load trigger


async def test_tool_runtime_error_escalates_and_never_escapes(
    db_session, billing_factory, sink
) -> None:
    user = await UserFactory.create(db_session)

    class ExplodingSandbox(FakeSandbox):
        async def files(self, handle: SandboxHandle, op: FileOp) -> FileResult:
            raise RuntimeError("supervisor blew up mid-write")

    fake = ExplodingSandbox()
    model = scripted_model(
        [tool_turn("write_file", {"path": "app/page.tsx", "file_text": "x"}), text_turn()]
    )
    orchestrator, _ = make_orchestrator(model, billing_factory)

    result = await orchestrator.run_build(uuid.uuid4(), user.id, fake, sink)  # must NOT raise

    assert result.status == BuildSessionStatus.FAILED
    # One terminal ended, always last and highest seq (KD-12).
    ended = [e for e in sink.events if e.type == "ended"]
    assert len(ended) == 1
    assert ended[0].seq == max(e.seq for e in sink.events)
    assert any(e.type == "escalation" for e in sink.events)
