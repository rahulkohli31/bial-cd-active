"""The self-heal state machine — `verify`, `where_are_we`, the re-check.

ONE layer now: the verify primitives, driven against a fake container and asserted on the
`VerifyOutcome` they return. The second layer this file used to carry — the standalone build
harness's multi-run loop, driven through `BuildOrchestrator.run_build` — was deleted with the
harness. Its live successor is the turn engine's self-heal loop, whose budget, re-seed and
endings are pinned in `tests/services/turns/test_write_turn.py` and `test_run_budget.py`; the
re-seed prompt itself is `prompt.build_repair_prompt`, pinned in `test_prompt.py`.

`selfheal` is the ONE health authority the live loop consults, so what is asserted here is what
the loop is entitled to believe: a verdict is green, red-with-a-named-defect, or honestly
unanswerable — never red-with-nothing-to-repair.
"""

from __future__ import annotations

import uuid

import pytest

from src.api.v1.build_sessions.schemas import ErrorSource
from src.core.integrity_types import BaselineIdentity
from src.services.orchestrator import constants, selfheal
from src.services.orchestrator.client_errors import forget_all_client_errors, park_client_error
from src.services.orchestrator.selfheal import (
    HealthState,
    Readiness,
    VerifyOutcome,
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
from tests.services.orchestrator.fake_sandbox import BASELINE_UNTOUCHED_STDOUT, FakeSandbox


@pytest.fixture(autouse=True)
def _empty_client_error_store():
    """The report store is a module-global that outlives a test. Emptying it on BOTH sides means
    neither a leftover from an earlier test nor a leak into a later one can make an assertion in
    this file (or anywhere else in the suite) pass for the wrong reason."""
    forget_all_client_errors()
    yield
    forget_all_client_errors()


_APP_ID = uuid.UUID("0198f2c0-0000-7000-8000-000000000006")


async def _verify(
    fake: FakeSandbox,
    *,
    log_cursor: int = 0,
    max_polls: int = 3,
    had_prior_building_turns: bool = False,
    indeterminate_retries: int = 0,
) -> tuple[VerifyOutcome, int]:
    """`verify` with this file's defaults, plus two DELIBERATE overrides, not mere convenience:
    `indeterminate_retries=0` (production retries before reporting a defect; zero surfaces ONE
    honest verdict instead of a patient test re-confirming it three times — the retry path has
    its own test below) and `had_prior_building_turns=False` (the content check stays off unless
    a test opts in)."""
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
    )


# =============================================================================
# Pure verify primitives — no DB
# =============================================================================


def test_detect_server_crash_matches_markers_not_benign_lines() -> None:
    assert detect_server_crash(["GET / 200 in 30ms", "compiled ok"]) is None
    crash = detect_server_crash(["  ⨯ unhandledRejection Error: boom", "  at RecordsPage"])
    assert crash is not None and "boom" in crash


async def test_where_are_we_ready_after_polls() -> None:
    fake = FakeSandbox()
    await fake.dev_start(fake.handle())
    fake.become_ready_after(2)
    assert await where_are_we(fake, fake.handle(), max_polls=5, poll_s=0.0) is Readiness.READY


async def test_where_are_we_dead_process_is_not_slow() -> None:
    fake = FakeSandbox()  # dev_running False, never started
    assert await where_are_we(fake, fake.handle(), max_polls=5, poll_s=0.0) is Readiness.DIED


async def test_where_are_we_believes_ready_over_a_dead_child() -> None:
    """The `/dev/status` row (`running=False, ready=True`): the supervisor's child is dead
    but an agent-relaunched server answers the dev port — observed truth says serving. The
    `ready` check runs FIRST, so this is "there", never the dead-process fast-fail. Pins that
    ordering: swap the two checks and this goes red."""
    fake = FakeSandbox()
    fake.dev_running = False
    fake.dev_ready = True
    assert await where_are_we(fake, fake.handle(), max_polls=5, poll_s=0.0) is Readiness.READY


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
    # Only `tsc` is ever run between runs — `next build` is a DEPLOY concern.
    assert fake.command_calls == [["npx", "tsc", "--noEmit"]]
    assert not any("build" in " ".join(cmd) for cmd in fake.command_calls)


async def test_verify_bounds_the_tsc_run_with_exec_timeout() -> None:
    fake = FakeSandbox()
    fake.dev_ready = True
    await _verify(fake, log_cursor=0, max_polls=3)
    # The tsc run is bounded by EXEC_TIMEOUT_S (300s), NOT the ABC's 900s default — the
    # constant is actually threaded to the call, not merely defined.
    assert fake.command_timeouts == [constants.EXEC_TIMEOUT_S]


