"""The workspace-integrity probes: the baseline-identity check and the agent watermark.

WHY A FILE OF ITS OWN. Both probes are one exec plus a pure parse, and the parse is where all the
edge cases live — a root commit that is not the seeded template, a file the baseline never held, a
pipeline whose exit status lies. Reached only through `verify`, each of those needs a whole health
verdict built around it to observe, which is how they came to be untested in the first place.
"""

from __future__ import annotations

import uuid

import pytest

from src.core.integrity_types import BaselineIdentity
from src.db.models.harness_counter import HarnessCounter
from src.services.build_sessions.counters import count
from src.services.build_sessions.integrity import (
    _CHANGED_SINCE_GUARDED,
    BASELINE_COMMIT_SUBJECT,
    BASELINE_PATH,
    anything_changed_since_the_watermark,
    baseline_identity,
    has_ever_been_built,
    parse_baseline_identity,
    stamp_the_watermark,
)
from src.services.sandbox import SandboxError
from tests.services.orchestrator.fake_sandbox import (
    BASELINE_DIVERGED_STDOUT,
    BASELINE_ROOT_SHA,
    BASELINE_TEMPLATE_BLOB,
    BASELINE_UNTOUCHED_STDOUT,
    SEEDED_SUBJECT,
    FakeSandbox,
)

_APP = uuid.UUID("0198f2c0-0000-7000-8000-0000000017e6")


def _stdout(roots: str, baseline: str, working: str, subject: str = SEEDED_SUBJECT) -> str:
    return f"{roots}@@{baseline}@@{working}@@{subject}"


# =============================================================================
# parse_baseline_identity — the pure half
# =============================================================================


def test_the_seeded_subject_matches_what_the_sandbox_client_actually_commits() -> None:
    """★ THE PIN THAT MAKES THE WHOLE CHECK MEAN ANYTHING: this module deliberately does not
    import `services/sandbox/client.py` (the worker must load it without dragging the sandbox
    client in), so the two copies of the golden-template subject are kept honest here instead
    of by the type system.
    Mutation check: change either literal and this goes red."""
    from src.services.sandbox.client import _INIT_REPO_SCRIPT

    assert f"'{BASELINE_COMMIT_SUBJECT}'" in _INIT_REPO_SCRIPT


def test_an_untouched_root_route_is_the_baseline() -> None:
    assert (
        parse_baseline_identity(BASELINE_UNTOUCHED_STDOUT) is BaselineIdentity.STILL_THE_BASELINE
    )


def test_a_rewritten_root_route_has_diverged() -> None:
    assert parse_baseline_identity(BASELINE_DIVERGED_STDOUT) is BaselineIdentity.DIVERGED


def test_a_root_route_the_agent_deleted_has_diverged_not_gone_unanswerable() -> None:
    """The baseline held the file and the tree does not — provably NOT the starter page. Whether
    an app with no root route is healthy is the SERVING half's business (it will answer 404),
    not this one's."""
    assert (
        parse_baseline_identity(_stdout(BASELINE_ROOT_SHA, BASELINE_TEMPLATE_BLOB, ""))
        is BaselineIdentity.DIVERGED
    )


@pytest.mark.parametrize(
    ("stdout", "why"),
    [
        (_stdout("", "", ""), "no root commit at all — no repository, or an unreadable one"),
        (
            _stdout(f"{'a' * 40}\n{'b' * 40}", "c" * 40, "d" * 40),
            "two root commits: no single birth certificate to compare against",
        ),
        (
            _stdout(BASELINE_ROOT_SHA, "", "d" * 40),
            "the root commit exists and never held this file",
        ),
        ("", "unparseable output"),
        ("only-one-field", "a truncated body, with the later fields simply absent"),
        (
            _stdout(BASELINE_ROOT_SHA, BASELINE_TEMPLATE_BLOB, BASELINE_TEMPLATE_BLOB, "wip"),
            "the root commit is NOT the seeded template",
        ),
    ],
)
def test_everything_unanswerable_is_unanswerable(stdout: str, why: str) -> None:
    """★ Never UNHEALTHY and never HEALTHY: an app cannot be convicted of showing the template
    by a check that could not find the template, nor cleared by one either.

    The last case is easiest to miss: provision-time `git init` is BEST-EFFORT, and its
    documented fallback creates the repository at the END of a turn, so its root commit could
    hold the FINISHED APP. Accepting that root would find `app/page.tsx` identical forever and
    the app permanently, irreversibly accused of serving the starter page."""
    assert parse_baseline_identity(stdout) is BaselineIdentity.UNANSWERABLE, why


# =============================================================================
# baseline_identity — the one exec around it
# =============================================================================


async def test_a_probe_that_cannot_run_is_unanswerable_not_a_verdict() -> None:
    fake = FakeSandbox()
    fake.probes_fail = True  # a non-zero exit from the script itself
    assert await baseline_identity(fake, fake.handle()) is BaselineIdentity.UNANSWERABLE


