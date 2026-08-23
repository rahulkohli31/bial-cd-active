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
from src.core.integrity_types import BaselineIdentity
from src.services.orchestrator import constants, selfheal
from src.services.orchestrator.selfheal import (
    HealthState,
    Readiness,
    VerifyOutcome,
    are_we_there_yet,
    detect_server_crash,
    verify,
    where_are_we,
)
from src.services.sandbox import (
    ExecResult,
    SandboxError,
    SandboxGoneError,
    SandboxHandle,
    ServedPage,
)
from tests.factories import UserFactory
from tests.services.orchestrator.conftest import make_orchestrator
from tests.services.orchestrator.fake_sandbox import BASELINE_UNTOUCHED_STDOUT, FakeSandbox
from tests.services.orchestrator.model_harness import scripted_model, text_turn, tool_turn

_APP_ID = uuid.UUID("0198f2c0-0000-7000-8000-000000000006")


async def _verify(
    fake: FakeSandbox,
    *,
    log_cursor: int = 0,
    max_polls: int = 3,
    had_prior_building_turns: bool = False,
    indeterminate_retries: int = 0,
    recheck_stale_log_evidence: bool = True,
) -> tuple[VerifyOutcome, int]:
    """`verify` with this file's defaults, and ONE default that is a decision rather than
    convenience: `indeterminate_retries=0`.

    `verify` in production asks again before reporting a defect, which is exactly what makes an
    unanswerable verdict cheap. In a unit test that patience would mean every INDETERMINATE case
    ran the whole pass three times to assert something the first pass already established. Zero
    lets a test observe ONE honest verdict; the retry itself has its own test below.

    `had_prior_building_turns=False` for the same reason: the content check is off unless a test
    says the app has been built, so tests that predate U6 keep asking what they always asked."""
    return await verify(
        fake,
        fake.handle(),
        log_cursor=log_cursor,
        max_polls=max_polls,
        poll_s=0.0,
        app_id=_APP_ID,
        had_prior_building_turns=had_prior_building_turns,
        indeterminate_retries=indeterminate_retries,
        indeterminate_backoff_s=0.0,
        recheck_stale_log_evidence=recheck_stale_log_evidence,
    )


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
    outcome, _ = await _verify(fake, log_cursor=0, max_polls=3)
    assert outcome.green is True
    assert outcome.error is None
    assert outcome.dev_ready is True


async def test_verify_red_on_tsc_failure_builds_a_tsc_error() -> None:
    fake = FakeSandbox()
    fake.dev_ready = True
    fake.queue_commands(ExecResult(stdout="app/x.tsx(1,1): error TS2322: bad", stderr="", exit=2))
    outcome, _ = await _verify(fake, log_cursor=0, max_polls=3)
    assert outcome.green is False
    assert outcome.error is not None and outcome.error.source == ErrorSource.TSC


async def test_verify_red_on_server_crash_builds_a_server_error() -> None:
    fake = FakeSandbox()
    fake.dev_ready = True  # tsc green, but the dev log tail shows a crash
    fake.push_dev_logs("⨯ unhandledRejection Error: cannot read properties of undefined")
    outcome, _ = await _verify(fake, log_cursor=0, max_polls=3)
    assert outcome.green is False
    assert outcome.error is not None and outcome.error.source == ErrorSource.SERVER


async def test_verify_never_runs_next_build() -> None:
    fake = FakeSandbox()
    fake.dev_ready = True
    await _verify(fake, log_cursor=0, max_polls=3)
    # Only `tsc` is ever run between runs — `next build` is a DEPLOY concern (D2/KD-6).
    assert fake.command_calls == [["npx", "tsc", "--noEmit"]]
    assert not any("build" in " ".join(cmd) for cmd in fake.command_calls)


async def test_verify_bounds_the_tsc_run_with_exec_timeout() -> None:
    fake = FakeSandbox()
    fake.dev_ready = True
    await _verify(fake, log_cursor=0, max_polls=3)
    # The tsc run is bounded by EXEC_TIMEOUT_S (300s), NOT the ABC's 900s default (KD-8) — the
    # constant is actually threaded to the call, not merely defined.
    assert fake.command_timeouts == [constants.EXEC_TIMEOUT_S]


