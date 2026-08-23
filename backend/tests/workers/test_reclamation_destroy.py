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

import base64
import datetime as dt
import uuid

import pytest
import redis.asyncio as aioredis
import structlog.testing

from src.services.build_sessions import pass_history
from src.services.build_sessions.destroy import (
    DESTROY_CEILING,
    PASS_SKIPPED_LOCKED_EVENT,
    destroy_candidates,
    may_destroy_on_this_control_plane,
    staging_tags,
)
from src.services.build_sessions.pass_history import CopyAttempt
from src.services.build_sessions.reclaim import ContainerVerdict, RegistryClaim, Tier, Verdict
from src.services.sandbox import SandboxError
from src.services.sandbox.base import TAG_RECLAIM_STAGED_AT, ExecResult, SandboxHandle
from tests.fakes import FakeSandboxClient, FakeStorage, a_git_bundle, a_sandbox_name

STAGED = {TAG_RECLAIM_STAGED_AT: dt.datetime(2026, 8, 11, tzinfo=dt.UTC).isoformat()}


@pytest.fixture(autouse=True)
def copy_attempts(monkeypatch: pytest.MonkeyPatch) -> list[CopyAttempt]:
    """Every U5 copy-before-reclaim outcome this test recorded, WITHOUT touching the database.

    AUTOUSE for the same reason as in the reaper's own suite: `record_durable_copy_attempt` opens
    its own session and COMMITS, so any test here that reaches a real reap would leave a permanent
    row in the SHARED test database — and `test_reclamation_report_only.py` counts every row in
    that table. The real writer is exercised in `test_durable_copy_gate.py`, against a connection
    that rolls back."""
    recorded: list[CopyAttempt] = []

    async def _spy(attempt: CopyAttempt) -> None:
        recorded.append(attempt)

    monkeypatch.setattr(pass_history, "record_durable_copy_attempt", _spy)
    return recorded


#: Every signal lapsed — the shape the classifier judged these containers in.
UNCLAIMED = RegistryClaim(
    lock_held=False, heartbeat_alive=False, stay_current=False, lease_held=False
)


def _candidate(name: str, *, verdict: Verdict = Verdict.DESTROY) -> ContainerVerdict:
    return ContainerVerdict(name, Tier.HIGH_CONFIDENCE, verdict, "staged and idle")


class _Arm:
    """A fake ARM that records teardowns and can be told what re-validation sees.

    `claims` is the SECOND re-validation input: what the coordination store says about a container
    at delete time, which is a different question from what its tags say and is answered by a
    different subsystem."""

    def __init__(
        self,
        tags: dict[str, dict[str, str] | None] | None = None,
        *,
        claims: dict[str, RegistryClaim] | None = None,
        refuses: tuple[str, ...] = (),
    ) -> None:
        self.torn_down: list[str] = []
        self.tags = tags or {}
        self.claims = claims or {}
        self.refuses = frozenset(refuses)

    async def revalidate(self, name: str) -> dict[str, str] | None:
        return self.tags.get(name, STAGED)

    async def claim_now(self, name: str) -> RegistryClaim | None:
        return self.claims.get(name)

    async def teardown(self, name: str) -> bool:
        if name in self.refuses:
            return False  # the durable-copy gate spared it, or ARM would not delete it
        self.torn_down.append(name)
        return True


async def _destroy(arm: _Arm, names: list[str], *, environment: str = "production"):
    return await destroy_candidates(
        tuple(_candidate(n) for n in names),
        revalidate=arm.revalidate,
        claim_now=arm.claim_now,
        teardown=arm.teardown,
        environment=environment,
    )


# --- the dev allowlist ------------------------------------------------------------


@pytest.mark.parametrize("environment", ["development", "staging"])
async def test_nothing_is_ever_destroyed_off_production(environment: str) -> None:
    """THE STANDING DIRECTIVE, enforced in code rather than by remembering.

    The dev subscription is a test bed holding containers people are actively using to validate
    this very feature — including the twenty-day-old orphan the plan names by hand. Deleting one
    because a classifier said so would destroy the evidence. `reclaim_destroy` is a flag an
    operator can flip anywhere; this is what makes flipping it in development harmless.

    Mutation-check: drop the `may_destroy_on_this_control_plane` guard and this goes red while
    every other test in the file stays green."""
    arm = _Arm()

    outcome = await _destroy(arm, ["sbx-a", "sbx-b"], environment=environment)

    assert arm.torn_down == []
    assert outcome.destroyed == ()
    assert outcome.remaining == 2


def test_only_production_may_destroy() -> None:
    assert may_destroy_on_this_control_plane("production") is True
    assert may_destroy_on_this_control_plane("development") is False
    assert may_destroy_on_this_control_plane("staging") is False