async def test_verify_bounds_the_dev_log_tail() -> None:
    fake = FakeSandbox()
    fake.dev_ready = True
    fake.push_dev_logs("EARLY_SENTINEL_LINE")  # oldest line, beyond the tail window
    fake.push_dev_logs(*[f"filler {i}" for i in range(constants.LOG_TAIL_MAX_LINES + 50)])
    fake.push_dev_logs("⨯ unhandledRejection Error: boom at the tail")  # crash at the very end
    outcome, _ = await _verify(fake, log_cursor=0, max_polls=3)
    assert outcome.error is not None and outcome.error.source == ErrorSource.SERVER
    assert "EARLY_SENTINEL_LINE" not in outcome.error.cleaned_stack


# =============================================================================
# The dead-child rescue — "have you tried turning it off and on again?"
# =============================================================================


async def test_verify_restarts_a_dead_dev_server_and_goes_green() -> None:
    """The dev child is dead (exit 137, OOM) and nothing
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
    last output) — never the old 'throws during render' guess."""
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
    # A crash marker in the dead child's last output is the TRUE diagnostic (the tail since
    # the last cursor must be clean) — it wins even when the restarted child comes up
    # fine. The returned cursor is 0: the restart reset the log ring, and re-reading the
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
    """A LIVE child that has not reported ready is the slow-startup case: no rescue.

    The diagnosis moved but the RESCUE rule did not, which is what this pins. The
    verdict here was red-with-no-error and the loop synthesized one; now `verify` names it itself
    once its patience is spent. Either way nothing restarts a child that is merely slow."""
    fake = FakeSandbox()
    await fake.dev_start(fake.handle())  # the session-start launch
    outcome, _ = await _verify(fake, log_cursor=0, max_polls=2)
    assert fake.dev_start_calls == 1  # only the session-start call — a live child is left alone
    assert outcome.green is False


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
# The dev server that never came up — the verdict the loop must not misread
# =============================================================================


async def test_dev_never_ready_reseeds_a_diagnostic_not_the_done_nudge() -> None:
    """tsc clean, no crash marker, but the dev server never becomes ready: the verdict must NOT
    be red-with-no-error. A caller handed `green=False, error=None` has nothing to repair, so it
    falls through to the "green but forgot declare_done" nudge and tells the model to carry on
    — a misdiagnosis, on the one path where the citizen is already waiting longest.

    RE-HOSTED ONTO `verify`. It used to be asserted through the standalone harness's multi-run
    loop, on the reseeded prompt text of the next run; the loop is deleted and the live one is
    the turn engine's. What was ever `selfheal`'s in it is the half asserted here: `verify`
    NAMES this state itself rather than leaving the caller to synthesize something. The re-seed
    channel that carries the diagnostic into the next run is `prompt.build_repair_prompt`
    (`test_prompt.py`), and the live loop that spends it is `turns/engine.py`
    (`tests/services/turns/test_write_turn.py`).

    Mutation check: return `error=None` on the never-ready arm and the source assert goes red."""
    fake = FakeSandbox()
    fake.dev_ready = False  # never becomes ready; default tsc exit 0; no crash logs

    outcome, _ = await _verify(fake, log_cursor=0, max_polls=3)

    assert outcome.green is False
    assert outcome.dev_ready is False
    # …and it is DIAGNOSED, not merely failed: an error object is what stops a caller reading
    # this as "green, the model just forgot to declare done".
    assert outcome.error is not None and outcome.error.source == ErrorSource.SERVER
    assert "did not report ready" in outcome.error.cleaned_stack


# =============================================================================
# Transient sandbox errors during verify — bounded retry, never a hard failure
# =============================================================================


async def test_verify_transient_blip_is_retried_not_escalated(monkeypatch) -> None:
    # One supervisor blip on the tsc hop must NOT escape `verify` — the bounded retry absorbs it
    # and the pass returns its ordinary verdict. (Re-hosted onto `verify`: the assertion
    # that the caller then does not escalate belonged to the deleted harness loop; the retry
    # itself is `selfheal._attempt`'s and is what is measured here.)
    monkeypatch.setattr(selfheal, "VERIFY_RETRY_BACKOFF_S", 0.0)
    fake = FakeSandbox()
    fake.dev_ready = True
    fake.queue_exec_errors(SandboxError("transient supervisor blip"))

    outcome, _ = await _verify(fake, log_cursor=0, max_polls=3)

    assert outcome.green is True  # the blip cost nothing: the verdict is the healthy one
    assert outcome.error is None
    tsc_runs = fake.command_calls.count(["npx", "tsc", "--noEmit"])
    assert tsc_runs == 2  # the blipped tsc attempt + the successful retry


