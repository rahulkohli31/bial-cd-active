"""BRAIN's end paths — it SIGNALS, SESSION-API terminates (U8, KD-11/KD-12, R7).

BRAIN emits NO terminal `ended` on any path: completion travels home as data on `BuildResult`
(`status` / `reason` / `preview_url` / `last_seq`), and SESSION-API renders the single terminal
frame in `_do_finalize` AFTER its C4 snapshot — the only point where `snapshot_committed` can be
told truthfully. BRAIN also never runs git, tears down, or touches a lock. On stop/idle
(cancellation) it unwinds without emitting and returns no value.

The manager side of this handoff — that the frame really is emitted post-snapshot, exactly once —
is covered in `tests/services/build_sessions/test_manager.py`.
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

    # The verdict IS the completion signal — there is no terminal envelope to agree with.
    assert result.status == BuildSessionStatus.ENDED
    assert result.reason == "completed"
    assert result.app_id == app_id
    assert not any(e.type == "ended" for e in sink.events)
    assert any(e.type == "preview_ready" for e in sink.events)
    # last_seq hands the seq baton to SESSION-API, which continues at last_seq + 1.
    assert result.last_seq == max(e.seq for e in sink.events)
    # BRAIN never ran git and never tore down (KD-11).
    assert not _ran_git(fake)
    assert fake.teardown_calls == 0


async def test_quota_emits_its_envelope_but_still_no_terminal(
    db_session, billing_factory, sink
) -> None:
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

    # `quota_exceeded` is informational and stays BRAIN's; only the terminal moved (R7).
    assert any(e.type == "quota_exceeded" for e in sink.events)
    assert sink.events[-1].type == "quota_exceeded"  # last thing BRAIN says, and not terminal
    assert not any(e.type == "ended" for e in sink.events)
    assert result.status == BuildSessionStatus.ENDED  # graceful, NOT failed
    assert result.reason == "quota_exceeded"
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
    assert result.reason == "escalated"
    assert result.error is None  # infra failure legitimately carries no BuildError (open-Q I)
    # `escalation` still egresses (it is not a terminal boundary); `ended` does not.
    assert sink.events[-1].type == "escalation"
    assert not any(e.type == "ended" for e in sink.events)
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
    assert result.reason == "escalated"
    escalations = [e for e in sink.events if e.type == "escalation"]
    assert len(escalations) == 1
    assert escalations[0].reason == "sandbox_gone"  # NOT "internal_error"
    assert not any(e.type == "ended" for e in sink.events)
    assert fake.teardown_calls == 0


@pytest.mark.parametrize(
    ("kind", "expected_status", "expected_reason"),
    [
        ("completed", BuildSessionStatus.ENDED, "completed"),
        ("quota", BuildSessionStatus.ENDED, "quota_exceeded"),
        ("escalated", BuildSessionStatus.FAILED, "escalated"),
    ],
)
async def test_no_brain_end_path_emits_a_terminal_frame(
    db_session, billing_factory, sink, kind, expected_status, expected_reason
) -> None:
    """The BRAIN half of "no code path can emit two `ended` frames or a false
    `snapshot_committed`" — swept across every arm of the funnel at once. Each arm must carry its
    outcome home on the verdict and leave the terminal frame to SESSION-API. An `ended` here would
    be BOTH a second terminal (the manager emits its own) AND a false `snapshot_committed=false`
    (BRAIN returns before the snapshot runs)."""
    user = await UserFactory.create(db_session)
    fake = FakeSandbox()
    fake.dev_ready = True
    model = scripted_model([tool_turn("declare_done", {"summary": "x"}), text_turn()])

    if kind == "quota":
        from src.db.models.user_limit import UserLimit

        db_session.add(UserLimit(user_id=user.id, daily_token_limit=50))
        await db_session.flush()
        await record_usage(db_session, user.id, input_tokens=50, output_tokens=0)
    elif kind == "escalated":
        fake.attach_error = SandboxGoneError("gone")

    orchestrator, _ = make_orchestrator(model, billing_factory)
    result = await orchestrator.run_build(uuid.uuid4(), user.id, fake, sink)

    assert not any(e.type == "ended" for e in sink.events)
    assert result.status == expected_status
    assert result.reason == expected_reason
    # The verdict's own snapshot flag is BRAIN's at-return-time view and is ALWAYS False —
    # never the answer to "was the work saved?" (only the manager's frame carries that).
    assert result.snapshot_committed is False
    assert not _ran_git(fake)
    assert fake.teardown_calls == 0


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