async def test_verify_bounds_the_dev_log_tail() -> None:
    fake = FakeSandbox()
    fake.dev_ready = True
    fake.push_dev_logs("EARLY_SENTINEL_LINE")  # oldest line, beyond the tail window
    fake.push_dev_logs(*[f"filler {i}" for i in range(constants.LOG_TAIL_MAX_LINES + 50)])
    fake.push_dev_logs("⨯ unhandledRejection Error: boom at the tail")  # crash at the very end
    outcome, _ = await _verify(fake, log_cursor=0, max_polls=3)
    # The crash at the tail is still detected …
    assert outcome.error is not None and outcome.error.source == ErrorSource.SERVER
    # … but only the last LOG_TAIL_MAX_LINES lines are scanned — the oldest line is dropped.
    assert "EARLY_SENTINEL_LINE" not in outcome.error.cleaned_stack


# =============================================================================
# The dead-child rescue — "have you tried turning it off and on again?"
# =============================================================================


async def test_verify_restarts_a_dead_dev_server_and_goes_green() -> None:
    """THE 2026-07-30 prod incident, fixed: the dev child is dead (exit 137, OOM) and nothing
    serves the port — verify restarts it instead of blaming the app, and a healthy comeback is
    plain green: no error envelope, no repair run burned, no agent wild-goose chase."""
    fake = FakeSandbox()
    fake.kill_dev(exit_code=137)
    fake.become_ready_after(1)  # the rescue's status probe ticks once; ready on the next poll
    outcome, _ = await _verify(fake, log_cursor=0, max_polls=5)
    assert fake.dev_start_calls == 1  # the rescue relaunch
    assert outcome.green is True
    assert outcome.error is None
    assert outcome.dev_ready is True


async def test_verify_dead_server_unrevivable_reports_the_death_not_a_render_bug() -> None:
    """Restarted but still not ready: the diagnostic names the PROCESS failure (exit code,
    last output) — never the old 'throws during render' guess that sent the calculator build
    on a 3-run, ~875k-token chase after app code that was already correct."""
    fake = FakeSandbox()
    fake.push_dev_logs("npm ERR! Killed")
    fake.kill_dev(exit_code=137)
    outcome, _ = await _verify(fake, log_cursor=0, max_polls=3)
    assert fake.dev_start_calls == 1
    assert outcome.green is False and outcome.dev_ready is False
    assert outcome.error is not None and outcome.error.source == ErrorSource.SERVER
    detail = outcome.error.cleaned_stack
    assert "exit code 137" in detail
    assert "did not report ready within the readiness budget" in detail
    assert "npm ERR! Killed" in detail  # the child's last words made it into the diagnostic
    assert "throws during render" not in detail  # the misdiagnosis this fix retires


async def test_verify_dead_child_crash_last_words_surface_the_crash() -> None:
    # A crash marker in the dead child's last output is the TRUE diagnostic (KD-6: the tail
    # since the last cursor must be clean) — it wins even when the restarted child comes up
    # fine. The returned cursor is 0: the restart reset the C1 log ring, and re-reading the
    # fresh ring from 0 is what keeps the next verify's crash detection alive.
    fake = FakeSandbox()
    fake.push_dev_logs("⨯ ReferenceError: boom at module load")
    fake.kill_dev(exit_code=1)
    fake.become_ready_after(1)
    outcome, cursor = await _verify(fake, log_cursor=0, max_polls=5)
    assert outcome.green is False
    assert outcome.error is not None and outcome.error.source == ErrorSource.SERVER
    assert "boom at module load" in outcome.error.cleaned_stack
    assert cursor == 0  # ring reset observed — without it the cursor would still be 1


async def test_verify_dead_server_failed_restart_reports_honestly(monkeypatch) -> None:
    monkeypatch.setattr(selfheal, "VERIFY_RETRY_BACKOFF_S", 0.0)
    fake = FakeSandbox()
    fake.kill_dev(exit_code=137)
    fake.dev_start_error = SandboxError("dev/start failed with status 500")
    outcome, _ = await _verify(fake, log_cursor=0, max_polls=2)
    assert outcome.green is False
    assert outcome.error is not None and outcome.error.source == ErrorSource.SERVER
    assert "restart attempt failed" in outcome.error.cleaned_stack
    # The relaunch got the bounded transient-retry, then verify reported instead of raising.
    assert fake.dev_start_calls == constants.VERIFY_TRANSIENT_RETRIES + 1


