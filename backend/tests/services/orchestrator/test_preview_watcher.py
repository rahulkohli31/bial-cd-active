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
from typing import Any

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage

from src.api.v1.build_sessions.schemas import BuildSessionStatus, ProgressEnvelope
from src.services.orchestrator.deps import BuildDeps, SandboxSession
from src.services.orchestrator.progress import ProgressEmitter
from src.services.sandbox import DevStatus, SandboxGoneError, SandboxHandle
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


async def test_teardown_completes_before_the_funnel_reads_last_seq(
    db_session, billing_factory, sink, monkeypatch
) -> None:
    """KD-12 ORDERING itself. The sibling teardown tests prove teardown HAPPENS + gap-freeness, but
    a `last_seq == last event` assertion holds whether teardown ran before OR after the funnel (in
    the happy path the watcher has nothing new to emit at completion). This pins the SEQUENCE by
    recording the call order of `_stop_watcher` and `_funnel`: a reorder (funnel first — which is
    what would let a late watcher emit collide with or gap the terminal `ended` seq) → red."""
    import src.services.orchestrator.harness as harness_mod

    user = await UserFactory.create(db_session)
    fake = FakeSandbox()
    fake.dev_ready = True
    model = scripted_model([tool_turn("declare_done", {"summary": "x"}), text_turn()])
    orchestrator, _ = make_orchestrator(model, billing_factory)

    order: list[str] = []
    real_stop = harness_mod._stop_watcher
    real_funnel = orchestrator._funnel

    async def spy_stop(task: asyncio.Task[None] | None) -> None:
        order.append("stop_watcher")
        await real_stop(task)

    async def spy_funnel(*args: Any, **kwargs: Any) -> Any:
        order.append("funnel")
        return await real_funnel(*args, **kwargs)

    monkeypatch.setattr(harness_mod, "_stop_watcher", spy_stop)
    monkeypatch.setattr(orchestrator, "_funnel", spy_funnel)

    result = await orchestrator.run_build(uuid.uuid4(), user.id, fake, sink)

    assert order == ["stop_watcher", "funnel"]  # teardown ran to completion BEFORE the funnel
    assert result.reason == "completed"
    assert result.last_seq == sink.events[-1].seq


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
        sandbox=SandboxSession(
            sandbox_client=fake,
            handle=fake.handle(),  # ready=False → cold; the watcher claims the first frame
            app_id=uuid.uuid4(),
            emitter=emitter,
        ),
        emitter=emitter,
        user_id=uuid.uuid4(),
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
        sandbox=SandboxSession(
            sandbox_client=fake,
            handle=fake.handle(),
            app_id=uuid.uuid4(),
            emitter=emitter,
        ),
        emitter=emitter,
        user_id=uuid.uuid4(),
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


async def test_a_slow_render_does_not_read_as_a_dev_process_crash(
    db_session, billing_factory
) -> None:
    """★ THE FLAP, pinned on the harness watcher so the two implementations cannot drift. They
    emit the same signal to the same pane, so a debounce on the turns engine alone would make the
    crash edge depend on which code path built the app.

    `/dev/status` answers from a bounded wait on an in-flight probe (2s) while a real cold root
    render against a per-project Postgres takes longer, and a negative is never cached — so a
    healthy app reads not-ready for as long as it renders. Paired with `running: false` (the
    NORMAL state for a dev server the agent started itself), one such poll used to be a crash
    edge, and the citizen's iframe was re-mounted under them over an app that was merely slow.

    TWO negatives, as a literal: a count derived from `CRASH_EDGE_CONSECUTIVE_POLLS` would move
    with the constant and could never go red. Mutation check: set it to 1 or 2."""

    class SlowRender(FakeSandbox):
        """Serves the first poll (so the frame lands), then `running=False, ready=False` for
        exactly two polls — the render window — and answers again after that."""

        def __init__(self) -> None:
            super().__init__()
            self.dev_running = True
            self.dev_ready = True
            self.polls = 0

        async def dev_status(self, handle: SandboxHandle) -> DevStatus:
            self.polls += 1
            stalled = 2 <= self.polls <= 3
            return DevStatus(running=not stalled, ready=not stalled, port=3000)

    fake = SlowRender()
    orchestrator, _ = make_orchestrator(scripted_model([text_turn()]), billing_factory)
    sink = CollectingSink()
    emitter = ProgressEmitter(sink)
    deps = BuildDeps(
        sandbox=SandboxSession(
            sandbox_client=fake,
            handle=fake.handle(),
            app_id=uuid.uuid4(),
            emitter=emitter,
        ),
        emitter=emitter,
        user_id=uuid.uuid4(),
    )
    task = asyncio.create_task(orchestrator._watch_preview(emitter, deps))
    try:
        await _until(lambda: fake.polls > 6)  # well past the stall window
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert _preview_ready_count(sink.events) == 1, "guard the premise: it framed, then stalled"
    assert [e.type for e in sink.events].count("preview_reconnecting") == 0


