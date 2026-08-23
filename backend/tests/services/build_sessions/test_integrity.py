"""The workspace-integrity probes: the baseline-identity check and the agent watermark (U6, U9).

WHY A FILE OF ITS OWN. Both probes are one exec plus a pure parse, and the parse is where all the
edge cases live — a root commit that is not the seeded template, a file the baseline never held, a
pipeline whose exit status lies. Reached only through `verify`, each of those needs a whole health
verdict built around it to observe, which is how they came to be untested in the first place.
"""

from __future__ import annotations

import uuid

import pytest

from src.core.integrity_types import BaselineIdentity
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
from src.services.storage import StorageError, recovery_key
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
    """★ THE PIN THAT MAKES THE WHOLE CHECK MEAN ANYTHING.

    The probe accepts a root commit as the golden template only when its subject matches. That
    literal is written in `services/sandbox/client.py`, which this module deliberately does not
    import — the worker has to be able to load this without dragging the sandbox client in — so
    the two copies are kept honest here instead of by the type system.

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
    """The baseline held the file and the tree does not. That is provably NOT the starter page,
    which is the only question asked here — whether an app with no root route is healthy is the
    SERVING half's business, and it will answer 404."""
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
    """★ Never UNHEALTHY and never HEALTHY. An app cannot be convicted of showing the template by
    a check that could not find the template, and it cannot be cleared by one either.

    The last case is the one that matters most and the one that is easiest to miss. The provision
    -time `git init` is BEST-EFFORT — it logs and carries on when it fails — and the documented
    fallback creates the repository at the END of a turn, so its root commit holds the FINISHED
    APP. Accepting that root would find `app/page.tsx` identical forever and the app would be
    permanently, irreversibly accused of serving the starter page: a completion claim that can
    never be earned again, which is worse than the false claim this check exists to stop."""
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
# the watermark (U9)
# =============================================================================


async def test_a_watermark_that_was_never_laid_down_reads_as_cannot_tell() -> None:
    """★ `None`, never `False`, and the difference is a guard that turns itself off silently.

    A shell pipeline reports the status of its LAST command, and `head` exits 0 on empty input
    whatever `find` did — so without the explicit marker test, a container whose stamp failed (or
    whose `/tmp` was cleared by a restart) answered "nothing changed" at exit 0. The caller reads
    that as "do not re-check", which is exactly backwards: the moment the container is misbehaving
    is the moment the re-check should not quietly stop happening.

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
    """★ `next dev` regenerates `next-env.d.ts` and normalises `tsconfig.json` on every boot —
    that is why `_FRAMEWORK_CHURN` exists at all. Left in, "the agent changed something" is true
    on essentially every pass whether it did or not, and a watermark that is always true is not a
    watermark: U9's re-check would fire on every red verdict rather than on the stale ones.

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


async def test_storage_unconfigured_is_a_confirmed_absent() -> None:
    """A fact about the DEPLOYMENT, not about anybody's work: with no store there can be no
    recovery copy for anyone, so the content check is skipped rather than run against a fiction."""
    assert await has_ever_been_built(_APP) is False


async def test_a_recovery_copy_means_a_turn_has_done_real_work(fake_storage) -> None:
    assert await has_ever_been_built(_APP) is False  # liveness: the absent case is the default
    await fake_storage.put(recovery_key(_APP), b"a bundle")
    assert await has_ever_been_built(_APP) is True


async def test_an_unreadable_store_fails_closed_toward_checking(
    fake_storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ THE TWO "NO"s FAIL IN OPPOSITE DIRECTIONS, on purpose.

    An unreadable store is a transient blip, and this plan exists because a completion claim
    appeared over an untouched template. The worst case of checking an app that turns out to be
    brand-new is one honest sentence saying it is still the starter page; the worst case of NOT
    checking is the 2026-08-18 lie, shipped again, during an outage nobody would connect it to.

    Mutation check: return False from the `StorageError` arm and this goes red."""

    async def blows_up(_key: str) -> object:
        raise StorageError("the store would not answer")

    monkeypatch.setattr(fake_storage, "head", blows_up)
    assert await has_ever_been_built(_APP) is True
