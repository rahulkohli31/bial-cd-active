"""U15 — the destructive half: single-flight, re-validated, ceilinged, allowlisted (R7/R8/R19).

This is the only file in the repo where a bug deletes somebody's work, so every protection here
is asserted by making it FAIL rather than by observing it succeed. Four independent guards, each
covering something the others do not:

* the **dev allowlist** — nothing off production is ever deleted, whatever the flags say;
* the **advisory lock** — two schedulers exist during an ACA revision roll, and both would
  otherwise delete the same candidate;
* **re-validation** — `app_name_for` is deterministic, so between enumeration and delete a
  builder's fresh start can provision into the very name this pass is about to destroy;
* the **ceiling** — a bounded blast radius, and a bounded runtime inside ACA's SIGTERM grace.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
import redis.asyncio as aioredis
import structlog.testing
from sqlalchemy.ext.asyncio import AsyncSession

from src.services.build_sessions.destroy import (
    DESTROY_CEILING,
    PASS_SKIPPED_LOCKED_EVENT,
    destroy_candidates,
    may_destroy_on_this_control_plane,
    staging_tags,
)
from src.services.build_sessions.reclaim import ContainerVerdict, Tier, Verdict
from src.services.sandbox.base import TAG_RECLAIM_STAGED_AT

STAGED = {TAG_RECLAIM_STAGED_AT: dt.datetime(2026, 8, 11, tzinfo=dt.UTC).isoformat()}


def _candidate(name: str) -> ContainerVerdict:
    return ContainerVerdict(name, Tier.HIGH_CONFIDENCE, Verdict.DESTROY, "staged and idle")


class _Arm:
    """A fake ARM that records teardowns and can be told what re-validation sees."""

    def __init__(self, tags: dict[str, dict[str, str] | None] | None = None) -> None:
        self.torn_down: list[str] = []
        self.tags = tags or {}

    async def revalidate(self, name: str) -> dict[str, str] | None:
        return self.tags.get(name, STAGED)

    async def teardown(self, name: str) -> None:
        self.torn_down.append(name)


async def _destroy(
    db: AsyncSession, arm: _Arm, names: list[str], *, environment: str = "production"
):
    return await destroy_candidates(
        tuple(_candidate(n) for n in names),
        db=db,
        revalidate=arm.revalidate,
        teardown=arm.teardown,
        environment=environment,
    )


# --- the dev allowlist ------------------------------------------------------------


@pytest.mark.parametrize("environment", ["development", "staging"])
async def test_nothing_is_ever_destroyed_off_production(
    db_session: AsyncSession, environment: str
) -> None:
    """THE STANDING DIRECTIVE, enforced in code rather than by remembering.

    The dev subscription is a test bed holding containers people are actively using to validate
    this very feature — including the twenty-day-old orphan the plan names by hand. Deleting one
    because a classifier said so would destroy the evidence. `reclaim_destroy` is a flag an
    operator can flip anywhere; this is what makes flipping it in development harmless.

    Mutation-check: drop the `may_destroy_on_this_control_plane` guard and this goes red while
    every other test in the file stays green."""
    arm = _Arm()

    outcome = await _destroy(db_session, arm, ["sbx-a", "sbx-b"], environment=environment)

    assert arm.torn_down == []
    assert outcome.destroyed == ()
    assert outcome.remaining == 2


def test_only_production_may_destroy() -> None:
    assert may_destroy_on_this_control_plane("production") is True
    assert may_destroy_on_this_control_plane("development") is False
    assert may_destroy_on_this_control_plane("staging") is False


# --- the happy path ---------------------------------------------------------------


async def test_a_staged_unclaimed_container_is_destroyed(db_session: AsyncSession) -> None:
    """*Covers AE1.* Everything concurred, it was staged on an earlier pass, and it goes."""
    arm = _Arm()

    outcome = await _destroy(db_session, arm, ["sbx-ghost"])

    assert arm.torn_down == ["sbx-ghost"]
    assert outcome.destroyed == ("sbx-ghost",)
    assert outcome.remaining == 0


# --- re-validation ----------------------------------------------------------------


async def test_a_container_that_lost_its_staging_tag_is_not_destroyed(
    db_session: AsyncSession,
) -> None:
    """THE RACE THAT MATTERS. `app_name_for(app_id)` is deterministic, so the name this pass is
    about to delete is the name the builder's next start provisions into. A fresh container at
    that name has no staging tag — anything that rewrote the tags since the snapshot means this
    is no longer the container we judged.

    Destroying it would take the NEW container, and the trailing registry clear would then wipe
    the record of a container that no longer exists — manufacturing exactly the orphan class this
    whole system was built to collect."""
    arm = _Arm({"sbx-restarted": {}})  # re-provisioned: identity present, staging tag gone

    outcome = await _destroy(db_session, arm, ["sbx-restarted"])

    assert arm.torn_down == []
    assert outcome.aborted == ("sbx-restarted",)


async def test_a_container_arm_says_is_already_gone_is_not_an_abort(
    db_session: AsyncSession,
) -> None:
    """Absent is the outcome we wanted, not a change of mind. A second delete of an absent
    resource is a 204 no-op, and the ordered teardown still has Redis state to clear."""
    arm = _Arm({"sbx-vanished": None})

    outcome = await _destroy(db_session, arm, ["sbx-vanished"])

    assert arm.torn_down == ["sbx-vanished"]
    assert outcome.aborted == ()


async def test_revalidation_happens_per_container_not_once_per_pass(
    db_session: AsyncSession,
) -> None:
    """The window is between enumeration and EACH delete, so one check at the top of the pass
    would leave every subsequent container racing."""
    seen: list[str] = []
    arm = _Arm()

    async def _watching(name: str) -> dict[str, str]:
        seen.append(name)
        return STAGED

    await destroy_candidates(
        tuple(_candidate(n) for n in ["sbx-a", "sbx-b", "sbx-c"]),
        db=db_session,
        revalidate=_watching,
        teardown=arm.teardown,
        environment="production",
    )

    assert seen == ["sbx-a", "sbx-b", "sbx-c"]


# --- the ceiling ------------------------------------------------------------------


async def test_the_pass_stops_at_the_ceiling_and_reports_the_remainder(
    db_session: AsyncSession,
) -> None:
    """*Covers AE12.* A bounded blast radius AND a bounded runtime: ACA sends SIGTERM with a ~30s
    grace, and `asyncio.wait(..., timeout=)` does not cancel on timeout — so a pass that overran
    would be killed mid-flight holding whatever it held.

    The remainder is REPORTED, not dropped. An operator seeing a remainder every pass is seeing a
    fleet growing faster than it is reclaimed, which is a different problem from a quiet one."""
    names = [f"sbx-{i}" for i in range(DESTROY_CEILING + 3)]
    arm = _Arm()

    outcome = await _destroy(db_session, arm, names)

    assert len(arm.torn_down) == DESTROY_CEILING
    assert outcome.remaining == 3


# --- single-flight ----------------------------------------------------------------


async def test_a_second_concurrent_pass_destroys_nothing(db_session: AsyncSession) -> None:
    """*Two schedulers exist during an ACA revision roll*, and both would otherwise walk the same
    candidate list. The advisory lock is Postgres, not Redis, on purpose: Redis is the store this
    entire work distrusts, its `maxmemory-policy` is unverified, and under any `allkeys-*` policy
    a lock key can be evicted mid-pass — silently undoing single-flight inside the one chain that
    deletes things.

    Simulated by taking the lock on a second session before the pass runs, which is exactly what
    the overlapping replica does."""
    import sqlalchemy as sa

    from src.db.base import async_session_factory
    from src.services.build_sessions.destroy import _PASS_LOCK_KEY

    async with async_session_factory() as holder:
        await holder.execute(sa.select(sa.func.pg_try_advisory_lock(_PASS_LOCK_KEY)))
        arm = _Arm()
        try:
            with structlog.testing.capture_logs() as logs:
                outcome = await _destroy(db_session, arm, ["sbx-a"])
        finally:
            await holder.execute(sa.select(sa.func.pg_advisory_unlock(_PASS_LOCK_KEY)))

    assert arm.torn_down == []
    assert outcome.skipped_locked is True
    assert any(entry.get("event") == PASS_SKIPPED_LOCKED_EVENT for entry in logs)


async def test_the_lock_is_released_even_when_a_teardown_raises(
    db_session: AsyncSession,
) -> None:
    """A wedged pass holding the lock forever would stop reclamation silently — the failure mode
    the `skipped_locked` event exists to make visible, and one worth not causing."""

    async def _boom(name: str) -> None:
        raise RuntimeError("ARM said no")

    with pytest.raises(RuntimeError):
        await destroy_candidates(
            (_candidate("sbx-a"),),
            db=db_session,
            revalidate=_Arm().revalidate,
            teardown=_boom,
            environment="production",
        )

    # The lock is free again: a fresh pass can take it.
    arm = _Arm()
    outcome = await _destroy(db_session, arm, ["sbx-b"])
    assert outcome.skipped_locked is False
    assert arm.torn_down == ["sbx-b"]


# --- the staging tag ---------------------------------------------------------------


def test_the_staging_tag_is_not_readable_as_ending() -> None:
    """`attach_existing` refuses a sandbox whose registry state reads `ending` BEFORE it probes,
    so a staged container that looked `ending` would be unreachable to the citizen coming back to
    it — rebuilding a known P0. A staged container must stay fully attachable; a citizen
    returning is precisely what clears the tag and spares it."""
    tags = staging_tags(dt.datetime(2026, 8, 11, tzinfo=dt.UTC))

    assert list(tags) == [TAG_RECLAIM_STAGED_AT]
    assert "ending" not in str(tags).lower()


# --- the janitor's own obligations ------------------------------------------------
#
# `destroy_candidates` above is drivable with fakes. These pin the WIRING — what the scheduled
# caller must do, which no test of the pure half can observe.


class _Settings:
    """Just enough of the settings surface: both flags on, so the only things that can stop the
    destroy arm are the ones actually under test."""

    def __init__(self, environment: str) -> None:
        self.ENVIRONMENT = environment
        self.sandbox = _SandboxFlags()


class _SandboxFlags:
    reclaim_enabled = True
    reclaim_destroy = True


def _report(name: str, owners: dict[str, tuple[uuid.UUID, uuid.UUID]]):  # noqa: ANN201
    from src.services.build_sessions import reclamation_pass as pass_mod

    return pass_mod.PassReport(
        scanned=1, spared=0, staged=0, destroy=1, escalate=0, not_ours=0,
        store_fault=False, candidates=(_candidate(name),), owners=owners,
    )  # fmt: skip


async def test_the_janitor_passes_app_id_so_the_durable_copy_gate_cannot_be_skipped(
    monkeypatch: pytest.MonkeyPatch, fake_redis: aioredis.Redis
) -> None:
    """THE ASSERTION THAT LIVES ON THIS SEAM AND NOWHERE ELSE.

    `reap_user`'s durable-copy gate is OPT-IN via `app_id`. The two in-repo callers that reap a
    user's own stale state deliberately pass nothing — a builder is standing right there, about
    to be handed a fresh container. The janitor is the caller with no human watching it, so it
    must pass the id; an ungated janitor is exactly the regression U14 exists to prevent.

    No test of `reap_user` can catch an omission here: called without an `app_id` it behaves
    correctly and always has. The defect would live at the call site.

    Mutation-check: drop `app_id=app_id` from `_teardown` and this goes red while all fourteen
    tests in `test_durable_copy_gate.py` stay green — which is the whole reason it exists."""
    from src.workers import reclamation

    user_id, app_id = uuid.uuid4(), uuid.uuid4()
    seen: list[tuple[uuid.UUID, uuid.UUID | None]] = []

    async def _spy_reap(redis, user, client, *, strict=False, app_id=None):  # noqa: ANN001
        seen.append((user, app_id))
        return True

    class _Destroyer:
        async def list_sandbox_fleet(self):  # noqa: ANN201
            return []

        async def get_app_tags(self, *, name: str) -> dict[str, str]:
            return STAGED

        async def stamp_tags(self, *, name: str, tags: dict[str, str]) -> None:
            return None

    monkeypatch.setattr("src.services.build_sessions.reaper.reap_user", _spy_reap)
    monkeypatch.setattr("src.services.sandbox.get_sandbox", lambda: _Destroyer())
    monkeypatch.setattr(reclamation, "settings", _Settings("production"))

    await reclamation._destroy_the_confirmed(
        _report("sbx-doomed", {"sbx-doomed": (user_id, app_id)})
    )

    assert seen == [(user_id, app_id)], "the janitor must pass app_id — the U14 gate is opt-in"


async def test_a_substrate_that_cannot_re_read_tags_destroys_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-validation is not optional on a destroy path, so a client that can enumerate but not
    re-read a container's tags must REFUSE rather than fall back to the enumeration snapshot.
    Acting on the snapshot is precisely the race that deletes a container a builder just
    started."""
    from src.workers import reclamation

    class _ListerOnly:
        async def list_sandbox_fleet(self):  # noqa: ANN201
            return []

    monkeypatch.setattr("src.services.sandbox.get_sandbox", lambda: _ListerOnly())
    monkeypatch.setattr(reclamation, "settings", _Settings("production"))

    report = _report("sbx-doomed", {"sbx-doomed": (uuid.uuid4(), uuid.uuid4())})
    assert await reclamation._destroy_the_confirmed(report) == 0