async def test_verify_slow_but_running_server_is_never_restarted() -> None:
    # A LIVE child that has not reported ready is the slow-startup case (open-Q F): no rescue,
    # no error from verify — the harness's dev_not_ready fallback owns that diagnosis.
    fake = FakeSandbox()
    await fake.dev_start(fake.handle())  # the session-start launch
    outcome, _ = await _verify(fake, log_cursor=0, max_polls=2)
    assert fake.dev_start_calls == 1  # only the session-start call — a live child is left alone
    assert outcome.green is False and outcome.error is None


async def test_verify_unowned_serving_server_is_not_restarted() -> None:
    # `running=False, ready=True` (an agent-relaunched server answering the port): observed
    # truth says serving — no rescue, and the build verifies against it as before.
    fake = FakeSandbox()
    fake.dev_running = False
    fake.dev_ready = True
    outcome, _ = await _verify(fake, log_cursor=0, max_polls=3)
    assert fake.dev_start_calls == 0
    assert outcome.green is True


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
    tsc_runs = fake.command_calls.count(["npx", "tsc", "--noEmit"])
    assert tsc_runs == 2  # the blipped tsc attempt + the successful retry


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
    tsc_runs = fake.command_calls.count(["npx", "tsc", "--noEmit"])
    assert tsc_runs == attempts  # exhausted the budget, then escalated as today


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
    tsc_runs = fake.command_calls.count(["npx", "tsc", "--noEmit"])
    assert tsc_runs == 1  # no retry attempt followed the gone signal


# =============================================================================
# U4 — the compile errors `tsc` cannot see
# =============================================================================


async def test_a_next_only_compile_error_is_invisible_until_someone_asks_for_the_page() -> None:
    """★ U4 (R4), and the whole point of the unit. A Server Component calling a client-only hook
    typechecks CLEANLY, leaves `/dev/logs` empty, and keeps `/dev/status` reporting ready — so
    the build ended GREEN and shipped a blank page to the citizen. Next writes its `⨯` only when
    the route is actually requested. Driven through the log-cursor mechanics on purpose: stubbing
    `detect_server_crash` would assert the plumbing and prove nothing about the ordering."""
    fake = FakeSandbox()
    fake.dev_ready = True  # tsc default exit 0, readiness holds — green by every old measure
    fake.compile_error_appears_on_first_request(
        "⨯ ./app/page.tsx:3:1",
        "Ecmascript file had an error: You're importing a component that needs `useState`.",
    )

    outcome, _ = await _verify(fake, log_cursor=0, max_polls=3)

    assert fake.warm_calls == 1, "verify must issue the request that makes the error exist"
    assert outcome.green is False, "…and the build must end RED, not green over a blank page"
    assert outcome.error is not None and outcome.error.source == ErrorSource.SERVER
    assert "Ecmascript file had an error" in outcome.error.cleaned_stack, (
        "the repair prompt has to carry the real Next diagnostic, not a synthesized guess"
    )


async def test_a_clean_workspace_stays_green_and_costs_no_extra_iteration() -> None:
    """The other side of the same change: a warm request against a healthy app must not invent
    a red. U4 spends self-heal budget only where there is a genuine defect."""
    fake = FakeSandbox()
    fake.dev_ready = True

    outcome, _ = await _verify(fake, log_cursor=0, max_polls=3)

    assert fake.warm_calls == 1
    assert outcome.green is True and outcome.error is None


async def test_a_serving_probe_that_never_answers_is_indeterminate_not_broken() -> None:
    """★ COVERS AE8. The probe swallows its own failures and answers `None`, and `None` means WE
    could not ask — never that the app could not answer.

    This test replaces one that asserted the opposite conclusion from the same input: before U6 an
    unanswered probe left the verdict green, because the status was logged and discarded. Green
    was the wrong answer for the same reason red would have been. The app is very likely serving;
    we simply do not know, so the honest verdict is the one that asks again.

    Mutation check: map `served is None` to UNHEALTHY and this goes red on the state; map it to
    HEALTHY and it goes red on the `green` assertion."""
    fake = FakeSandbox()
    fake.dev_ready = True
    fake.warm_status = None  # the probe's "I could not reach it" answer

    outcome, _ = await _verify(fake, log_cursor=0, max_polls=3)

    assert outcome.state is HealthState.INDETERMINATE
    assert outcome.green is False, "an unreachable check is not a completion claim"
    assert outcome.error is None, "nothing to tell the model — we learned nothing"
    assert outcome.served is None