async def test_verify_persistent_transient_errors_escalate_after_retries(monkeypatch) -> None:
    # The budget is finite: once it is spent the error is raised to the caller (which is what
    # the loop turns into its internal_error escalation), never absorbed into a green.
    monkeypatch.setattr(selfheal, "VERIFY_RETRY_BACKOFF_S", 0.0)
    fake = FakeSandbox()
    fake.dev_ready = True
    attempts = constants.VERIFY_TRANSIENT_RETRIES + 1
    fake.queue_exec_errors(*(SandboxError("supervisor still down") for _ in range(attempts)))

    with pytest.raises(SandboxError):
        await _verify(fake, log_cursor=0, max_polls=3)

    tsc_runs = fake.command_calls.count(["npx", "tsc", "--noEmit"])
    assert tsc_runs == attempts  # exhausted the budget, then raised as today


async def test_verify_sandbox_gone_escalates_immediately_without_retry() -> None:
    # Gone is terminal for the handle (restore-needed): no retry may be burned on it, and it must
    # stay a `SandboxGoneError` all the way out — never be blurred into the transient retry, which
    # is what lets a caller keep its dedicated sandbox_gone arm.
    fake = FakeSandbox()
    fake.dev_ready = True
    fake.queue_exec_errors(SandboxGoneError("container torn down mid-verify"))

    with pytest.raises(SandboxGoneError):
        await _verify(fake, log_cursor=0, max_polls=3)

    tsc_runs = fake.command_calls.count(["npx", "tsc", "--noEmit"])
    assert tsc_runs == 1  # no retry attempt followed the gone signal


# =============================================================================
# The compile errors `tsc` cannot see
# =============================================================================


async def test_a_next_only_compile_error_is_invisible_until_someone_asks_for_the_page() -> None:
    """★ A Server Component calling a client-only hook typechecks clean and ships a blank page —
    `tsc`, `/dev/status`, and the log tail all stay silent until the route is actually requested.
    Driven through the log-cursor mechanics on purpose: stubbing `detect_server_crash` would
    assert the plumbing and prove nothing about the ordering."""
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
    a red. Self-heal budget is spent only where there is a genuine defect."""
    fake = FakeSandbox()
    fake.dev_ready = True

    outcome, _ = await _verify(fake, log_cursor=0, max_polls=3)

    assert fake.warm_calls == 1
    assert outcome.green is True and outcome.error is None


async def test_a_serving_probe_that_never_answers_is_indeterminate_not_broken() -> None:
    """The probe swallows its own failures and answers `None`, meaning WE could not ask — never
    that the app could not answer.

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
    whether a build is green: a root route answering 500 while printing none of
    `detect_server_crash`'s five hard-coded text markers shipped green over a broken app.

    The supervisor's readiness probe fail-opens on 5xx by explicit design, so if this verdict
    does not call it broken, nothing does.

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
# The three-state verdict, the serving half and the content half
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


async def test_a_readiness_budget_that_ran_out_is_asked_again_not_reported() -> None:
    """★ The dev server is still `running` — we stopped waiting, it did not stop starting.
    Reported red it would carry `dev_not_ready_error()`, and the model would spend a repair run,
    and the citizen's tokens, on a startup hang that does not exist.

    THE WIN IS THE SECOND LOOK, not a permanent verdict: an app that comes up while we were
    deciding is healthy, and costs nothing.

    Mutation check: map `STILL_TRYING` to UNHEALTHY and this goes red."""
    fake = FakeSandbox()
    await fake.dev_start(fake.handle())
    fake.become_ready_after(3)  # not inside the first budget of 2; comfortably inside the retry

    outcome, _ = await _verify(fake, log_cursor=0, max_polls=2, indeterminate_retries=2)

    assert outcome.state is HealthState.HEALTHY
    assert outcome.error is None


async def test_a_readiness_budget_that_never_answers_becomes_an_honest_defect() -> None:
    """★ THE BOUND ON THE TEST ABOVE: this silence is not the same kind as the others. A serving
    probe that never answers, or a baseline with no root commit, describes an app that IS up. A
    READINESS budget that ran out describes an app that is not serving at all — once patience is
    spent that is a fact about the app, not our impatience.

    Mutation check: drop the `Unanswered.READINESS` conversion and this goes red on the state."""
    fake = FakeSandbox()
    await fake.dev_start(fake.handle())  # running, and it never becomes ready

    outcome, _ = await _verify(fake, log_cursor=0, max_polls=2, indeterminate_retries=2)

    assert outcome.state is HealthState.UNHEALTHY
    assert outcome.error is not None and "did not report ready" in outcome.error.title
    assert outcome.dev_ready is False


