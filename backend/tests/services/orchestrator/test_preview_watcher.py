"""The F8/U5 early readiness watcher — frames the preview the instant the dev server serves,
decoupled from the between-runs verify cadence, with a distinct dev-process-crash → reconnecting
state and a managed teardown that completes before the terminal funnel reads `last_seq`.

The three concurrency-contract properties this pins:
  1. DIRECT emit (not a loop-boundary signal): the watcher frames WHILE the first model request is
     still in flight — `_run_one`'s first act is a (here, blocked) model request.
  2. SYNCHRONOUS 3-site dedup: a warm/resumed sandbox's warm-resume emit + the watcher's first poll
     + the between-steps verify never produce more than ONE `preview_ready`.
  3. Managed TEARDOWN before the funnel: on normal completion, on STOP (CancelledError), and on a
     build error the watcher is cancelled + awaited (no leaked task, gap-free stream, and
     `last_seq` captured after the watcher is gone).
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Callable

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage

from src.api.v1.build_sessions.schemas import BuildSessionStatus, ProgressEnvelope
from src.services.orchestrator.deps import BuildDeps
from src.services.orchestrator.progress import ProgressEmitter
from src.services.sandbox import SandboxGoneError, SandboxHandle
from tests.factories import UserFactory
from tests.services.orchestrator.conftest import CollectingSink, make_orchestrator
from tests.services.orchestrator.fake_sandbox import FakeSandbox
from tests.services.orchestrator.model_harness import scripted_model, text_turn, tool_turn


async def _until(pred: Callable[[], bool], *, tries: int = 2000) -> None:
    """Yield to the concurrent watcher task until `pred` holds (poll_s is 0 in tests, so the
    watcher spins on `await asyncio.sleep(0)`); bounded so a broken watcher fails, not hangs."""
    for _ in range(tries):
        if pred():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was never met — the watcher did not make progress")


def _preview_ready_count(events: list[ProgressEnvelope]) -> int:
    return len([e for e in events if e.type == "preview_ready"])


def _assert_gap_free(events: list[ProgressEnvelope]) -> None:
    seqs = [e.seq for e in events]
    assert seqs == list(range(1, len(seqs) + 1)), f"seq not gap-free: {seqs}"


def _leaked_tasks(before: set[asyncio.Task[object]]) -> list[asyncio.Task[object]]:
    """Tasks created during a run that are still running — a leaked watcher would show up here."""
    current = asyncio.current_task()
    return [t for t in asyncio.all_tasks() - before if not t.done() and t is not current]


# ── (1) DIRECT emit — frames while the first model request is still in flight ────────────────────


async def test_watcher_frames_while_first_model_request_is_in_flight(
    db_session, billing_factory, sink
) -> None:
    """The blind window this unit closes: the watcher emits `preview_ready` DURING the first model
    request, not after it. The model's first request BLOCKS until it sees the frame — if framing
    were gated on the between-steps verify (which runs only AFTER the request returns), this wait
    would spin out and raise. That it resolves proves the decoupled, direct emit."""
    user = await UserFactory.create(db_session)
    fake = FakeSandbox()
    # Cold at attach (handle.ready False → no warm-resume emit), but the dev server flips ready on
    # the watcher's very first `/dev/status` poll.
    fake.become_ready_after(0)
    calls = {"n": 0}

    async def respond(messages: list, info: AgentInfo) -> ModelResponse:
        calls["n"] += 1
        if calls["n"] == 1:
            await _until(lambda: _preview_ready_count(sink.events) >= 1, tries=5000)
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="declare_done", args={"summary": "x"}, tool_call_id="c1"
                    )
                ],
                usage=RequestUsage(input_tokens=1, output_tokens=1),
            )
        return ModelResponse(
            parts=[TextPart(content="done")], usage=RequestUsage(input_tokens=1, output_tokens=1)
        )

    orchestrator, _ = make_orchestrator(FunctionModel(respond), billing_factory)

    result = await orchestrator.run_build(uuid.uuid4(), user.id, fake, sink)

    _assert_gap_free(sink.events)
    assert _preview_ready_count(sink.events) == 1  # the watcher framed once; verify never re-emits
    assert result.reason == "completed"
    assert result.status == BuildSessionStatus.ENDED
    assert result.last_seq == sink.events[-1].seq


# ── (2) 3-site dedup — warm-resume + watcher + verify never double-fire ──────────────────────────


async def test_warm_resume_and_watcher_emit_exactly_one_preview_ready(
    db_session, billing_factory, sink
) -> None:
    """A warm/resumed sandbox emits `preview_ready` immediately (`handle.ready`) AND the watcher's
    first poll sees ready AND the between-steps verify sees ready — the shared guard (seeded from
    `handle.ready`) admits exactly ONE frame across all three sites."""
    user = await UserFactory.create(db_session)
    fake = FakeSandbox()
    fake.dev_ready = True  # attach returns handle.ready=True → warm-resume claims the frame
    model = scripted_model([tool_turn("declare_done", {"summary": "x"}), text_turn()])
    orchestrator, _ = make_orchestrator(model, billing_factory)

    result = await orchestrator.run_build(uuid.uuid4(), user.id, fake, sink)

    _assert_gap_free(sink.events)
    assert _preview_ready_count(sink.events) == 1  # NOT two (warm-resume + watcher) or three
    assert result.reason == "completed"


# ── (3) Managed teardown — normal completion / STOP / build error ────────────────────────────────


async def test_watcher_torn_down_on_normal_completion(db_session, billing_factory, sink) -> None:
    user = await UserFactory.create(db_session)
    fake = FakeSandbox()
    fake.dev_ready = True
    model = scripted_model([tool_turn("declare_done", {"summary": "x"}), text_turn()])
    orchestrator, _ = make_orchestrator(model, billing_factory)
    before = set(asyncio.all_tasks())

    result = await orchestrator.run_build(uuid.uuid4(), user.id, fake, sink)

    assert not _leaked_tasks(before)  # the watcher was cancelled + awaited, no zombie poller
    _assert_gap_free(sink.events)
    # Teardown COMPLETED before the funnel read last_seq — no watcher emit landed after it.
    assert result.last_seq == sink.events[-1].seq
    assert result.reason == "completed"


async def test_watcher_torn_down_on_stop_cancelled(db_session, billing_factory, sink) -> None:
    """STOP: SESSION-API cancels the task; BRAIN unwinds on CancelledError. The watcher must be
    torn down (cancelled + awaited) on that path too — no leaked task polling a dead sandbox."""
    user = await UserFactory.create(db_session)
    fake = FakeSandbox()
    fake.dev_ready = True  # frames immediately, so the watcher is definitely live when we cancel
    gate = asyncio.Event()

    async def respond(messages: list, info: AgentInfo) -> ModelResponse:
        await gate.wait()  # block the build indefinitely until the STOP cancels us
        return ModelResponse(
            parts=[TextPart(content="done")], usage=RequestUsage(input_tokens=1, output_tokens=1)
        )

    orchestrator, _ = make_orchestrator(FunctionModel(respond), billing_factory)
    before = set(asyncio.all_tasks())
    task = asyncio.create_task(orchestrator.run_build(uuid.uuid4(), user.id, fake, sink))
    await _until(
        lambda: _preview_ready_count(sink.events) >= 1
    )  # let it frame + start the watcher

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not _leaked_tasks(before)  # the watcher did not survive the cancel


async def test_watcher_torn_down_on_build_error(db_session, billing_factory, sink) -> None:
    """A build error (a `SandboxGoneError` mid-write funnels to the `sandbox_gone` escalation): the
    watcher is torn down before the funnel too, so the stream stays gap-free and no task leaks."""
    user = await UserFactory.create(db_session)
    fake = FakeSandbox()
    fake.dev_ready = True
    fake.files_error = SandboxGoneError("the sandbox vanished mid-write")
    model = scripted_model(
        [tool_turn("write_file", {"path": "app/page.tsx", "file_text": "x\n"}), text_turn()]
    )
    orchestrator, _ = make_orchestrator(model, billing_factory)
    before = set(asyncio.all_tasks())

    result = await orchestrator.run_build(uuid.uuid4(), user.id, fake, sink)

    assert not _leaked_tasks(before)
    _assert_gap_free(sink.events)
    assert result.status == BuildSessionStatus.FAILED
    assert result.last_seq == sink.events[-1].seq  # last_seq captured after the watcher was gone


# ── (4) Dev-process crash → reconnecting → re-frame (watcher unit, driven directly) ──────────────


async def test_dev_process_crash_emits_reconnecting_then_reframes(
    db_session, billing_factory
) -> None:
    """Drive the watcher directly over a full crash cycle: first serve frames; the dev PROCESS
    exits (`running` false) → ONE `preview_reconnecting`; it restarts (`ready` again) → a re-frame.
    The frontend cannot originate this — `/dev/status` is backend-only — so the watcher owns it."""
    fake = FakeSandbox()
    fake.dev_running = True
    fake.dev_ready = True
    orchestrator, _ = make_orchestrator(scripted_model([text_turn()]), billing_factory)
    sink = CollectingSink()
    emitter = ProgressEmitter(sink)
    deps = BuildDeps(
        sandbox_client=fake,
        handle=fake.handle(),  # ready=False → cold; the watcher claims the first frame
        emitter=emitter,
        user_id=uuid.uuid4(),
        app_id=uuid.uuid4(),
    )
    task = asyncio.create_task(orchestrator._watch_preview(emitter, deps))
    try:
        # 1) first serve → the initial frame
        await _until(lambda: _preview_ready_count(sink.events) == 1)
        # 2) the dev PROCESS exits (port closes) → a distinct reconnecting signal, once
        fake.dev_running = False
        fake.dev_ready = False
        await _until(lambda: any(e.type == "preview_reconnecting" for e in sink.events))
        # 3) the dev server restarts → re-frame
        fake.dev_running = True
        fake.dev_ready = True
        await _until(lambda: _preview_ready_count(sink.events) == 2)
        # …and while it stays down, reconnecting is emitted only ONCE (edge, not every poll)
        await asyncio.sleep(0)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    types = [e.type for e in sink.events]
    assert types.count("preview_ready") == 2  # initial frame + re-frame
    assert types.count("preview_reconnecting") == 1  # one edge, not one-per-poll
    _assert_gap_free(sink.events)


async def test_crash_reconnecting_is_not_re_emitted_while_the_server_stays_down(
    db_session, billing_factory
) -> None:
    """The reconnecting signal is an EDGE: while the dev process stays dead across many polls, it
    fires exactly once (never one `preview_reconnecting` per poll)."""
    fake = FakeSandbox()
    fake.dev_running = True
    fake.dev_ready = True
    orchestrator, _ = make_orchestrator(scripted_model([text_turn()]), billing_factory)
    sink = CollectingSink()
    emitter = ProgressEmitter(sink)
    deps = BuildDeps(
        sandbox_client=fake,
        handle=fake.handle(),
        emitter=emitter,
        user_id=uuid.uuid4(),
        app_id=uuid.uuid4(),
    )
    task = asyncio.create_task(orchestrator._watch_preview(emitter, deps))
    try:
        await _until(lambda: _preview_ready_count(sink.events) == 1)
        fake.dev_running = False
        fake.dev_ready = False
        await _until(lambda: any(e.type == "preview_reconnecting" for e in sink.events))
        for _ in range(50):  # many more polls while it stays down
            await asyncio.sleep(0)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert [e.type for e in sink.events].count("preview_reconnecting") == 1


async def test_watcher_ignores_a_transient_dev_status_error(
    db_session, billing_factory, monkeypatch
) -> None:
    """A transient supervisor blip on `/dev/status` must not crash the managed watcher (it would
    surface as an unretrieved-exception at teardown) — it keeps polling and still frames."""
    from src.services.sandbox import DevStatus, SandboxError

    fake = FakeSandbox()
    fake.dev_running = True
    fake.dev_ready = True
    orchestrator, _ = make_orchestrator(scripted_model([text_turn()]), billing_factory)
    sink = CollectingSink()
    emitter = ProgressEmitter(sink)
    deps = BuildDeps(
        sandbox_client=fake,
        handle=fake.handle(),
        emitter=emitter,
        user_id=uuid.uuid4(),
        app_id=uuid.uuid4(),
    )
    # First dev_status poll raises, the rest succeed — the watcher must survive and still frame.
    calls = {"n": 0}
    real_dev_status = fake.dev_status

    async def flaky_dev_status(handle: SandboxHandle) -> DevStatus:
        calls["n"] += 1
        if calls["n"] == 1:
            raise SandboxError("supervisor blip")
        return await real_dev_status(handle)

    monkeypatch.setattr(fake, "dev_status", flaky_dev_status)
    task = asyncio.create_task(orchestrator._watch_preview(emitter, deps))
    try:
        await _until(lambda: _preview_ready_count(sink.events) == 1)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert _preview_ready_count(sink.events) == 1  # the blip did not stop the frame