async def test_a_root_route_that_500s_without_a_marker_is_now_red() -> None:
    """★ THE SILENT GREEN, CLOSED. The status code came back from the app's own root and every
    call site discarded it — including this one, the only place in the codebase that decides
    whether a build is green. That verdict was `detect_server_crash` matching five hard-coded text
    markers, so a root route answering 500 while printing none of them shipped green over a broken
    app.

    The test that stood here asserted `green is True` and said in as many words that promoting a
    non-2xx "belongs to the owner, not to this test". R9 is that decision, taken. The supervisor's
    readiness probe fail-opens on 5xx by explicit design, so if this verdict does not call it
    broken, nothing does.

    Mutation check: widen the accepted range to include 5xx and this goes red on the state."""
    from structlog.testing import capture_logs

    fake = FakeSandbox()
    fake.dev_ready = True
    fake.warm_status = 500  # answered, and answered badly — but printed no recognized marker

    with capture_logs() as logs:
        outcome, _ = await _verify(fake, log_cursor=0, max_polls=3)

    assert outcome.state is HealthState.UNHEALTHY
    assert outcome.error is not None and outcome.error.source == ErrorSource.SERVER
    assert "500" in outcome.error.cleaned_stack
    complaints = [entry for entry in logs if entry["event"] == "verify_root_route_answered_badly"]
    assert len(complaints) == 1
    assert complaints[0]["status"] == 500


async def test_a_root_route_that_answers_200_says_nothing() -> None:
    """The companion bound: a healthy app must not emit the complaint, or the signal is noise
    and nobody will ever read it again."""
    from structlog.testing import capture_logs

    fake = FakeSandbox()
    fake.dev_ready = True

    with capture_logs() as logs:
        await _verify(fake, log_cursor=0, max_polls=3)

    assert [e for e in logs if e["event"] == "verify_root_route_answered_badly"] == []


# =============================================================================
# U6 / R9 / R10 — the three-state verdict, the serving half and the content half
# =============================================================================


async def test_where_are_we_tells_a_dead_process_from_a_slow_one() -> None:
    """★ THE DISTINCTION THE BOOLEAN COULD NOT MAKE. Both used to be `False`, and folding them
    together is what fed a slow startup to the model as a defect to fix.

    Mutation check: return `DIED` for the budget-spent arm and the third assertion goes red."""
    ready = FakeSandbox()
    await ready.dev_start(ready.handle())
    ready.become_ready_after(1)
    assert await where_are_we(ready, ready.handle(), max_polls=5, poll_s=0.0) is Readiness.READY

    dead = FakeSandbox()  # never started: running False, ready False
    assert await where_are_we(dead, dead.handle(), max_polls=5, poll_s=0.0) is Readiness.DIED

    slow = FakeSandbox()
    await slow.dev_start(slow.handle())  # running, and it never becomes ready inside the budget
    assert (
        await where_are_we(slow, slow.handle(), max_polls=2, poll_s=0.0) is Readiness.STILL_TRYING
    )


async def test_a_readiness_budget_that_ran_out_is_indeterminate_not_a_defect() -> None:
    """★ COVERS AE8. The dev server is still `running` — we stopped waiting, it did not stop
    starting. Before U6 this was red and carried `dev_not_ready_error()`, so the model spent a
    repair run, and the citizen's tokens, on a startup hang that may not exist.

    Mutation check: map `STILL_TRYING` to UNHEALTHY and both the state and the error go red."""
    fake = FakeSandbox()
    await fake.dev_start(fake.handle())  # running, never ready inside the budget

    outcome, _ = await _verify(fake, log_cursor=0, max_polls=2)

    assert outcome.state is HealthState.INDETERMINATE
    assert outcome.error is None, "we learned nothing, so there is nothing to diagnose"
    assert outcome.dev_ready is False


async def test_a_dev_process_that_is_down_is_still_a_defect() -> None:
    """The companion bound to the test above, and the one that stops INDETERMINATE from becoming
    a way to never fail: `running=False` is a real dead process and must stay red."""
    fake = FakeSandbox()  # never started at all
    fake.dev_start_error = SandboxError("supervisor will not start it")

    outcome, _ = await _verify(fake, log_cursor=0, max_polls=2)

    assert outcome.state is HealthState.UNHEALTHY
    assert outcome.error is not None


