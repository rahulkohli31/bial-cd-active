"""The scheduled reclamation pass — report-only until somebody flips a second flag (U11).

WHAT THIS DOES TODAY: enumerates Azure (U9), reads the coordination store as a spare-list, runs the
confidence-tier classifier (U10), writes a pass record, and **logs what it would destroy**. It
destroys nothing, and it cannot: the destroy arm is U15 and it is gated behind a flag that is off
in every environment.

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

    _log.info(PASS_COMPLETED_EVENT, store_fault=report.store_fault, **counts)
    await _record_pass(
        outcome="declined" if report.store_fault else "ok",
        counts=counts,
        detail="store fault: the pass refused to judge" if report.store_fault else None,
    )


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