# --- the happy path ---------------------------------------------------------------


async def test_a_staged_unclaimed_container_is_destroyed() -> None:
    """*Covers AE1.* Everything concurred, it was staged on an earlier pass, and it goes."""
    arm = _Arm()

    outcome = await _destroy(arm, ["sbx-ghost"])

    assert arm.torn_down == ["sbx-ghost"]
    assert outcome.destroyed == ("sbx-ghost",)
    assert outcome.remaining == 0


# --- re-validation ----------------------------------------------------------------


async def test_a_container_that_lost_its_staging_tag_is_not_destroyed() -> None:
    """THE RACE THAT MATTERS. `app_name_for(app_id)` is deterministic, so the name this pass is
    about to delete is the name the builder's next start provisions into. A fresh container at
    that name has no staging tag — anything that rewrote the tags since the snapshot means this
    is no longer the container we judged.

    Destroying it would take the NEW container, and the trailing registry clear would then wipe
    the record of a container that no longer exists — manufacturing exactly the orphan class this
    whole system was built to collect."""
    arm = _Arm({"sbx-restarted": {}})  # re-provisioned: identity present, staging tag gone

    outcome = await _destroy(arm, ["sbx-restarted"])

    assert arm.torn_down == []
    assert outcome.aborted == ("sbx-restarted",)


async def test_a_container_arm_says_is_already_gone_is_not_an_abort() -> None:
    """Absent is the outcome we wanted, not a change of mind. A second delete of an absent
    resource is a 204 no-op, and the ordered teardown still has Redis state to clear."""
    arm = _Arm({"sbx-vanished": None})

    outcome = await _destroy(arm, ["sbx-vanished"])

    assert arm.torn_down == ["sbx-vanished"]
    assert outcome.aborted == ()


async def test_revalidation_happens_per_container_not_once_per_pass() -> None:
    """The window is between enumeration and EACH delete, so one check at the top of the pass
    would leave every subsequent container racing."""
    seen: list[str] = []
    arm = _Arm()

    async def _watching(name: str) -> dict[str, str]:
        seen.append(name)
        return STAGED

    await destroy_candidates(
        tuple(_candidate(n) for n in ["sbx-a", "sbx-b", "sbx-c"]),
        revalidate=_watching,
        claim_now=arm.claim_now,
        teardown=arm.teardown,
        environment="production",
    )

    assert seen == ["sbx-a", "sbx-b", "sbx-c"]


# --- re-validating the CLAIM, not only the tags -----------------------------------


async def test_a_builder_who_came_back_is_spared_even_though_the_tags_never_changed() -> None:
    """THE HOLE A TAG RE-READ CANNOT SEE, and it is not a corner case — it is the intended way a
    staged container gets away.

    A staged container stays fully attachable on purpose: `attach_existing` refuses anything
    reading `ending`, so a citizen coming back has to be able to reach it, and coming back is
    exactly what should spare it. But coming back writes a LOCK, a heartbeat, a stay or an R10
    lease — none of which are ARM tags. So between enumeration and this delete the classifier's
    verdict can go stale in the one direction that costs somebody their work, with every tag
    still saying precisely what it said when we judged it, and the tag re-read waving it through.

    Mutation-check: drop the `_somebody_came_back` call from the destroy loop and this goes red
    while every other test in this file stays green."""
    resumed = RegistryClaim(
        lock_held=True, heartbeat_alive=True, stay_current=False, lease_held=False
    )
    arm = _Arm(claims={"sbx-resumed": resumed})

    outcome = await _destroy(arm, ["sbx-resumed"])

    assert arm.torn_down == []
    assert outcome.aborted == ("sbx-resumed",)
    assert outcome.destroyed == ()


async def test_a_claim_whose_every_signal_has_lapsed_does_not_spare_anything() -> None:
    """The check is `spares_the_container`, NOT "is there a registry record". Registration alone
    sparing a container would disable essentially all reclamation — a pardoned-then-abandoned
    sandbox keeps its entry forever, which is most of the population this system collects."""
    arm = _Arm(claims={"sbx-lapsed": UNCLAIMED})

    outcome = await _destroy(arm, ["sbx-lapsed"])

    assert arm.torn_down == ["sbx-lapsed"]
    assert outcome.aborted == ()


async def test_the_claim_is_re_read_per_container_not_once_per_pass() -> None:
    """Same window, same argument as the tag re-read: a single check at the top of the pass leaves
    every container after the first racing a builder who came back while we walked the list."""
    seen: list[str] = []
    arm = _Arm()

    async def _watching(name: str) -> RegistryClaim | None:
        seen.append(name)
        return None

    await destroy_candidates(
        tuple(_candidate(n) for n in ["sbx-a", "sbx-b", "sbx-c"]),
        revalidate=arm.revalidate,
        claim_now=_watching,
        teardown=arm.teardown,
        environment="production",
    )

    assert seen == ["sbx-a", "sbx-b", "sbx-c"]


