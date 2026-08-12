"""The scheduled reclamation pass — report-only until somebody flips a second flag (U11).

WHAT THIS DOES TODAY: enumerates Azure (U9), reads the coordination store as a spare-list, runs the
confidence-tier classifier (U10), **stamps the staging tag on first-sighting candidates**, writes a
pass record, and logs what it would destroy. It destroys nothing unless the second flag is on, and
that flag is off in every environment.

STAGING IS NOT DESTRUCTION AND IS NOT GATED LIKE IT. The tag is how one pass tells the next that it
saw this container — the mechanism behind the two-independent-reads rule — and a container carrying
it stays fully attachable, so a citizen coming back clears it and is spared. Gating the stamp on
the destroy flag would have made `Verdict.DESTROY` unreachable by construction.

TWO FLAGS, NOT ONE. `reclaim_enabled` turns the pass on; `reclaim_destroy` lets it act. One switch
would mean the only way to learn what reclamation would do is to let it do it, and there would be
no state in which an operator reads a candidate list before agreeing to it.

THE PASS RECORD IS THE POINT OF THE OBSERVABILITY HALF. Every alarm this pass raises is emitted BY
the pass, so a crashlooping scheduler emits nothing and reads exactly like a healthy quiet fleet —
the origin incident's epistemic failure moved one layer out. The only detector of a dead worker is
the ABSENCE of a record, so a record is written on EVERY outcome, including a zero-candidate pass
and a failed one. A pass that fails every time must not look like a pass that never runs.
"""

from __future__ import annotations

import datetime as dt
from typing import Final

import structlog

from src.broker import broker
from src.config import settings
from src.services.build_sessions.reclamation_pass import PassReport

_log = structlog.get_logger()

#: The task's own name, and the `task_name` its pass records carry.
RECLAMATION_TASK_NAME: Final = "sandbox_reclamation"
#: Pinned, not minted — `LabelScheduleSource` generates a fresh id for any schedule that omits one,
#: so an unpinned id changes on every process start and the scheduler cannot dedupe across a
#: restart (the same trap `deploy_reconcile` documents).
RECLAMATION_SCHEDULE_ID: Final = "sandbox-reclamation-every-15m"

#: Every fifteen minutes. Slower than deploy-reconcile because a pass enumerates the whole fleet
#: over ARM, and because the staging interval (U10) is fifteen minutes — a cadence faster than the
#: interval would let two "independent" reads land inside one window, which is exactly the
#: independence the two-pass rule buys.
RECLAMATION_CRON: Final = "*/15 * * * *"

# --- the two event constants R20 asks an alert rule to grep for ---------------------------
#
# DISTINCT NAMES ON PURPOSE. One says "the fleet is bigger than anyone intended" and is emitted by
# a pass that ran; the other says "a pass ran at all". An alert rule keyed on the first cannot
# detect a dead worker, because a dead worker never emits it — which is the whole reason the
# second exists and why its ABSENCE is the alarm.
FLEET_THRESHOLD_EVENT: Final = "sandbox_fleet_over_threshold"
PASS_COMPLETED_EVENT: Final = "sandbox_reclamation_pass_completed"


def _off_duty_because() -> str | None:
    """Why this pass must not run, or `None` when it may.

    Reported separately because they mean different things to whoever reads the log: `unconfigured`
    is "this deployment has no ARM access at all", the ordinary local posture; `flag_off` is "it
    does, and reclamation has not been switched on" — the state every environment ships in."""
    sandbox = settings.sandbox
    if sandbox is None:
        return "unconfigured"
    if not sandbox.reclaim_enabled:
        return "flag_off"
    return None