async def test_an_unanswered_probe_does_not_become_a_startup_defect() -> None:
    """The companion bound, and the one that keeps the conversion narrow. This app IS serving —
    it answered readiness — and only our own request came back empty. Converting that into "the
    dev server did not report ready" would be the fabricated diagnosis all over again."""
    fake = FakeSandbox()
    fake.dev_ready = True
    fake.warm_status = None

    outcome, _ = await _verify(fake, log_cursor=0, max_polls=2, indeterminate_retries=1)

    assert outcome.state is HealthState.INDETERMINATE
    assert outcome.error is None


async def test_a_dev_process_that_is_down_is_still_a_defect() -> None:
    """The companion bound to the test above, and the one that stops INDETERMINATE from becoming
    a way to never fail: `running=False` is a real dead process and must stay red."""
    fake = FakeSandbox()  # never started at all
    fake.dev_start_error = SandboxError("supervisor will not start it")

    outcome, _ = await _verify(fake, log_cursor=0, max_polls=2)

    assert outcome.state is HealthState.UNHEALTHY
    assert outcome.error is not None


async def test_an_app_still_serving_the_starter_template_is_not_finished() -> None:
    """`tsc` is clean, the dev server is ready, the log tail is quiet, the root answers 200, and
    `app/page.tsx` is byte-for-byte the golden template — every server-side signal clean over an
    app that is not the user's.

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
    """A derived metric can produce a false critical verdict that the raw field disproves in one
    step. Whoever asks "but what was it actually serving?" must not have to reproduce the run to
    find out."""
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
    """★ "The check is retried rather than failed". The patience lives in `verify` rather
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
    """`green` is a property, not a field, for the reason `IntegrityVerdict.may_restore` is one:
    `state is HEALTHY` spelled out at every call site is a chance at each one to write `is not
    UNHEALTHY` instead — which reads an unanswerable verdict as a completion claim."""
    greens = [
        state
        for state in HealthState
        if VerifyOutcome(state=state, dev_ready=True, error=None, preview_url=None).green
    ]
    assert greens == [HealthState.HEALTHY]


# =============================================================================
# The stale-evidence re-check
# =============================================================================


class _AnswersOnTheSecondLook(FakeSandbox):
    """A container whose root route is unreachable on the first ask and answers on the second.

    A SUBCLASS rather than a reassigned bound method — assigning over one is a type error
    under `ty`, so the fakes here are subclassed anyway."""

    async def what_is_it_serving(self, handle: SandboxHandle) -> ServedPage | None:
        if self.serving_calls >= 1:
            self.warm_status = 200
        return await super().what_is_it_serving(handle)