# --- only a CONFIRMED deletion counts ---------------------------------------------


async def test_a_teardown_that_declined_is_not_counted_as_a_destruction() -> None:
    """ "I asked" and "it is gone" are different observations, and the pass record is the thing an
    operator reads to decide whether reclamation is working. A teardown declines for real reasons
    — the durable-copy gate sparing a container whose work is not preserved, an ARM delete that
    would not take — and counting those as destructions reports a fleet shrinking while it grows.

    Mutation-check: append to `destroyed` unconditionally instead of on the teardown's return and
    this goes red."""
    arm = _Arm(refuses=("sbx-spared",))

    outcome = await _destroy(arm, ["sbx-spared", "sbx-doomed"])

    assert outcome.destroyed == ("sbx-doomed",)
    assert outcome.refused == ("sbx-spared",)
    assert arm.torn_down == ["sbx-doomed"]


# --- the ceiling ------------------------------------------------------------------


async def test_the_pass_stops_at_the_ceiling_and_reports_the_remainder() -> None:
    """*Covers AE12.* A bounded blast radius AND a bounded runtime: ACA sends SIGTERM with a ~30s
    grace, and `asyncio.wait(..., timeout=)` does not cancel on timeout — so a pass that overran
    would be killed mid-flight holding whatever it held.

    The remainder is REPORTED, not dropped. An operator seeing a remainder every pass is seeing a
    fleet growing faster than it is reclaimed, which is a different problem from a quiet one."""
    names = [f"sbx-{i}" for i in range(DESTROY_CEILING + 3)]
    arm = _Arm()

    outcome = await _destroy(arm, names)

    assert len(arm.torn_down) == DESTROY_CEILING
    assert outcome.remaining == 3


# --- single-flight ----------------------------------------------------------------


async def test_a_second_concurrent_pass_destroys_nothing() -> None:
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
                outcome = await _destroy(arm, ["sbx-a"])
        finally:
            await holder.execute(sa.select(sa.func.pg_advisory_unlock(_PASS_LOCK_KEY)))

    assert arm.torn_down == []
    assert outcome.skipped_locked is True
    assert any(entry.get("event") == PASS_SKIPPED_LOCKED_EVENT for entry in logs)


async def test_the_lock_does_not_ride_the_application_pool() -> None:
    """A SESSION-SCOPED LOCK ON A POOLED CONNECTION IS NOT SINGLE-FLIGHT.

    `pg_try_advisory_lock` lives on the connection that took it. On the shared pool that gives two
    silent failures: a session that releases its connection mid-pass drops the lock while the
    destroy loop keeps deleting in the belief it is alone, and a process that dies holding it
    leaves the lock on a pooled connection that blocks every later pass until the pool recycles.

    `NullPool` makes the connection's lifetime exactly the pass — Postgres frees a session lock
    when the session ends, so even a hard crash releases it. Asserted structurally because the
    failure it prevents cannot be provoked in a unit test: it needs a pool under contention and a
    caller that commits in the middle of a walk, which is a future caller's mistake, not today's.

    Mutation-check: drop `poolclass=NullPool`, or take the lock on the caller's session again,
    and this goes red."""
    from sqlalchemy.pool import NullPool

    from src.db.base import engine as application_engine
    from src.services.build_sessions.destroy import _the_lock_engine

    lock_engine = _the_lock_engine()

    assert lock_engine is not application_engine, "the lock must not share the request pool"
    assert isinstance(lock_engine.pool, NullPool)
    assert lock_engine.dialect.name == application_engine.dialect.name
    # Built once and reused: a fresh engine per pass would leak connectors on every tick.
    assert _the_lock_engine() is lock_engine


async def test_the_lock_is_released_even_when_a_teardown_raises() -> None:
    """A wedged pass holding the lock forever would stop reclamation silently — the failure mode
    the `skipped_locked` event exists to make visible, and one worth not causing."""

    async def _boom(name: str) -> bool:
        raise RuntimeError("ARM said no")

    with pytest.raises(RuntimeError):
        await destroy_candidates(
            (_candidate("sbx-a"),),
            revalidate=_Arm().revalidate,
            claim_now=_Arm().claim_now,
            teardown=_boom,
            environment="production",
        )

    # The lock is free again: a fresh pass can take it.
    arm = _Arm()
    outcome = await _destroy(arm, ["sbx-b"])
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

    def __init__(
        self, environment: str, *, destroy: bool = True, reclaim: bool = True, sweep: bool = True
    ) -> None:
        self.ENVIRONMENT = environment
        self.sandbox = _SandboxFlags(destroy=destroy, reclaim=reclaim, sweep=sweep)
        # The scheduled sweep's off-duty check reads this before anything else; a `None` here
        # would answer "unconfigured" and hide whatever the test was actually asking about.
        self.redis = object()