async def test_an_app_still_serving_the_starter_template_is_not_finished() -> None:
    """★ COVERS AE6 — THE 2026-08-18 HEADLINE. `tsc` is clean, the dev server is ready, the log
    tail is quiet, the root answers 200, and `app/page.tsx` is byte-for-byte the golden template.
    Every signal the platform had said green, and "Build complete — your app is live below" sat
    above the untouched starter page for nine minutes in front of a client.

    Mutation check: drop the `STILL_THE_BASELINE` arm and this goes green — which is precisely
    the bug."""
    fake = FakeSandbox()
    fake.dev_ready = True
    fake.baseline_stdout = BASELINE_UNTOUCHED_STDOUT

    outcome, _ = await _verify(fake, log_cursor=0, max_polls=3, had_prior_building_turns=True)

    assert outcome.state is HealthState.UNHEALTHY
    assert outcome.green is False
    assert outcome.baseline is BaselineIdentity.STILL_THE_BASELINE
    assert outcome.error is not None and outcome.error.source == ErrorSource.SERVER
    assert "starter template" in outcome.error.cleaned_stack


async def test_an_app_whose_root_route_the_agent_rewrote_is_healthy() -> None:
    """The other half of the same check. It also answers the redirect question: an agent that
    replaces the root with a redirect has WRITTEN `app/page.tsx`, so the blob differs, so the app
    has diverged from its baseline — healthy, and for the right reason rather than by accident."""
    fake = FakeSandbox()
    fake.dev_ready = True  # the fake's default baseline stdout is a rewritten root route

    outcome, _ = await _verify(fake, log_cursor=0, max_polls=3, had_prior_building_turns=True)

    assert outcome.state is HealthState.HEALTHY
    assert outcome.baseline is BaselineIdentity.DIVERGED
    assert outcome.error is None


async def test_a_brand_new_app_showing_the_template_is_not_accused_of_anything() -> None:
    """A project nobody has built yet is SUPPOSED to be showing the starter page. The check is
    not merely tolerant of that case — it is never asked, which is what stops it manufacturing an
    accusation the moment someone asks their first question about a new project.

    Mutation check: drop the `had_prior_building_turns` gate and this goes red."""
    fake = FakeSandbox()
    fake.dev_ready = True
    fake.baseline_stdout = BASELINE_UNTOUCHED_STDOUT

    outcome, _ = await _verify(fake, log_cursor=0, max_polls=3, had_prior_building_turns=False)

    assert outcome.state is HealthState.HEALTHY
    assert outcome.baseline is None, "not merely tolerated — never asked"
    assert not any(cmd[0] == "sh" for cmd in fake.command_calls)


async def test_a_baseline_the_repository_cannot_answer_for_is_indeterminate() -> None:
    """No root commit, more than one root, or a root commit that never held the file. An app
    cannot be convicted of showing the template by a check that could not find the template —
    and it cannot be cleared by one either.

    Mutation check: collapse UNANSWERABLE into either of the other two arms and this goes red."""
    for stdout, why in [
        ("@@@@", "no root commit at all"),
        (f"{'a' * 40}\n{'b' * 40}@@{'c' * 40}@@{'d' * 40}", "two root commits"),
        (f"{'a' * 40}@@@@{'d' * 40}", "the root commit never held the file"),
        ("", "unparseable output"),
    ]:
        fake = FakeSandbox()
        fake.dev_ready = True
        fake.baseline_stdout = stdout

        outcome, _ = await _verify(fake, log_cursor=0, max_polls=3, had_prior_building_turns=True)

        assert outcome.state is HealthState.INDETERMINATE, why
        assert outcome.error is None, why


async def test_a_type_error_outranks_the_content_check() -> None:
    """Ordering is the diagnosis. An app that does not type-check is not an app whose rendered
    output is worth arguing about, so the model is handed the type error rather than a complaint
    about its home page."""
    fake = FakeSandbox()
    fake.dev_ready = True
    fake.baseline_stdout = BASELINE_UNTOUCHED_STDOUT
    fake.queue_commands(ExecResult(stdout="app/x.tsx(1,1): error TS2322: bad", stderr="", exit=2))

    outcome, _ = await _verify(fake, log_cursor=0, max_polls=3, had_prior_building_turns=True)

    assert outcome.state is HealthState.UNHEALTHY
    assert outcome.error is not None and outcome.error.source == ErrorSource.TSC