@broker.task(
    task_name=RECLAMATION_TASK_NAME,
    schedule=[{"cron": RECLAMATION_CRON, "schedule_id": RECLAMATION_SCHEDULE_ID}],
)
async def reclaim_abandoned_sandboxes() -> None:
    """One reclamation pass. Reports; destroys nothing until U15 and a second flag.

    THE FLAG GATE COMES FIRST, before a single heavy import — the same contract
    `deploy_reconcile` set. A disabled task costs structlog, the broker and the settings profile
    and nothing else, so adding a passenger never taxes a deployment that has not turned it on.

    NOTHING IS SWALLOWED. A raise is caught by the receiver, logged with a traceback, recorded as
    a failed pass, and re-driven by the next tick. Swallowing would buy nothing and hide the one
    signal that distinguishes a broken pass from an absent one."""
    off_duty = _off_duty_because()
    if off_duty is not None:
        _log.info("sandbox_reclamation_pass_disabled", reason=off_duty)
        await _record_pass(outcome="declined", counts={}, detail=off_duty)
        return

    # Imported inside the flag gate, deliberately: the ARM SDK, the ORM and the classifier are all
    # heavy, and a deployment with reclamation off must not pay for them.
    from src.services.build_sessions.reclamation_pass import run_reclamation_pass

    try:
        report = await run_reclamation_pass()
    except Exception:
        _log.exception("sandbox_reclamation_pass_failed")
        await _record_pass(
            outcome="failed", counts={}, detail="the pass raised; see the traceback"
        )
        raise

    counts = {
        "scanned": report.scanned,
        "spared": report.spared,
        "staged": report.staged,
        "destroy_candidates": report.destroy,
        "escalate": report.escalate,
        "not_ours": report.not_ours,
    }
    if report.store_fault:
        # The classifier refused to judge. Reported as an alarm, not as a quiet zero — a pass that
        # declined and a pass that found nothing are different facts about the world.
        _log.error(
            "sandbox_reclamation_store_fault",
            detail="the coordination store accounts for too little of the live fleet",
            **counts,
        )
    if report.scanned >= _threshold():
        _log.warning(FLEET_THRESHOLD_EVENT, fleet=report.scanned, threshold=_threshold())

    for verdict in report.candidates:
        # THE EVIDENCE, not just the verdict. An operator reading this at 2am has to be able to
        # agree or disagree with the decision, which needs the tier and the reason behind it.
        _log.info(
            "sandbox_reclamation_candidate",
            app_name=verdict.name,
            tier=str(verdict.tier),
            verdict=str(verdict.verdict),
            reason=verdict.reason,
            would_destroy=settings.sandbox is not None and settings.sandbox.reclaim_destroy,
        )

    if not report.store_fault:
        # THE STAGING ARM, and it runs on `reclaim_enabled` ALONE. Stamping a tag destroys
        # nothing; what it does is let the NEXT pass know this one happened, which is the entire
        # mechanism behind the two-independent-reads rule. Gating it on `reclaim_destroy` too
        # would mean nothing ever writes the tag on a report-only deployment, `reclaim_staged_at`
        # stays `None` forever, and every candidate re-stages on every pass — `Verdict.DESTROY`
        # would be unreachable by construction and the destroy arm below would be dead code
        # nobody could tell was dead.
        try:
            counts["stamped"] = await _stage_the_candidates(report)
        except Exception:
            _log.exception("sandbox_reclamation_staging_failed")
            await _record_pass(
                outcome="failed",
                counts=counts,
                detail="the staging arm raised; see the traceback",
            )
            raise
        # THE DESTROY ARM. Everything above ran regardless of the second flag; this is the only
        # place a container is ever removed, and it is behind `reclaim_destroy` AND a
        # production-only allowlist AND a single-flight lock AND per-container re-validation AND
        # a per-pass ceiling. A store fault skips it entirely: a pass that does not trust its own
        # inputs does not get to act on them.
        try:
            counts["destroyed"] = await _destroy_the_confirmed(report)
        except Exception:
            # THE SAME CONTRACT THE PASS ABOVE KEEPS, and it has to be kept here too. An ARM
            # throttle during the mandatory per-candidate re-validation is the expected shape of
            # a raise on this arm, and an absent row is precisely how this system says "the
            # worker is dead" — so a pass that died in its destructive half would otherwise
            # impersonate a crashlooping scheduler and send an operator hunting the wrong thing.
            # The counts gathered before the raise go in: the record still says what the pass
            # SAW, and only `destroyed` is missing, which is honest — we do not know.
            _log.exception("sandbox_reclamation_destroy_failed")
            await _record_pass(
                outcome="failed",
                counts=counts,
                detail="the destroy arm raised; see the traceback",
            )
            raise

    _log.info(PASS_COMPLETED_EVENT, store_fault=report.store_fault, **counts)
    await _record_pass(
        outcome="declined" if report.store_fault else "ok",
        counts=counts,
        detail="store fault: the pass refused to judge" if report.store_fault else None,
    )