class _SandboxFlags:
    """`destroy` is a parameter because ONE of these flags gates the destroy arm and the other
    half of this unit must go on working with it off — a report-only deployment still has to
    stamp the staging tag, or a destroy verdict is never reachable in the first place.

    `reclaim` and `sweep` are separate parameters because they gate DIFFERENT WORKERS, and the
    whole point of `sweep_enabled` is that the pre-existing sweep keeps running on a deployment
    that has never switched the new reclamation pass on."""

    def __init__(self, *, destroy: bool = True, reclaim: bool = True, sweep: bool = True) -> None:
        self.reclaim_enabled = reclaim
        self.reclaim_destroy = destroy
        self.sweep_enabled = sweep
        # Read by `_threshold()` on the full-task path; irrelevant to the arm-level tests above.
        self.reclaim_fleet_alarm_threshold = 25


def _report(  # noqa: ANN201
    name: str,
    owners: dict[str, tuple[uuid.UUID, uuid.UUID]],
    *,
    verdict: Verdict = Verdict.DESTROY,
):
    from src.services.build_sessions import reclamation_pass as pass_mod

    return pass_mod.PassReport(
        scanned=1, spared=0,
        staged=1 if verdict is Verdict.STAGE else 0,
        destroy=1 if verdict is Verdict.DESTROY else 0,
        escalate=0, not_ours=0, store_fault=False,
        candidates=(_candidate(name, verdict=verdict),), owners=owners,
    )  # fmt: skip


class _Destroyer:
    """A control plane that can list, stamp and re-read tags — the full `FleetDestroyer` shape,
    recording every stamp so the staging arm is observable rather than merely un-crashed."""

    def __init__(self) -> None:
        self.stamped: list[tuple[str, dict[str, str]]] = []

    async def list_sandbox_fleet(self):  # noqa: ANN201
        return []

    async def get_app_tags(self, *, name: str) -> dict[str, str]:
        return STAGED

    async def stamp_tags(self, *, name: str, tags: dict[str, str]) -> None:
        self.stamped.append((name, dict(tags)))


async def test_the_janitor_destroys_the_container_it_judged_and_gates_it_on_app_id(
    monkeypatch: pytest.MonkeyPatch, fake_redis: aioredis.Redis
) -> None:
    """THE ASSERTIONS THAT LIVE ON THIS SEAM AND NOWHERE ELSE — and there are two of them.

    FIRST, THE REAP IS KEYED BY CONTAINER NAME. The pass judged `sbx-doomed`; a reap keyed by USER
    reads that user's registry and destroys whatever it names *now*, which after a fresh start is
    a different and very much alive container — and for the unregistered orphans this feature
    exists to collect it is nothing at all, while the pass counts them destroyed. Neither failure
    is visible from inside the reaper: both are correct behaviour for the function being called.

    SECOND, THE DURABLE-COPY GATE IS OPT-IN via `app_id`. Callers reaping a user's own stale state
    may pass nothing — a builder is standing right there, about to be handed a fresh container.
    The janitor is the caller with no human watching it, so it must pass the id.

    Mutation-check: key `_teardown` off the registry (call `reap_user`) and the name assertion
    goes red; drop `app_id=app_id` and the id assertion does — while every test in
    `test_durable_copy_gate.py` stays green, which is the whole reason both live here."""
    from src.workers import reclamation

    user_id, app_id = uuid.uuid4(), uuid.uuid4()
    seen: list[tuple[str, uuid.UUID, uuid.UUID]] = []

    async def _spy_reap(redis, client, *, app_name, user_uuid, app_id):  # noqa: ANN001
        seen.append((app_name, user_uuid, app_id))
        return True

    monkeypatch.setattr(
        "src.services.build_sessions.reaper.reap_the_container_we_judged", _spy_reap
    )
    monkeypatch.setattr("src.services.sandbox.get_sandbox", lambda: _Destroyer())
    monkeypatch.setattr(reclamation, "settings", _Settings("production"))

    destroyed = await reclamation._destroy_the_confirmed(
        _report("sbx-doomed", {"sbx-doomed": (user_id, app_id)})
    )

    assert destroyed == 1
    assert seen == [("sbx-doomed", user_id, app_id)], (
        "the janitor must destroy the container it JUDGED and must pass app_id — the reap is "
        "keyed by user unless told otherwise, and the U14 gate is opt-in"
    )