async def test_the_raw_served_head_is_carried_beside_the_derived_verdict() -> None:
    """The 2026-08-02 learning, applied: a derived metric produced a false P0 that the raw field
    disproved in one step. Whoever asks "but what was it actually serving?" must not have to
    reproduce the run to find out."""
    from structlog.testing import capture_logs

    fake = FakeSandbox()
    fake.dev_ready = True
    fake.served_head = "<!DOCTYPE html><title>VIP tracker</title>"

    with capture_logs() as logs:
        outcome, _ = await _verify(fake, log_cursor=0, max_polls=3, had_prior_building_turns=True)

    assert outcome.served is not None
    assert outcome.served.head == "<!DOCTYPE html><title>VIP tracker</title>"
    verdicts = [entry for entry in logs if entry["event"] == "verify_verdict"]
    assert len(verdicts) == 1
    assert verdicts[0]["served_head"] == "<!DOCTYPE html><title>VIP tracker</title>"
    assert verdicts[0]["state"] is HealthState.HEALTHY


async def test_an_indeterminate_verdict_is_asked_again_before_it_is_believed() -> None:
    """★ "The check is retried rather than failed" (AE8). The patience lives in `verify` rather
    than at either loop, because `selfheal` is the ONE health authority both harnesses consult —
    a budget applied in the turn engine and forgotten in the legacy harness would be a health
    rule with an escape hatch.

    Mutation check: return on the first pass regardless of state and this goes red."""
    fake = _AnswersOnTheSecondLook()
    fake.dev_ready = True
    fake.warm_status = None  # unanswerable on the first look…

    outcome, _ = await _verify(fake, log_cursor=0, max_polls=3, indeterminate_retries=2)

    assert fake.serving_calls == 2, "asked again, and stopped as soon as it got an answer"
    assert outcome.state is HealthState.HEALTHY


async def test_patience_is_bounded_and_an_unanswerable_verdict_is_returned_as_one() -> None:
    """The bound on the test above. A verdict that stays unanswerable is returned AS
    INDETERMINATE — nothing inside `verify` converts it into a red one behind the loops' backs,
    because what an unanswerable verdict COSTS is the loop's decision, not the authority's."""
    fake = FakeSandbox()
    fake.dev_ready = True
    fake.warm_status = None

    outcome, _ = await _verify(fake, log_cursor=0, max_polls=3, indeterminate_retries=2)

    assert outcome.state is HealthState.INDETERMINATE
    assert fake.serving_calls == 3, "one pass plus exactly two retries"


async def test_may_never_be_green_is_true_for_exactly_one_state() -> None:
    """`green` is a property, not a field, for the reason `CopyVerdict.may_destroy` is one:
    `state is HEALTHY` spelled out at every call site is a chance at each one to write `is not
    UNHEALTHY` instead — which reads an unanswerable verdict as a completion claim."""
    greens = [
        state
        for state in HealthState
        if VerifyOutcome(state=state, dev_ready=True, error=None, preview_url=None).green
    ]
    assert greens == [HealthState.HEALTHY]


# =============================================================================
# U9 / R15 — the stale-evidence re-check
# =============================================================================


class _AnswersOnTheSecondLook(FakeSandbox):
    """A container whose root route is unreachable on the first ask and answers on the second.

    A SUBCLASS rather than a reassigned bound method, which is what this file's own
    `_WarmOrderSandbox` note explains: assigning over a bound method is a type error under `ty`,
    and the fakes here are subclassed anyway."""

    async def what_is_it_serving(self, handle: SandboxHandle) -> ServedPage | None:
        if self.serving_calls >= 1:
            self.warm_status = 200
        return await super().what_is_it_serving(handle)