async def _stage_the_candidates(report: PassReport) -> int:
    """Stamp `bial-reclaim-staged-at` on every STAGE verdict. Returns how many took.

    NOTHING ELSE WRITES THIS TAG, and until this existed nothing did — `staging_tags` was defined,
    unit-tested and never called, so `identity.reclaim_staged_at` was `None` on every container on
    every pass. The classifier reads exactly that field to decide STAGE versus DESTROY, so the
    whole confidence chain terminated one step short: candidates were re-staged forever and the
    destroy arm, its ceiling, its lock and its re-validation had no reachable input.

    ONE CONTAINER'S REFUSED PATCH IS ONE CONTAINER'S PROBLEM. The stamp is idempotent and the next
    pass retries it, so a throttled or vanished container is logged and stepped over; aborting the
    sweep on the first failure would leave the fleet part-staged with no report of what remains —
    the same rule the C10 backfill follows for the same reason."""
    from src.services.build_sessions.destroy import staging_tags
    from src.services.build_sessions.inventory import FleetTagger
    from src.services.build_sessions.reclaim import Verdict
    from src.services.sandbox import SandboxError, get_sandbox

    staging = tuple(v for v in report.candidates if v.verdict is Verdict.STAGE)
    if not staging:
        # Before touching the control plane at all: a pass with nothing to stage must not make an
        # ARM client appear, and the report-only tests drive exactly that shape.
        return 0

    control_plane = get_sandbox()
    if not isinstance(control_plane, FleetTagger):
        # A substrate that can list but not stamp cannot participate in the two-pass rule. Loud,
        # because on such a deployment reclamation can never progress past STAGE and the silence
        # would read as a fleet that simply has nothing to collect.
        _log.error("sandbox_reclamation_staging_unsupported_substrate")
        return 0

    tags = staging_tags(dt.datetime.now(dt.UTC))
    stamped = 0
    for verdict in staging:
        try:
            await control_plane.stamp_tags(name=verdict.name, tags=tags)
        except SandboxError:
            _log.warning(
                "sandbox_reclamation_staging_stamp_failed", app_name=verdict.name, exc_info=True
            )
            continue
        stamped += 1
    return stamped