async def test_a_claim_held_by_anyone_at_all_aborts_the_destroy(
    monkeypatch: pytest.MonkeyPatch, fake_redis: aioredis.Redis
) -> None:
    """THE RE-READ ASKS ABOUT THE CONTAINER, NOT ABOUT THE OWNER ON ITS TAGS.

    An earlier version took `user_id` from `report.owners` and read only that user's record, so
    "is this container claimed?" quietly became "does the ARM-tagged owner claim it?". Here the
    tagged owner claims nothing and a different record holds a live lock and heartbeat on the very
    same container — the narrow question answers `None` and the container is deleted out from
    under a running build. The classifier already spares this shape; the arm that re-checks it
    immediately before deleting must agree, or the second gate is weaker than the first.

    Mutation-check: read `report.owners[name]` and pass that user id to `claim_for_container`
    and this goes red, while every other test in this file stays green."""
    from src.services.redis import registry_key
    from src.services.redis.keys import REGISTRY_FIELD_APP_NAME
    from src.workers import reclamation

    tagged_owner, someone_else = uuid.uuid4(), uuid.uuid4()

    async def _never(*_a: object, **_k: object) -> bool:
        return pytest.fail("a claimed container must never reach the teardown")

    await fake_redis.hset(
        registry_key(someone_else), mapping={REGISTRY_FIELD_APP_NAME: "sbx-doomed"}
    )
    await fake_redis.set(f"bial:development:sandbox:lock:{someone_else}", "tok", ex=900)
    await fake_redis.set(f"bial:development:sandbox:heartbeat:{someone_else}", "now", ex=90)

    monkeypatch.setattr("src.services.build_sessions.reaper.reap_the_container_we_judged", _never)
    monkeypatch.setattr("src.services.sandbox.get_sandbox", lambda: _Destroyer())
    monkeypatch.setattr(reclamation, "settings", _Settings("production"))

    destroyed = await reclamation._destroy_the_confirmed(
        _report("sbx-doomed", {"sbx-doomed": (tagged_owner, uuid.uuid4())})
    )

    assert destroyed == 0


class _DestroyerYouCanAlsoBundleFrom(FakeSandboxClient):
    """A control plane that is BOTH a fleet destroyer and a container you can attach to and exec
    in — which is what `get_sandbox()` actually returns in production.

    `_Destroyer` above is neither: it answers the three fleet methods and nothing else, which is
    fine for every test that monkeypatches the reap away, and useless for the one test that must
    not. U5 writes a real bundle out of the judged container through the SAME client the janitor
    hands the reaper, so the seam is only observable against a double that can do both jobs."""

    def __init__(self, *, head: str, bundles_to: str) -> None:
        super().__init__()
        name = a_sandbox_name("doomed")
        self.attach_handle = SandboxHandle(
            fqdn=f"{name}.example",
            token="tok",
            app_name=name,
            preview_url=f"https://{name}.example/",
            ready=True,
        )
        bundle = base64.b64encode(a_git_bundle(bundles_to)).decode()

        def handler(cmd: list[str]) -> ExecResult:
            if cmd[0] == "sh" and "rev-parse" in cmd[-1]:
                answered = "0 0" if "merge-base" in cmd[-1] else ""
                return ExecResult(stdout=f"{head}@@@@4@@{answered}", stderr="", exit=0)
            if cmd[0] == "base64":
                return ExecResult(stdout=bundle, stderr="", exit=0)
            return ExecResult(stdout="", stderr="", exit=0)

        self.exec_handler = handler

    async def list_sandbox_fleet(self):  # noqa: ANN201
        return []

    async def get_app_tags(self, *, name: str) -> dict[str, str]:
        return STAGED

    async def stamp_tags(self, *, name: str, tags: dict[str, str]) -> None:
        return None