async def test_a_crash_the_agent_has_already_fixed_costs_no_repair_round_trip() -> None:
    """★ COVERS AE12. Three of the four repair cycles in the 2026-08-18 demo were the platform
    re-reporting errors it had already fixed, and the mechanism is structural: `log_cursor` bounds
    the read by log POSITION rather than by agent action, a dev-server restart resets the ring
    underneath it, and a dead child's last words are carried forward on purpose. So a crash
    printed before the agent's edit can be read after it and charged as a fresh defect.

    Here the log holds a crash, the agent HAS written since the watermark, and the re-check's
    fresh window is clean — so the verdict is healthy and no repair is bought.

    Mutation check: drop the `changed is True` gate (or the `continue`) and this goes red."""
    fake = FakeSandbox()
    fake.dev_ready = True
    fake.push_dev_logs("⨯ unhandledRejection Error: the thing the agent already fixed")
    fake.changed_since_watermark = True

    outcome, _ = await _verify(fake, log_cursor=0, max_polls=3)

    assert outcome.state is HealthState.HEALTHY
    assert outcome.error is None
    assert fake.command_calls.count(["npx", "tsc", "--noEmit"]) == 2, "one pass, then the re-check"


async def test_a_crash_that_is_still_there_after_the_re_check_still_costs_a_repair() -> None:
    """The bound on the test above, and the one that stops the re-check becoming a way to never
    fail. Next re-emits its diagnostic every time the route is requested, so a REAL compile error
    reappears in the re-check's fresh window and the verdict stays red.

    Mutation check: make the re-check return its own verdict unconditionally without re-reading
    the logs and this goes green."""
    fake = FakeSandbox()
    fake.dev_ready = True
    fake.changed_since_watermark = True
    # The failure is CURRENT: every request to the route re-prints it, so it lands in the
    # re-check's window exactly as it landed in the first one.
    fake.compile_error_appears_on_first_request("⨯ ./app/page.tsx:3:1 still broken")

    outcome, _ = await _verify(fake, log_cursor=0, max_polls=3)

    assert outcome.state is HealthState.UNHEALTHY
    assert outcome.error is not None and outcome.error.source == ErrorSource.SERVER


async def test_a_file_written_through_the_shell_still_advances_the_watermark() -> None:
    """The open sandbox lets the agent edit through `run_command` as readily as through the file
    tools, so a watermark counted from tool calls would miss every `sed`, every install and every
    shell redirect. This container reports a newer file having served ZERO file-tool calls.

    Mutation check: source the watermark from tool bookkeeping instead of the filesystem and the
    re-check never fires here."""
    fake = FakeSandbox()
    fake.dev_ready = True
    fake.push_dev_logs("⨯ unhandledRejection Error: stale")
    fake.changed_since_watermark = True  # `find -newer` prints a path; no tool wrote it

    outcome, _ = await _verify(fake, log_cursor=0, max_polls=3)

    assert outcome.state is HealthState.HEALTHY
    assert fake.watermark_stamps == 0, "the loops stamp it; verify only ever asks"


async def test_a_container_that_cannot_answer_the_watermark_changes_nothing() -> None:
    """`None` is not folded into `False`, and it is not folded into `True` either. A container
    that cannot answer costs the improvement, never the correctness — the verdict is exactly what
    it was before U9."""
    fake = FakeSandbox()
    fake.dev_ready = True
    fake.push_dev_logs("⨯ unhandledRejection Error: boom")

    fake.probes_fail = True

    outcome, _ = await _verify(fake, log_cursor=0, max_polls=3)

    assert outcome.state is HealthState.UNHEALTHY
    assert outcome.error is not None and outcome.error.source == ErrorSource.SERVER


async def test_the_re_check_is_gated_on_the_evidence_not_on_the_verdict() -> None:
    """A failed type-check, a 500 from the root route and a baseline comparison are all produced
    during the pass that reads them. They cannot be stale, so they must never buy a second pass —
    that gate is the whole reason the re-check is cheap enough to be unconditional.

    Mutation check: gate on the verdict instead of on `rests_on_log_evidence` and the tsc arm
    starts running two passes."""
    fake = FakeSandbox()
    fake.dev_ready = True
    fake.changed_since_watermark = True
    fake.queue_commands(
        ExecResult(stdout="app/x.tsx(1,1): error TS2322: bad", stderr="", exit=2),
        ExecResult(stdout="", stderr="", exit=0),  # would make a second pass go green
    )

    outcome, _ = await _verify(fake, log_cursor=0, max_polls=3)

    assert outcome.state is HealthState.UNHEALTHY
    assert outcome.error is not None and outcome.error.source == ErrorSource.TSC
    assert fake.command_calls.count(["npx", "tsc", "--noEmit"]) == 1, "no second pass was bought"