async def test_a_probe_that_raises_is_unanswerable_and_never_escapes() -> None:
    """A probe that could throw would fail a build for a supervisor blip."""
    fake = FakeSandbox()
    fake.probe_error = SandboxError("supervisor blip")
    assert await baseline_identity(fake, fake.handle()) is BaselineIdentity.UNANSWERABLE


async def test_the_probe_asks_about_the_root_route_and_nothing_else() -> None:
    fake = FakeSandbox()
    await baseline_identity(fake, fake.handle())
    script = fake.command_calls[-1][2]
    assert BASELINE_PATH in script
    assert "rev-list --max-parents=0" in script


# =============================================================================
# the watermark
# =============================================================================


async def test_a_watermark_that_was_never_laid_down_reads_as_cannot_tell() -> None:
    """★ `None`, never `False` — the difference is a guard that turns itself off silently. A
    shell pipeline reports the status of its LAST command, and `head` exits 0 on empty input
    whatever `find` did, so without the explicit marker a container whose stamp failed answers
    "nothing changed" at exit 0 — exactly backwards, since that's the moment the re-check
    should not quietly stop happening.
    Mutation check: drop the `[ -f … ] || exit 1` guard and this goes red."""
    fake = FakeSandbox()
    assert await anything_changed_since_the_watermark(fake, fake.handle()) is False  # liveness
    fake.watermark_marker_missing = True
    assert await anything_changed_since_the_watermark(fake, fake.handle()) is None


def test_the_watermark_question_refuses_to_run_without_its_marker() -> None:
    """The guard itself, read off the composed command rather than inferred from behaviour."""
    assert _CHANGED_SINCE_GUARDED.startswith("[ -f ")
    assert "|| exit 1" in _CHANGED_SINCE_GUARDED


def test_the_watermark_ignores_the_files_the_toolchain_rewrites_by_itself() -> None:
    """`next dev` regenerates `next-env.d.ts` and normalises `tsconfig.json` on every boot — left
    in, "the agent changed something" is true on essentially every pass whether it did or not,
    and a watermark that is always true fires the re-check on every verdict, not the stale ones.
    Mutation check: remove either prune and this goes red."""
    assert "-name next-env.d.ts -prune" in _CHANGED_SINCE_GUARDED
    assert "-name tsconfig.json -prune" in _CHANGED_SINCE_GUARDED
    for heavy in ("node_modules", ".next", ".git"):
        assert f"-name {heavy}" in _CHANGED_SINCE_GUARDED


async def test_stamping_reports_whether_it_landed() -> None:
    fake = FakeSandbox()
    assert await stamp_the_watermark(fake, fake.handle()) is True
    fake.probes_fail = True
    assert await stamp_the_watermark(fake, fake.handle()) is False


# =============================================================================
# has_ever_been_built — the gate on the content check
# =============================================================================


async def test_a_project_nobody_has_built_in_is_a_confirmed_absent(
    empty_harness_counts: None,
) -> None:
    """A brand-new project is SUPPOSED to be showing the starter template, so asking the content
    question about one would manufacture an accusation."""
    assert await has_ever_been_built(_APP) is False


async def test_a_turn_that_wrote_the_workspace_means_it_has_been_built(
    empty_harness_counts: None,
) -> None:
    """★ THE COUNTER ROW IS THE SOURCE, and it has to be: a container that factory-resets loses
    everything it could be asked, and the row survives — which is exactly when the content check
    most needs to run.
    Mutation check: answer this from the container or the store and a reverted app stops being
    checked."""
    assert await has_ever_been_built(_APP) is False  # liveness: the absent case is the default
    await count(HarnessCounter.WORKSPACE_WAS_WRITTEN, app_id=_APP)
    assert await has_ever_been_built(_APP) is True


async def test_a_row_for_another_app_is_never_read_as_this_ones(
    empty_harness_counts: None,
) -> None:
    """The table is deployment-wide, so a predicate that dropped `app_id` would report every app
    as built the moment anybody built anything."""
    await count(HarnessCounter.WORKSPACE_WAS_WRITTEN, app_id=uuid.uuid4())

    assert await has_ever_been_built(_APP) is False


async def test_a_database_that_will_not_answer_fails_closed_toward_checking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ THE TWO "NO"s FAIL IN OPPOSITE DIRECTIONS, on purpose: the worst case of checking an app
    that turns out to be brand-new is one honest sentence saying it is still the starter page;
    the worst case of NOT checking is a completion claim shipped over an untouched template,
    during an outage nobody would connect it to.
    Mutation check: return False from the `except` arm and this goes red."""
    import src.db.base as db_base

    def explode() -> object:
        raise RuntimeError("the session factory itself is broken")

    monkeypatch.setattr(db_base, "async_session_factory", explode)

    assert await has_ever_been_built(_APP) is True