async def test_a_cancel_inside_the_warm_request_still_frames_the_preview(
    db_session, billing_factory
) -> None:
    """★ Same defect as the turns engine's, on the legacy harness path. U3 put a cancellable,
    up-to-8s warm request between the one-shot `claim_preview_frame()` and the emit, and
    `_stop_watcher` cancels this watcher at every terminal — so a cancel in that window leaves
    the frame claimed forever and never sent, and no later poll re-claims it.

    The shared `FakeSandbox`'s warm returns synchronously, which makes the window unreachable;
    this one actually suspends. Mutation check: unwrap the `try/finally` in `_frame_the_preview`
    and no `preview_ready` reaches the sink."""

    class WarmThatHangs(FakeSandbox):
        def __init__(self) -> None:
            super().__init__()
            self.dev_running = True
            self.dev_ready = True
            self.entered = asyncio.Event()

        async def someone_has_to_go_first(self, handle: SandboxHandle) -> int | None:
            self.warm_calls += 1
            self.entered.set()
            await asyncio.sleep(3600)  # the app that never answers
            return 200

    fake = WarmThatHangs()
    orchestrator, _ = make_orchestrator(scripted_model([text_turn()]), billing_factory)
    sink = CollectingSink()
    emitter = ProgressEmitter(sink)
    deps = BuildDeps(
        sandbox=SandboxSession(
            sandbox_client=fake,
            handle=fake.handle(),  # ready=False → the watcher claims the first frame
            app_id=uuid.uuid4(),
            emitter=emitter,
        ),
        emitter=emitter,
        user_id=uuid.uuid4(),
    )
    task = asyncio.create_task(orchestrator._watch_preview(emitter, deps))
    await asyncio.wait_for(fake.entered.wait(), timeout=5)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert deps.preview_framed, "guard the premise: the one-shot claim was already spent"
    assert _preview_ready_count(sink.events) == 1
    _assert_gap_free(sink.events)


async def test_watcher_ignores_a_transient_dev_status_error(
    db_session, billing_factory, monkeypatch
) -> None:
    """A transient supervisor blip on `/dev/status` must not crash the managed watcher (it would
    surface as an unretrieved-exception at teardown) — it keeps polling and still frames."""
    from src.services.sandbox import SandboxError

    fake = FakeSandbox()
    fake.dev_running = True
    fake.dev_ready = True
    orchestrator, _ = make_orchestrator(scripted_model([text_turn()]), billing_factory)
    sink = CollectingSink()
    emitter = ProgressEmitter(sink)
    deps = BuildDeps(
        sandbox=SandboxSession(
            sandbox_client=fake,
            handle=fake.handle(),
            app_id=uuid.uuid4(),
            emitter=emitter,
        ),
        emitter=emitter,
        user_id=uuid.uuid4(),
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