async def test_the_re_check_happens_once_so_a_busy_container_cannot_loop_it() -> None:
    """The container keeps reporting changes and the crash keeps reappearing. Exactly two passes
    run — the re-check is once per call, not once per change."""
    fake = FakeSandbox()
    fake.dev_ready = True
    fake.changed_since_watermark = True
    fake.compile_error_appears_on_first_request("⨯ ./app/page.tsx:3:1 broken every time")

    await _verify(fake, log_cursor=0, max_polls=3)

    assert fake.command_calls.count(["npx", "tsc", "--noEmit"]) == 2


async def test_a_died_diagnostic_that_postdates_the_watermark_is_preserved() -> None:
    """The carried-forward `died_lines` behaviour is deliberate — a crash marker in a dead child's
    last words is the true diagnostic even when the restarted child comes up clean — and U9 must
    not delete it. Nothing changed since the watermark, so the death stands as reported."""
    fake = FakeSandbox()
    fake.kill_dev(exit_code=137)
    fake.push_dev_logs("⨯ FATAL: out of memory while loading app/layout.tsx")
    fake.changed_since_watermark = False

    outcome, _ = await _verify(fake, log_cursor=0, max_polls=2)

    assert outcome.state is HealthState.UNHEALTHY
    assert outcome.error is not None and outcome.error.source == ErrorSource.SERVER
    assert "out of memory" in outcome.error.cleaned_stack


async def test_a_stale_crash_marker_from_a_previous_run_is_not_re_reported() -> None:
    """The `log_cursor` handoff still excludes lines an earlier verify already reported —
    otherwise the first real error would be re-diagnosed on every subsequent iteration and the
    self-heal loop would never converge."""
    fake = FakeSandbox()
    fake.dev_ready = True
    fake.push_dev_logs("⨯ unhandledRejection Error: this was iteration one's problem")

    first, cursor = await _verify(fake, log_cursor=0, max_polls=3)
    assert first.error is not None and first.error.source == ErrorSource.SERVER

    second, _ = await _verify(fake, log_cursor=cursor, max_polls=3)

    assert second.green is True, "the stale marker sits behind the cursor and must stay there"


async def test_the_new_red_path_terminates_instead_of_looping(
    db_session, billing_factory, sink
) -> None:
    """★ THE BLAST-RADIUS GUARD for U4. This unit deliberately changes build outcomes: work that
    ended green-with-a-blank-app now ends red and spends self-heal budget. An unterminating red
    would be far worse than the bug — so a workspace whose compile error survives every repair
    must exhaust the budget and STOP, with the real Next diagnostic on the way out."""
    user = await UserFactory.create(db_session)
    fake = FakeSandbox()
    fake.dev_ready = True  # tsc clean forever; only the warm request ever finds the defect
    fake.compile_error_appears_on_first_request(
        "⨯ ./app/page.tsx:3:1",
        "Ecmascript file had an error: You're importing a component that needs `useState`.",
    )
    turns = [
        t for _ in range(4) for t in (tool_turn("declare_done", {"summary": "x"}), text_turn())
    ]
    orchestrator, _ = make_orchestrator(scripted_model(turns), billing_factory)

    result = await orchestrator.run_build(uuid.uuid4(), user.id, fake, sink)

    assert result.status == BuildSessionStatus.FAILED
    escalations = [e for e in sink.events if e.type == "escalation"]
    assert len(escalations) == 1 and escalations[0].reason == "self_heal_budget_exhausted"
    errors = [e for e in sink.events if e.type == "error"]
    assert errors and all(e.source == ErrorSource.SERVER for e in errors), (
        "reported as a SERVER error carrying Next's own words — not a synthesized tsc guess"
    )


async def test_a_dev_server_that_never_came_up_is_not_asked_for_a_page() -> None:
    """ "After readiness" is a precondition, not just an ordering. A server that never came up
    has nothing to answer with, so warming it spends the helper's whole budget re-learning what
    the readiness poll just established — up to three times per build, on exactly the red path
    where the citizen is already waiting longest. U4's case is the opposite one: ready is TRUE,
    `tsc` is clean, and the page is still blank."""
    fake = FakeSandbox()  # dev server down, and it never becomes ready

    outcome, _ = await _verify(fake, log_cursor=0, max_polls=2)

    assert outcome.dev_ready is False
    assert fake.warm_calls == 0, "nothing to ask, so we do not spend the budget asking"