async def test_the_scheduled_janitor_takes_the_copy_before_it_deletes_anything(
    monkeypatch: pytest.MonkeyPatch,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    copy_attempts: list[CopyAttempt],
) -> None:
    """★ THE WIRING, WITH NOTHING STUBBED OUT BETWEEN THE PASS AND THE BUNDLE.

    Every other test on this seam monkeypatches `reap_the_container_we_judged`, which is right for
    what they assert — that the janitor reaps by NAME and passes an `app_id` — and is precisely
    what makes them blind to whether the reap it calls does anything with that id. ADR-0029 §7's
    promise lives inside the function they replace, and it went unkept for the entire life of the
    feature: the janitor is the caller with no human watching it, so a container whose autosave
    had failed was spared on every fifteen-minute pass, indefinitely, at full ACA cost.

    So this one runs the real reap, against a control plane that can both destroy a fleet and be
    execed in — which is what production hands it. The copy is BEHIND the container, so §7 applies:
    take one, then reclaim.

    Mutation check: put `if not verdict.may_destroy: return False` back in
    `reap_the_container_we_judged` and this goes red on `destroyed == 1` — the container is spared
    and the recovery slot still holds the older tree."""
    from src.services.redis import registry_key
    from src.services.redis.keys import REGISTRY_FIELD_APP_NAME, REGISTRY_FIELD_STATE
    from src.services.storage import recovery_key
    from src.workers import reclamation

    user_id, app_id = uuid.uuid4(), uuid.uuid4()
    doomed = a_sandbox_name("doomed")
    # An unclaimed record naming the container we are about to judge: no lock, no heartbeat, no
    # stay, no lease. Present so `attach_existing` has an address to build a handle from — the
    # copy cannot be taken from a container the registry no longer claims.
    await fake_redis.hset(
        registry_key(user_id),
        mapping={REGISTRY_FIELD_APP_NAME: doomed, REGISTRY_FIELD_STATE: "ready"},
    )
    await fake_storage.put(
        recovery_key(app_id), a_git_bundle("b" * 40), metadata={"head_sha": "b" * 40}
    )
    plane = _DestroyerYouCanAlsoBundleFrom(head="a" * 40, bundles_to="c" * 40)

    monkeypatch.setattr("src.services.sandbox.get_sandbox", lambda: plane)
    monkeypatch.setattr(reclamation, "settings", _Settings("production"))

    destroyed = await reclamation._destroy_the_confirmed(
        _report(doomed, {doomed: (user_id, app_id)})
    )

    assert destroyed == 1
    assert plane.torn_down == [doomed]
    meta = await fake_storage.head(recovery_key(app_id))
    assert meta is not None and (meta.metadata or {})["head_sha"] == "c" * 40, (
        "the janitor destroyed the container without first securing its newest work"
    )
    assert copy_attempts == [CopyAttempt.COPIED]


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


async def test_a_raise_in_the_destroy_arm_is_still_recorded_as_a_failed_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PASS THAT DIES IN ITS DESTRUCTIVE HALF MUST NOT IMPERSONATE A DEAD WORKER.

    An absent `worker_passes` row is how this system says "the scheduler is gone" — it is the
    ONLY detector of a crashlooping worker, because every other alarm this unit raises is emitted
    by the pass itself and therefore goes silent exactly when the pass does. An ARM throttle
    during the mandatory per-candidate re-validation is the foreseeable raise on this arm, and
    the destroy call sat outside the one try/except in the task: the exception escaped, the
    record was never written, and an operator would have gone hunting a dead container app while
    the worker was alive and failing in a way nothing said out loud.

    The counts gathered BEFORE the raise go into the row, so it still reports what the pass SAW;
    only `destroyed` is missing, which is honest — that is the number we do not know.

    Mutation-check: delete the try/except around `_destroy_the_confirmed` in
    `reclaim_abandoned_sandboxes` and this goes red (the raise escapes with nothing recorded)
    while every other test in this file stays green."""
    from src.services.build_sessions import reclamation_pass as pass_mod
    from src.workers import reclamation

    recorded: list[tuple[str, dict[str, int], str | None]] = []

    async def _spy_record(*, outcome: str, counts: dict[str, int], detail: str | None) -> None:
        recorded.append((outcome, dict(counts), detail))

    async def _a_pass_that_found_a_candidate():  # noqa: ANN202
        return _report("sbx-doomed", {"sbx-doomed": (uuid.uuid4(), uuid.uuid4())})

    async def _throttled(_: object) -> int:
        raise RuntimeError("ARM throttled the per-candidate re-validation read")

    monkeypatch.setattr(pass_mod, "run_reclamation_pass", _a_pass_that_found_a_candidate)
    monkeypatch.setattr(reclamation, "_destroy_the_confirmed", _throttled)
    monkeypatch.setattr(reclamation, "_record_pass", _spy_record)
    monkeypatch.setattr(reclamation, "settings", _Settings("production"))

    # The raise still PROPAGATES — the receiver logs the traceback and the next tick re-drives it.
    # Recording the failure is not the same as swallowing it, and swallowing it here would hide
    # the one signal that distinguishes a broken pass from an absent one.
    with pytest.raises(RuntimeError):
        await reclamation.reclaim_abandoned_sandboxes()

    assert [outcome for outcome, _, _ in recorded] == ["failed"], (
        "a pass that raised in the destroy arm must still leave a record"
    )
    _, counts, detail = recorded[0]
    assert counts["scanned"] == 1, "the row must still say what the pass saw before it died"
    assert "destroyed" not in counts, "we do not know how many died; do not claim a number"
    assert detail


# --- the staging arm: the tag nothing used to write --------------------------------
#
# `staging_tags` was defined, unit-tested and never called. `reclaim.py` returns STAGE for any
# candidate whose `reclaim_staged_at` is None, so with nothing writing the tag every candidate
# re-staged forever and `Verdict.DESTROY` was unreachable by construction — the destroy arm, its
# ceiling, its advisory lock and its re-validation were all guarding an input that could not occur.


async def test_a_first_sighting_is_stamped_so_the_next_pass_can_see_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE STEP THE CHAIN WAS MISSING. Two independent reads a full interval apart is the whole
    safety argument for destroying anything; the tag is how the second read learns the first one
    happened, and it is the only durable record of it — a pass keeps no memory.

    Mutation-check: delete the `stamp_tags` call from `_stage_the_candidates` and this goes red."""
    from src.workers import reclamation

    plane = _Destroyer()
    monkeypatch.setattr("src.services.sandbox.get_sandbox", lambda: plane)
    monkeypatch.setattr(reclamation, "settings", _Settings("production"))

    report = _report(
        "sbx-first", {"sbx-first": (uuid.uuid4(), uuid.uuid4())}, verdict=Verdict.STAGE
    )
    stamped = await reclamation._stage_the_candidates(report)

    assert stamped == 1
    assert [name for name, _ in plane.stamped] == ["sbx-first"]
    assert list(plane.stamped[0][1]) == [TAG_RECLAIM_STAGED_AT]