async def test_a_crash_the_agent_has_already_fixed_costs_no_repair_round_trip() -> None:
    """The log holds a crash, the agent HAS written since the watermark, and the re-check's fresh
    window is clean — so the verdict is healthy and no repair is bought. Why a crash can be stale
    in the first place is `selfheal.verify`'s re-check comment.

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
    that cannot answer costs the improvement, never the correctness."""
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
    last words is the true diagnostic even when the restarted child comes up clean — and future
    changes must not delete it. Nothing changed since the watermark, so the death stands as
    reported."""
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


async def test_a_dev_server_that_never_came_up_is_not_asked_for_a_page() -> None:
    """ "After readiness" is a precondition, not just an ordering. A server that never came up
    has nothing to answer with, so warming it spends the helper's whole budget re-learning what
    the readiness poll just established — up to three times per build, on exactly the red path
    where the citizen is already waiting longest.
    `test_a_next_only_compile_error_is_invisible_until_someone_asks_for_the_page` covers the
    opposite case: ready is TRUE, `tsc` is clean, and the page is still blank."""
    fake = FakeSandbox()  # dev server down, and it never becomes ready

    outcome, _ = await _verify(fake, log_cursor=0, max_polls=2)

    assert outcome.dev_ready is False
    assert fake.warm_calls == 0, "nothing to ask, so we do not spend the budget asking"


async def test_a_root_route_that_redirects_counts_as_serving() -> None:
    """The redirect scenario, asserted on the VERDICT rather than only on the failure copy.

    An agent that replaces the root with a redirect has built something: the route compiled and
    answered, which is the whole question the serving half asks. Reading 3xx as "not serving"
    would fail every app whose home page sends the citizen somewhere else — a perfectly ordinary
    shape — and the content half would never even be consulted.

    Mutation check: narrow the accepted range to `200 <= status < 300` and this goes red."""
    for status in (301, 302, 307, 308):
        fake = FakeSandbox()
        fake.dev_ready = True
        fake.warm_status = status

        outcome, _ = await _verify(fake, log_cursor=0, max_polls=3, had_prior_building_turns=True)

        assert outcome.state is HealthState.HEALTHY, f"{status} answered — the route compiled"
        assert outcome.error is None


async def test_an_indeterminate_verdict_never_reaches_a_teardown_or_a_restore() -> None:
    """★ The directly-asserted safety property: no ambiguous verdict may reach a
    destructive branch.

    Asserted on the CONTAINER rather than on a code path, because that is what actually matters —
    `teardown_calls` counts every delete this verdict could have caused, and the fake records one
    whether the caller was the loop, the funnel or a compensation.

    RE-HOSTED ONTO `verify`. It used to drive the standalone harness's whole loop and count
    teardowns across it; that loop is deleted, and the live one (`turns/engine.py`) reaches no
    teardown from a verdict at all. `verify` is the one surviving thing that both produces the
    INDETERMINATE verdict and holds a container handle, so this is where the property stays
    checkable."""
    fake = FakeSandbox()
    fake.dev_ready = True
    fake.warm_status = None  # the serving probe never comes back: INDETERMINATE, forever
    before = fake.handle()

    outcome, _ = await _verify(fake, log_cursor=0, max_polls=3)

    assert outcome.state is HealthState.INDETERMINATE  # liveness: the verdict really is that one
    assert fake.teardown_calls == 0, "an unanswerable verdict may not destroy a container"
    # …nor provision over one. `provision_new` is what a restore lands through on this fake, and
    # it swaps the handle — so an unchanged handle is the restore not having happened.
    assert fake.handle() == before


async def test_a_re_check_that_comes_back_unanswerable_is_retried_not_charged() -> None:
    """★ The re-check and the patience budget compose rather than colliding: the re-check runs
    because evidence could be stale, and when the fresh pass cannot answer either, patience takes
    over. What must NOT happen is that pair resolving into a defect the citizen is charged for —
    the crash marker is behind the cursor by then, so calling it red would report evidence nobody
    re-read.

    Mutation check: return on the first INDETERMINATE and the pass count goes red."""
    fake = FakeSandbox()
    fake.dev_ready = True
    fake.push_dev_logs("⨯ unhandledRejection Error: from before the fix")
    fake.changed_since_watermark = True  # the agent wrote: the marker may be stale
    fake.warm_status = None  # …and the fresh pass cannot reach the app either

    outcome, _ = await _verify(fake, log_cursor=0, max_polls=2, indeterminate_retries=2)

    assert outcome.state is HealthState.INDETERMINATE
    assert outcome.error is None, "no defect was established, so none is reported"
    # One pass, one re-check, then the patience budget — never a verdict on the first silence.
    assert fake.command_calls.count(["npx", "tsc", "--noEmit"]) == 4


async def test_the_re_check_does_not_carry_a_browser_crash_out_of_the_verdict() -> None:
    """★ A FALSE GREEN THE RE-CHECK WOULD OTHERWISE CREATE: `drain_client_errors` is destructive,
    so a pass that goes UNHEALTHY on a dev-log crash can still have consumed a real browser crash
    from the queue — if the re-check drains again it finds nothing and calls the app healthy, and
    the crash disappears between two looks at the same app. "A report counts against exactly one
    verdict" must hold for `verify`'s ANSWER, never for each attempt at it.

    Mutation check: drop `carried_reports` from the drain and this goes green — verified."""
    fake = FakeSandbox()
    fake.dev_ready = True
    fake.push_dev_logs("⨯ unhandledRejection Error: printed before the agent's edit")
    fake.changed_since_watermark = True  # so the re-check fires
    park_client_error(
        fake.handle().app_name,
        source="window.onerror",
        title="Cannot read properties of undefined (reading 'map')",
        stack="at RecordsTable (app/records/page.tsx:41:19)",
    )

    outcome, _ = await _verify(fake, log_cursor=0, max_polls=3)

    assert fake.command_calls.count(["npx", "tsc", "--noEmit"]) == 2, "the re-check really ran"
    assert outcome.state is HealthState.UNHEALTHY, "the browser crash survived the re-check"
    assert outcome.error is not None and outcome.error.source is ErrorSource.CLIENT
