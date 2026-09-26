"""The publish gate's reading: one stored review + one answer set -> one record.

`deploy/router.py` decides which rung answers; this module holds what that decision is
*written in* — reading a stored review against the shipping commit, handing sources to the
merge, the declaration document every outcome records, and the one audit action.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.classification_review import ClassificationReviewStatus
from src.services.audit.log import append_audit
from src.services.classification.merge import (
    MergeOutcome,
    QuestionMergeInput,
    ScanSignal,
)
from src.services.classification.schema import Verdict
from src.services.classification.service import ReviewReadout
from src.services.classification.store import ReviewRecord
from src.services.deploy.classification import DATA_CLASSIFICATION_QUESTIONS

GATE_AUDIT_ACTION = "publish_gate"
"""The one audit action every gate outcome writes. See `append_gate_audit`."""


@dataclass(frozen=True)
class ReviewAtHead:
    """The stored review SITUATED against the shipping commit, computed once so the ladder, merge
    and record agree on what "there is a review" means.

    `complete` (rule 4's predicate) is narrower than the bare status: COMPLETE AND
    `answers_complete` AND not aged out AND stamped exactly this commit — a complete-but-partial
    row reads FAILED, as the runner does. `verdicts`/`scan` populate whenever the document is
    stamped this commit, even a FAILED row (the Tier A scan floor); a row stamped ANOTHER commit
    contributes nothing — an answer about an older version must NEVER be read as this one's."""

    complete: bool
    available: bool
    """Whether a review document for THIS commit informed the merge, recorded on every
    decision."""
    status: str | None
    failure_code: str | None
    source: str | None
    """`review` or `scan_floor`, or None when no document applies."""
    verdicts: dict[str, Any]
    """Per-question stored entries for this commit, or empty."""
    scan: dict[str, Any]
    """The stored scan block (booleans only, never locations), or empty."""


def review_at_head(readout: ReviewReadout | None, head_sha: str | None) -> ReviewAtHead:
    """One stored row + the shipping commit -> the gate's reading of it."""
    if readout is None or head_sha is None or readout.review.head_sha != head_sha:
        # Absent, or stamped a different version — the same nothing, deliberately: a
        # stamp mismatch makes a stored answer unusable.
        return ReviewAtHead(
            complete=False,
            available=False,
            status=readout.review.status.value if readout is not None else None,
            failure_code=readout.review.failure_code if readout is not None else None,
            source=None,
            verdicts={},
            scan={},
        )
    record: ReviewRecord = readout.review
    document = record.verdicts or {}
    questions = document.get("questions") or {}
    complete = (
        record.status is ClassificationReviewStatus.COMPLETE
        and record.answers_complete is True
        and not readout.aged_out
    )
    return ReviewAtHead(
        complete=complete,
        available=bool(questions),
        status=record.status.value,
        failure_code=record.failure_code,
        source=document.get("source"),
        verdicts=questions,
        scan=document.get("scan") or {},
    )


def merge_inputs(flags: dict[str, bool], review: ReviewAtHead) -> list[QuestionMergeInput]:
    """One `QuestionMergeInput` per questionnaire key: the citizen's answer, the stored
    verdict (None when no completed verdict is on record — the merge's convention), the
    scan signal, and the policy weight.

    VERDICTS ARE CONSULTED ONLY WHEN THE REVIEW IS COMPLETE FOR THIS COMMIT, except the
    Tier A floor (a FAILED row by construction) — feeding a running row's absent verdicts
    through as No would reopen the bypass rule 4 closes. The scan signal is meaningful
    for credentials alone, read off the stored booleans, never a location."""
    # A FLOOR row is not a review that answered — it is the record of one that never
    # returned, with the scan's Tier A hit written in as the credentials answer. Its
    # verdicts are therefore NOT handed to the merge as verdicts: `review_verdict=None` is
    # the merge's documented word for "no completed verdict is on record", which is
    # exactly this row's situation, and it is what lets the merge's own floor branch fire
    # and record SCAN_STOOD_IN.
    #
    # Passing the stored `yes` through instead made that branch UNREACHABLE, and the
    # mislabel was user-visible: the queue item read `review_yes_over_citizen_no`, whose
    # admin copy is "The automatic check found this kind of data" — on the one path where
    # no automatic check ran at all. The non-credentials questions are stored `unanswered`
    # on a floor row and merge identically either way (both fall to the citizen), so
    # nothing else moves.
    floor = review.source == "scan_floor"
    usable = review.complete or floor
    scan_signal = ScanSignal.NONE
    if usable and review.scan.get("tier_a_hit"):
        scan_signal = ScanSignal.TIER_A
    elif usable and review.scan.get("tier_b_hit"):
        scan_signal = ScanSignal.TIER_B

    inputs: list[QuestionMergeInput] = []
    for key, _label, weight in DATA_CLASSIFICATION_QUESTIONS:
        verdict: Verdict | None = None
        downgraded = False
        if usable and not floor:
            entry = review.verdicts.get(key)
            if isinstance(entry, dict):
                raw = entry.get("verdict")
                # An unrecognised label is treated as NO COMPLETED VERDICT rather than
                # guessed at — the question falls to the citizen, which is the fail-safe
                # direction: it can add routing, never remove it.
                verdict = next((v for v in Verdict if v.value == raw), None)
                # The runner's discard, carried through rather than re-derived: it turned
                # a Yes whose every cited location was absent into UNANSWERED, and from
                # the verdict alone that is indistinguishable from an honest abstention.
                # The merge routes on it (the agent DID raise a flag), so losing the flag
                # here would silently restore the fall-through it exists to close.
                downgraded = bool(entry.get("downgraded_from_yes"))
        inputs.append(
            QuestionMergeInput(
                key=key,
                weight=weight,
                citizen_yes=bool(flags.get(key)),
                review_verdict=verdict,
                scan=scan_signal if key == "credentials_secrets" else ScanSignal.NONE,
                downgraded_from_yes=downgraded,
            )
        )
    return inputs


def declaration_document(
    *,
    head_sha: str | None,
    citizen: dict[str, bool],
    explanation: str | None,
    review: ReviewAtHead,
    merged: MergeOutcome,
) -> dict[str, Any]:
    """THE DECLARATION — the one payload every branch records and the queue carries.

    Written once, read by three consumers, so its shape is CONTRACT: the registry's
    `declaration` column, the `publish_gate` audit detail, and the routed response's
    provenance. `differences` carries `DisagreementKind` VALUES verbatim — renaming one
    is a data migration. Evidence locations are structurally absent; only the
    plain-language `reasons` ships, carried HERE because the review store is overwritten
    by the next run."""
    document: dict[str, Any] = {
        "commits": {
            "shipping": head_sha,
            # What the recorded verdicts are actually ABOUT: null when no review informed
            # the decision.
            "reviewed": head_sha if review.available else None,
        },
        "citizen": {"answers": dict(citizen), "explanation": explanation},
        "review": {
            "available": review.available,
            "complete": review.complete,
            "status": review.status,
            "failureCode": review.failure_code,
            "source": review.source,
            "answers": {
                key: str(entry.get("verdict"))
                for key, entry in review.verdicts.items()
                if isinstance(entry, dict)
            },
            # Only where the stored entry actually holds prose: an absent reason must stay
            # absent so the screen can say "no reason recorded" rather than render "None".
            "reasons": {
                key: str(entry["reason"])
                for key, entry in review.verdicts.items()
                if isinstance(entry, dict) and isinstance(entry.get("reason"), str)
            },
            "scan": {
                "tierAHit": bool(review.scan.get("tier_a_hit")),
                "tierBHit": bool(review.scan.get("tier_b_hit")),
                "incomplete": bool(review.scan.get("incomplete")),
                "tierADispute": bool(review.scan.get("tier_a_dispute")),
            },
        },
        "merged": {
            "answers": {question.key: question.effective_yes for question in merged.questions},
            "anyWeightedYes": merged.any_weighted_yes,
        },
        "differences": {
            question.key: [kind.value for kind in question.recorded]
            for question in merged.questions
            if question.recorded
        },
    }
    return document


async def append_gate_audit(
    db: AsyncSession,
    *,
    actor_id: uuid.UUID,
    email: str | None,
    app_id: uuid.UUID,
    project_id: uuid.UUID,
    decision: str,
    rule: str,
    declaration: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """ONE audit action for every gate outcome, APP-SCOPED. Commit-less.

    App-scoped is the fix: the refusal row this replaces was PROJECT-scoped with no app
    id, invisible to the admin app audit drawer (`resource_id`/`detail->>'appId'`). One
    action with a `decision` field, not four, so one query answers "what did the gate
    decide for this app". `email` is denormalised because the actor REFERENCE is nulled
    on user removal."""
    detail: dict[str, Any] = {
        "appId": str(app_id),
        "projectId": str(project_id),
        "email": email,
        "decision": decision,
        # WHICH rung answered — the difference between "routed because the review found
        # something" and "routed because there was no review" is the whole story.
        "rule": rule,
    }
    if declaration is not None:
        detail["declaration"] = declaration
    if extra:
        detail.update(extra)
    await append_audit(
        db,
        actor_id=actor_id,
        action=GATE_AUDIT_ACTION,
        resource_type="app",
        resource_id=str(app_id),
        detail=detail,
    )
