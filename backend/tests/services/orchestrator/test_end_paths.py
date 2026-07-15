"""Terminal/end paths — BRAIN signals, SESSION-API snapshots (U8, KD-11/KD-12).

BRAIN emits exactly one terminal `ended` with snapshot_committed=False and NEVER runs git, tears
down, or touches a lock. On stop/idle (cancellation) it unwinds WITHOUT emitting and returns no
value — SESSION-API owns that terminal.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from src.api.v1.build_sessions.schemas import BuildSessionStatus
from src.services.sandbox import SandboxGoneError, SandboxHandle
from src.services.usage.gate import record_usage
from tests.factories import UserFactory
from tests.services.orchestrator.conftest import make_orchestrator
from tests.services.orchestrator.fake_sandbox import FakeSandbox
from tests.services.orchestrator.model_harness import scripted_model, text_turn, tool_turn


def _ran_git(fake: FakeSandbox) -> bool:
    return any("git" in part for cmd in fake.command_calls for part in cmd)


async def test_completed_signals_without_git_or_teardown(
    db_session, billing_factory, sink
) -> None:
    user = await UserFactory.create(db_session)
    fake = FakeSandbox()
    fake.dev_ready = True
    model = scripted_model([tool_turn("declare_done", {"summary": "built"}), text_turn()])
    orchestrator, app_id = make_orchestrator(model, billing_factory)

    result = await orchestrator.run_build(uuid.uuid4(), user.id, fake, sink)

    ended = sink.events[-1]
    assert ended.type == "ended"
    assert ended.status == BuildSessionStatus.ENDED and ended.reason == "completed"
    assert ended.snapshot_committed is False  # BRAIN signals; SESSION-API snapshots (KD-11)
    assert any(e.type == "preview_ready" for e in sink.events)
    # BuildResult agrees with the ended envelope on the shared fields.
    assert result.status == BuildSessionStatus.ENDED
    assert result.snapshot_committed is False
    assert result.app_id == app_id
    assert result.last_seq == ended.seq
    # BRAIN never ran git and never tore down.
    assert not _ran_git(fake)
    assert fake.teardown_calls == 0


async def test_quota_is_graceful_not_failed(db_session, billing_factory, sink) -> None:
    user = await UserFactory.create(db_session)
    # Pre-spend the whole cap so the very first pre-request enforce trips.
    from src.db.models.user_limit import UserLimit

    db_session.add(UserLimit(user_id=user.id, daily_token_limit=50))
    await db_session.flush()
    await record_usage(db_session, user.id, input_tokens=50, output_tokens=0)
    fake = FakeSandbox()
    fake.dev_ready = True
    model = scripted_model([tool_turn("declare_done", {"summary": "x"}), text_turn()])
    orchestrator, _ = make_orchestrator(model, billing_factory)

    result = await orchestrator.run_build(uuid.uuid4(), user.id, fake, sink)

    assert result.status == BuildSessionStatus.ENDED  # graceful, NOT failed
    assert result.snapshot_committed is False
    assert any(e.type == "quota_exceeded" for e in sink.events)
    ended = sink.events[-1]
    assert ended.type == "ended" and ended.reason == "quota_exceeded"
    assert not _ran_git(fake)
    assert fake.teardown_calls == 0


async def test_escalated_with_sandbox_gone_carries_no_error_and_no_teardown(
    db_session, billing_factory, sink
) -> None:
    user = await UserFactory.create(db_session)
    fake = FakeSandbox()
    fake.attach_error = SandboxGoneError("gone")
    model = scripted_model([text_turn()])
    orchestrator, _ = make_orchestrator(model, billing_factory)

    result = await orchestrator.run_build(uuid.uuid4(), user.id, fake, sink)

    assert result.status == BuildSessionStatus.FAILED
    assert result.error is None  # infra failure legitimately carries no BuildError (open-Q I)
    assert result.snapshot_committed is False
    ended = sink.events[-1]
    assert ended.type == "ended" and ended.status == BuildSessionStatus.FAILED
    assert ended.reason == "escalated"
    assert fake.attach_calls == 1  # gone is never re-probed (restore, not patience)
    assert not _ran_git(fake)
    assert fake.teardown_calls == 0


async def test_mid_run_sandbox_gone_escalates_as_sandbox_gone_not_internal_error(
    db_session, billing_factory, sink
) -> None:
    # A container torn down mid-edit surfaces as a SandboxGoneError from a tool's files() op. It
    # must propagate to run_build's dedicated sandbox_gone escalation (the RESTORE signal), NOT be
    # swallowed into a ModelRetry and misclassified as internal_error.
    user = await UserFactory.create(db_session)
    fake = FakeSandbox()
    fake.dev_ready = True
    fake.files_error = SandboxGoneError("container vanished mid-edit")
    model = scripted_model(
        [tool_turn("write_file", {"path": "app/page.tsx", "file_text": "x\n"}), text_turn()]
    )
    orchestrator, _ = make_orchestrator(model, billing_factory)

    result = await orchestrator.run_build(uuid.uuid4(), user.id, fake, sink)

    assert result.status == BuildSessionStatus.FAILED
    escalations = [e for e in sink.events if e.type == "escalation"]
    assert len(escalations) == 1
    assert escalations[0].reason == "sandbox_gone"  # NOT "internal_error"
    ended = sink.events[-1]
    assert ended.type == "ended" and ended.reason == "escalated"
    assert fake.teardown_calls == 0


async def test_exactly_one_terminal_ended_always_last(db_session, billing_factory, sink) -> None:
    user = await UserFactory.create(db_session)
    fake = FakeSandbox()
    fake.dev_ready = True
    model = scripted_model([tool_turn("declare_done", {"summary": "x"}), text_turn()])
    orchestrator, _ = make_orchestrator(model, billing_factory)

    await orchestrator.run_build(uuid.uuid4(), user.id, fake, sink)

    ended = [e for e in sink.events if e.type == "ended"]
    assert len(ended) == 1
    assert ended[0] is sink.events[-1]
    assert ended[0].seq == max(e.seq for e in sink.events)


async def test_cancellation_unwinds_without_emitting_or_tearing_down(
    db_session, billing_factory, sink
) -> None:
    user = await UserFactory.create(db_session)
    reached = asyncio.Event()
    forever = asyncio.Event()  # never set

    class BlockingSandbox(FakeSandbox):
        async def dev_start(
            self, handle: SandboxHandle, *, cmd: list[str] | None = None, cwd: str | None = None
        ) -> int:
            reached.set()
            await forever.wait()  # block here so we can cancel mid-run
            return 4242

    fake = BlockingSandbox()
    model = scripted_model([text_turn()])
    orchestrator, _ = make_orchestrator(model, billing_factory)

    task = asyncio.create_task(orchestrator.run_build(uuid.uuid4(), user.id, fake, sink))
    await reached.wait()  # the run is parked inside dev_start
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task  # BRAIN re-raises (returns no value) — SESSION-API owns the terminal

    assert not any(e.type == "ended" for e in sink.events)  # emitted nothing further
    assert fake.teardown_calls == 0
    assert not _ran_git(fake)