async def _destroy_the_confirmed(report: PassReport) -> int:
    """Act on the candidates the classifier confirmed. Returns how many were CONFIRMED destroyed.

    THE JANITOR PASSES `app_id`, AND THAT IS HALF THE POINT OF THIS FUNCTION EXISTING. The
    durable-copy gate is opt-in — the callers that reap a user's own stale state may pass nothing,
    because a builder standing right there is about to be handed a fresh container. This caller is
    the one with no human watching it, so it passes the id and cannot skip the gate. An ungated
    janitor is precisely the regression U14 exists to prevent, and no test of the reaper alone
    would catch it, which is why the assertion lives on this seam.

    THE OTHER HALF IS THAT IT REAPS BY CONTAINER, NOT BY USER. `reap_the_container_we_judged`
    exists because those two stopped being the same thing the moment this ran out of process: see
    its docstring for both divergences and what each one costs."""
    from src.services.build_sessions.destroy import destroy_candidates
    from src.services.build_sessions.inventory import FleetDestroyer
    from src.services.build_sessions.reaper import reap_the_container_we_judged
    from src.services.build_sessions.reclaim import RegistryClaim, Verdict
    from src.services.build_sessions.reclamation_pass import claim_for_container
    from src.services.redis import get_redis
    from src.services.sandbox import get_sandbox

    confirmed = tuple(v for v in report.candidates if v.verdict is Verdict.DESTROY)
    if not confirmed or settings.sandbox is None or not settings.sandbox.reclaim_destroy:
        return 0

    control_plane = get_sandbox()
    if not isinstance(control_plane, FleetDestroyer):
        # A substrate that can list but not re-read a container's tags cannot satisfy the
        # re-validation rule, and re-validation is not optional on a destroy path. Refusing is
        # the only safe answer: acting on the enumeration snapshot is precisely the race that
        # deletes a container a builder just started.
        _log.error("sandbox_reclamation_destroy_unsupported_substrate")
        return 0

    async def _revalidate(name: str) -> dict[str, str] | None:
        """Re-read THIS container's tags, right now. Not the enumeration snapshot — the whole
        point is that the snapshot may be stale by the time we reach this container."""
        return await control_plane.get_app_tags(name=name)

    async def _claim_now(name: str) -> RegistryClaim | None:
        """Rebuild THIS container's spare-list entry, right now. The tag re-read above cannot
        see a builder who simply came back: resuming writes a lock, a heartbeat, a stay or a
        lease, and leaves every ARM tag exactly as the classifier found it.

        Asks by CONTAINER, not by the owner the ARM tags name — the same question the classifier
        asked, so the two reads cannot disagree about what "claimed" means."""
        return await claim_for_container(get_redis(), app_name=name)

    async def _teardown(name: str) -> bool:
        """Destroy THE CONTAINER WE JUDGED — by name, never by whatever the owner's record
        currently points at. Reaping by user would delete a sandbox the builder started since
        enumeration and leave the orphan standing, and would silently no-op on the unregistered
        orphans this whole feature exists to collect while the pass counted them destroyed."""
        user_id, app_id = report.owners[name]
        return await reap_the_container_we_judged(
            get_redis(), control_plane, app_name=name, user_uuid=user_id, app_id=app_id
        )

    # NO SESSION HELD ACROSS THE PASS. The single-flight lock owns its own connection now
    # (`destroy._the_lock_engine`), so this arm no longer has to keep an application-pool session
    # open for the whole walk just to keep a lock alive on it.
    outcome = await destroy_candidates(
        confirmed,
        revalidate=_revalidate,
        claim_now=_claim_now,
        teardown=_teardown,
        environment=str(settings.ENVIRONMENT),
    )
    if outcome.remaining:
        _log.warning("sandbox_reclamation_ceiling_reached", remaining=outcome.remaining)
    if outcome.aborted:
        _log.info("sandbox_reclamation_aborted_on_revalidation", count=len(outcome.aborted))
    if outcome.refused:
        # NOT an abort and not a destruction: the teardown ran and declined. A durable-copy gate
        # sparing the same container every pass is a container whose work nothing is preserving,
        # which is a report worth reading rather than a number quietly missing from `destroyed`.
        _log.warning("sandbox_reclamation_teardown_refused", names=list(outcome.refused))
    return len(outcome.destroyed)


def _threshold() -> int:
    sandbox = settings.sandbox
    return sandbox.reclaim_fleet_alarm_threshold if sandbox else 25


async def _record_pass(*, outcome: str, counts: dict[str, int], detail: str | None) -> None:
    """Write the pass record. EVERY outcome, including the boring ones.

    A zero-candidate pass still writes, because a healthy quiet fleet and a dead worker are
    otherwise the same observation. A declined pass writes, because "reclamation is switched off"
    is a thing an operator should be able to see rather than infer from silence. A failed pass
    writes, because a pass that raises every tick leaves no `ok` row and would otherwise be
    indistinguishable from one that never ran.

    ITS OWN SESSION, not the caller's: this runs outside any request, and it must land even when
    the pass it is describing has just failed."""
    from src.db.base import async_session_factory
    from src.db.models.worker_pass import PassOutcome, WorkerPass

    try:
        async with async_session_factory() as db:
            db.add(
                WorkerPass(
                    task_name=RECLAMATION_TASK_NAME,
                    outcome=PassOutcome(outcome),
                    finished_at=dt.datetime.now(dt.UTC),
                    counts=counts,
                    detail=detail,
                )
            )
            await db.commit()
    except Exception:
        # A pass whose WORK succeeded must not be reported as failed because its bookkeeping did.
        # Loud, though: the staleness alarm reads this table, so a silent failure here would make
        # a healthy worker look dead.
        _log.exception("sandbox_reclamation_pass_record_failed", outcome=outcome)