async def test_a_report_only_deployment_still_stamps_the_staging_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """STAGING IS NOT GATED ON `reclaim_destroy`, and the reason is not symmetry — it is that
    gating it there makes the destroy verdict unreachable. Every environment ships with the second
    flag off; if the tag were only written when it is on, a fleet would have to be running with
    destruction ALREADY enabled before any container could ever reach a destroy verdict, and the
    operator reading a report-only pass to decide whether to flip the flag would be reading a
    candidate list that could never advance.

    Stamping destroys nothing. A staged container stays fully attachable, and a citizen coming
    back to it is exactly what clears the tag and spares it.

    Mutation-check: gate `_stage_the_candidates` on `reclaim_destroy`, or delete its call from
    `reclaim_abandoned_sandboxes`, and this goes red."""
    from src.services.build_sessions import reclamation_pass as pass_mod
    from src.workers import reclamation

    plane = _Destroyer()
    report = _report(
        "sbx-first", {"sbx-first": (uuid.uuid4(), uuid.uuid4())}, verdict=Verdict.STAGE
    )

    async def _a_pass_that_saw_a_first_sighting():  # noqa: ANN202
        return report

    async def _noop_record(*, outcome: str, counts: dict[str, int], detail: str | None) -> None:
        return None

    monkeypatch.setattr(pass_mod, "run_reclamation_pass", _a_pass_that_saw_a_first_sighting)
    monkeypatch.setattr(reclamation, "_record_pass", _noop_record)
    monkeypatch.setattr("src.services.sandbox.get_sandbox", lambda: plane)
    # The DESTROY flag is off — the posture every environment actually ships in.
    monkeypatch.setattr(reclamation, "settings", _Settings("production", destroy=False))

    await reclamation.reclaim_abandoned_sandboxes()

    assert [name for name, _ in plane.stamped] == ["sbx-first"]


async def test_one_container_that_refuses_the_stamp_does_not_cost_the_others_theirs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A throttled or vanished container is stepped over, not raised on. The stamp is idempotent
    and the next pass retries it; aborting on the first failure would leave the fleet part-staged
    with no report of what remains — the same rule the C10 tag backfill follows."""
    from src.services.build_sessions import reclamation_pass as pass_mod
    from src.workers import reclamation

    stamped: list[str] = []

    class _Flaky(_Destroyer):
        async def stamp_tags(self, *, name: str, tags: dict[str, str]) -> None:
            if name == "sbx-refused":
                raise SandboxError("ARM throttled the PATCH")
            stamped.append(name)

    monkeypatch.setattr("src.services.sandbox.get_sandbox", lambda: _Flaky())
    monkeypatch.setattr(reclamation, "settings", _Settings("production"))

    report = pass_mod.PassReport(
        scanned=2, spared=0, staged=2, destroy=0, escalate=0, not_ours=0, store_fault=False,
        candidates=tuple(
            _candidate(n, verdict=Verdict.STAGE) for n in ("sbx-refused", "sbx-second")
        ),
        owners={},
    )  # fmt: skip

    assert await reclamation._stage_the_candidates(report) == 1
    assert stamped == ["sbx-second"]


# --- the OTHER scheduled reaper, which does almost all of the deleting -------------
#
# `sandbox_reap` ports the F1 sweep onto the worker. It ran with no `app_id` (which is exactly how
# the U14 durable-copy gate is opted out of) and behind no allowlist at all — so the path that does
# almost all of the deleting was the one path with none of this plan's protection, while the
# report-only pass above carefully guarded the rare case.


async def test_the_scheduled_sweep_deletes_nothing_off_production(
    monkeypatch: pytest.MonkeyPatch, fake_redis: aioredis.Redis
) -> None:
    """THE SAME STANDING DIRECTIVE THE JANITOR IS UNDER. The dev subscription is a test bed full
    of containers people are using to validate this very feature, and an unattended five-minute
    timer is the last thing that should be deleting from it.

    Scoped to the SCHEDULED sweep: `POST /v1/internal/reap` still sweeps anywhere (superadmin,
    audited, a human behind it), and reconcile-on-start still collects a developer's own stale
    sandbox the moment they start their next build.

    Mutation-check: drop the `may_destroy_on_this_control_plane` check from `_off_duty_because`
    and this goes red."""
    from src.workers import sandbox_reap

    swept: list[object] = []

    async def _spy_sweep(*args: object, **kwargs: object) -> object:
        swept.append(kwargs)
        raise AssertionError("the scheduled sweep must not run off production")

    async def _owning() -> dict[str, uuid.UUID]:
        return {}

    # Redis, the control plane and the owner map are all AVAILABLE here on purpose: with the
    # allowlist removed the sweep must get all the way to `sweep_all` and fail on the spy, not
    # trip over an unconfigured dependency and go green for the wrong reason.
    monkeypatch.setattr("src.services.build_sessions.reaper.sweep_all", _spy_sweep)
    monkeypatch.setattr(sandbox_reap, "_owning_app_ids", _owning)
    monkeypatch.setattr("src.services.sandbox.get_sandbox", lambda: _Destroyer())

    for environment in ("development", "staging"):
        monkeypatch.setattr(sandbox_reap, "settings", _Settings(environment))
        await sandbox_reap.reap_abandoned_sandboxes()

    assert swept == []
    monkeypatch.setattr(sandbox_reap, "settings", _Settings("production"))
    assert sandbox_reap._off_duty_because() is None


def test_the_sweep_does_not_stop_because_the_new_pass_is_switched_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE UPGRADE THAT SILENTLY STOPS REAPING.

    `sweep_all` predates this whole ADR: it ran as an unflagged `while True` in the API lifespan,
    wherever a sandbox was configured, and it does almost all of the deleting. Porting it onto
    the scheduler was meant to change WHERE it runs. Gating it on `reclaim_enabled` — which ships
    off in every environment, deliberately — changed WHETHER it runs, so deploying this release
    would have stopped reaping everywhere while every health check stayed green and the only
    symptom was the Azure bill.

    Two flags for two workers: `sweep_enabled` (on, because it is pre-existing behaviour) and
    `reclaim_enabled` (off, because the pass is new).

    Mutation-check: point `_off_duty_because` back at `reclaim_enabled` and this goes red."""
    from src.workers import sandbox_reap

    monkeypatch.setattr(sandbox_reap, "settings", _Settings("production", reclaim=False))
    assert sandbox_reap._off_duty_because() is None

    monkeypatch.setattr(sandbox_reap, "settings", _Settings("production", sweep=False))
    assert sandbox_reap._off_duty_because() == "flag_off"


async def test_the_scheduled_sweep_hands_the_owning_app_ids_to_the_gate(
    monkeypatch: pytest.MonkeyPatch, fake_redis: aioredis.Redis
) -> None:
    """WITHOUT THE MAP THE GATE IS OFF ON THIS PATH. `reap_user` only consults
    `confirm_durable_copy` when it is handed an `app_id`, and this sweep handed it nothing — so
    U14 protected the rare orphan the janitor collects and not the claimed-but-expired population,
    which is where the deletions actually happen.

    Mutation-check: drop `app_ids_by_name=await _owning_app_ids()` from the sweep call and this
    goes red (the sweep is handed `None`, which is indistinguishable from opting out)."""
    from src.services.build_sessions.reaper import SweepResult
    from src.workers import sandbox_reap

    app_id = uuid.uuid4()
    seen: dict[str, object] = {}

    async def _spy_sweep(redis, client, *, live_users, app_ids_by_name=None):  # noqa: ANN001
        seen["map"] = app_ids_by_name
        return SweepResult(reaped=0, failed=0)

    async def _owning() -> dict[str, uuid.UUID]:
        return {"sbx-x": app_id}

    monkeypatch.setattr("src.services.build_sessions.reaper.sweep_all", _spy_sweep)
    monkeypatch.setattr(sandbox_reap, "_owning_app_ids", _owning)
    monkeypatch.setattr("src.services.sandbox.get_sandbox", lambda: _Destroyer())
    monkeypatch.setattr(sandbox_reap, "settings", _Settings("production"))

    await sandbox_reap.reap_abandoned_sandboxes()

    assert seen["map"] == {"sbx-x": app_id}
