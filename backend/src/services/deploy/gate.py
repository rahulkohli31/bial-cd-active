"""The publish gate's shared reading: one stored review + one answer set -> one record.

EXTRACTED IN U10, FROM THE ROUTE THAT STILL OWNS THE LADDER. `deploy/router.py` decides
which rung answers; this module holds the three things that decision is *written in* —
how a stored review is read against the commit about to ship, how the three sources are
handed to the merge, and the declaration document every outcome records. They live here
rather than in the route because the route is no longer their only writer: on the drift
path (R13) the DETACHED PIPELINE re-checks the new version and must produce the same
document, in the same shape, for the same queue. A pipeline that cannot import a route
(the route imports the pipeline) would otherwise have grown a second copy of a shape the
route's own docstring calls contract — and two copies of a contract is how `differences`
quietly stops meaning the same thing in the two places an administrator reads it.

Nothing here decides anything. There is no ladder in this module, no status check, no
routing: it reads, it merges, it formats. Both callers keep their own decision.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
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
"""The one audit action every gate outcome writes, whoever decided it — the route's
ladder and the pipeline's post-re-check decision alike. See `append_gate_audit`."""


@dataclass(frozen=True)
class ReviewAtHead:
    """The stored review SITUATED against the commit about to ship — the reading rule 4
    is written in terms of, computed once so the ladder, the merge and the record can
    never disagree about what "there is a review" means.

    `complete` is rule 4's predicate and is deliberately narrower than the bare status
    word: COMPLETE status AND the runner's own `answers_complete` signal AND not aged
    out AND stamped exactly this commit. A complete-but-flagged-partial row is FAILED
    for the ladder — U5 and U6 already class partial as a failure, and reading the bare
    status here would make the two disagree about the same row.

    `verdicts` and `scan` are populated whenever the stored document exists AND is
    stamped this commit, even on a FAILED row: that is P8's Tier A floor arriving (the
    model never returned, a complete scan's high-confidence hit stands in as the
    credentials answer), and dropping it would discard the one signal origin kept for
    when the model is unavailable. A row stamped ANOTHER commit contributes nothing at
    all — a stored answer about an older version must never be read as this version's.
    """

    complete: bool
    available: bool
    """Whether a review document for THIS commit informed the merge — R22's "whether a
    review was available", recorded on every decision."""
    status: str | None
    failure_code: str | None
    source: str | None
    """`review` or `scan_floor` (U6's marker), or None when no document applies."""
    verdicts: dict[str, Any]
    """Per-question stored entries for this commit, or empty."""
    scan: dict[str, Any]
    """The stored scan block (booleans only, never locations), or empty."""


def review_at_head(readout: ReviewReadout | None, head_sha: str | None) -> ReviewAtHead:
    """One stored row + the shipping commit -> the gate's reading of it."""
    if readout is None or head_sha is None or readout.review.head_sha != head_sha:
        # Absent, or stamped a different version — the same nothing, deliberately: R6's
        # whole point is that a stamp mismatch makes a stored answer unusable.
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
    verdict (None when no completed verdict is on record for this version — the merge's
    documented convention), the scan signal, and the policy weight.

    THE VERDICTS ARE ONLY CONSULTED WHEN THE REVIEW IS COMPLETE FOR THIS COMMIT, with
    exactly one exception: the Tier A floor (`source == "scan_floor"`), which lives on a
    FAILED row by construction. Feeding a running row's absent verdicts through as No
    would be the bypass rule 4 exists to close — but rule 4 routes that state anyway, so
    the merge here is about what gets RECORDED, not about whether to route.

    The scan signal is meaningful for credentials alone (the merge's own convention) and
    is read off the stored scan block's booleans — never from a location, which stays
    internal (OD-B)."""
    usable = review.complete or review.source == "scan_floor"
    scan_signal = ScanSignal.NONE
    if usable and review.scan.get("tier_a_hit"):
        scan_signal = ScanSignal.TIER_A
    elif usable and review.scan.get("tier_b_hit"):
        scan_signal = ScanSignal.TIER_B

    inputs: list[QuestionMergeInput] = []
    for key, _label, weight in DATA_CLASSIFICATION_QUESTIONS:
        verdict: Verdict | None = None
        if usable:
            entry = review.verdicts.get(key)
            if isinstance(entry, dict):
                raw = entry.get("verdict")
                # An unrecognised label is treated as NO COMPLETED VERDICT rather than
                # guessed at — the question falls to the citizen (R5), which is the
                # fail-safe direction: it can add routing, never remove it.
                verdict = next((v for v in Verdict if v.value == raw), None)
        inputs.append(
            QuestionMergeInput(
                key=key,
                weight=weight,
                citizen_yes=bool(flags.get(key)),
                review_verdict=verdict,
                scan=scan_signal if key == "credentials_secrets" else ScanSignal.NONE,
            )
        )
    return inputs


@dataclass(frozen=True)
class DriftFacts:
    """WHY THIS QUEUE ITEM LOOKS LIKE AN ANSWER TO A DIFFERENT QUESTION (U10, R13).

    Set only on the save-and-publish path, where the citizen answered the form about one
    commit and the pipeline then re-checked another. Nobody is at the form when that
    decision lands, so R10's mandatory explanation — if there is one at all — was written
    about `answered_about`, not about the version an administrator is being asked to
    approve. U13 leads with that distinction; these are the facts that make it renderable.
    """

    answered_about: str | None
    """The commit the citizen's answers and explanation were written about: the stamp on
    the review that pre-filled the form. None when no stored review informed them."""

    newly_raised: tuple[str, ...]
    """The weighted questionnaire keys that routed this version and that the submitted
    answer set did NOT already carry — the ones the citizen's explanation cannot be an
    answer to, because they were not among the things it was written about.

    NOT the reason publishing stopped, and it must not be read as one: that reason is the
    merged answer set having any weighted Yes at all (rule 6). An item can route with this
    list EMPTY — the citizen declared the category themselves and the re-check simply
    agreed — and a screen that renders "nothing new was found" as "nothing was found"
    would tell an administrator the opposite of the truth."""


def declaration_document(
    *,
    head_sha: str | None,
    citizen: dict[str, bool],
    explanation: str | None,
    review: ReviewAtHead,
    merged: MergeOutcome,
    drift: DriftFacts | None = None,
) -> dict[str, Any]:
    """THE DECLARATION — the one payload every branch records and the queue carries.

    Written once, read by three consumers, so its shape is contract rather than
    convenience: the registry's `declaration` column (U13's admin review screen renders
    it), the `publish_gate` audit detail (R22's per-decision record), and the routed
    response's provenance. Keys are snake_case INSIDE the JSON document — it is stored
    data, not a wire schema, and the questionnaire keys it is keyed by are snake_case
    everywhere else in the system (the deployment row's `classification`, the review
    row's `verdicts`).

        {
          "commits": {"shipping": "<40-hex>" | null,
                      "reviewed": "<40-hex>" | null},
          "citizen":  {"answers": {<key>: bool, ...}, "explanation": str | null},
          "review":   {"available": bool, "complete": bool, "status": str | null,
                       "failureCode": str | null, "source": "review"|"scan_floor"|null,
                       "answers": {<key>: "yes"|"no"|"unanswered", ...},
                       "reasons": {<key>: str, ...},
                       "scan": {"tierAHit": bool, "tierBHit": bool,
                                "incomplete": bool, "tierADispute": bool}},
          "merged":   {"answers": {<key>: bool, ...}, "anyWeightedYes": bool},
          "differences": {<key>: ["review_yes_over_citizen_no", ...], ...},

          # PRESENT ONLY ON THE DRIFT PATH (U10/R13) — absent, not null, otherwise:
          "drift":    {"answeredAbout": "<40-hex>" | null,
                       "shipping": "<40-hex>" | null,
                       "newlyRaised": [<key>, ...],
                       "routedBy": "pipeline_recheck"}
        }

    `differences` carries the merge module's `DisagreementKind` VALUES verbatim and only
    for questions that recorded one — renaming one of those strings is a data migration,
    not a refactor. Evidence locations are structurally absent: the administrator sees
    the plain-language reason and the dispute, never where it was found (OD-B).

    `reasons` is that plain-language half, carried HERE rather than looked up later
    (U13): the review store holds one row per app and is overwritten by the next run
    (R6), so an administrator reading a queue item next week would otherwise be shown
    prose about a version nobody submitted. R6a's rule — the durable history lives in the
    record written at routing time — applies to the reason exactly as it does to the
    verdict. The strings are already redacted and already written for a non-technical
    reader (U6 runs every one through the shared redactor before it is stored), so the
    projection adds no new exposure; the `evidence` document, which is where locations
    live, is never read on this path at all. It matters twice over on U10's drift path:
    the reasons stored there are the RE-CHECK's, about the version actually queued, and
    the row they came from is overwritten by the citizen's very next save.

    `drift` is the U10 block and its presence is itself the signal: this queue item was
    routed by the pipeline after a save, with nobody at the form. `answeredAbout` is the
    commit the citizen's answers and explanation describe, `shipping` is the commit
    actually examined and pinned into the queue, `newlyRaised` names the weighted
    categories the citizen's answers never covered (possibly none — see `DriftFacts`, it is
    not the routing reason), and `routedBy` records that no human submitted this. U13
    renders all four; adding a key here is additive, renaming one is a migration.
    """
    document: dict[str, Any] = {
        "commits": {
            "shipping": head_sha,
            # What the recorded verdicts are actually ABOUT. Equal to shipping whenever a
            # review informed the decision; null when none did. U10's drift path is what
            # makes these two legitimately differ, and U13 leads with that distinction.
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
    if drift is not None:
        document["drift"] = {
            "answeredAbout": drift.answered_about,
            "shipping": head_sha,
            "newlyRaised": list(drift.newly_raised),
            "routedBy": "pipeline_recheck",
        }
    return document


# --- reading one back (U10) ---------------------------------------------------------
#
# THE READERS LIVE BESIDE THE WRITER, deliberately. A declaration written here and taken
# apart somewhere else is the two-copies-of-a-contract failure this module's docstring
# exists to prevent: rename a section above and the only thing that tells you is a queue
# item that quietly reads every category as a new one. The single caller is the drift
# re-check, which is handed the gate's own document and must re-merge what is in it.


def answers_in(declaration: Mapping[str, Any], section: str) -> dict[str, bool]:
    """One answer block out of a declaration document.

    Read defensively — missing key, wrong type, a section that predates a question — and
    the missing answer is False, which is the policy table's own reading of an omitted key
    (`classification.total_weight`). Every direction of that leniency ADDS routing rather
    than removing it: an unreadable submitted baseline makes every Yes look new."""
    block = declaration.get(section)
    answers = block.get("answers") if isinstance(block, dict) else None
    if not isinstance(answers, dict):
        return {}
    return {str(key): bool(value) for key, value in answers.items()}


def explanation_in(declaration: Mapping[str, Any]) -> str | None:
    """The citizen's R10 explanation, already redacted by the gate that stored it. On the
    drift path it was written about the EARLIER version — carried forward unchanged rather
    than dropped, with `drift.answeredAbout` naming the version it answers."""
    citizen = declaration.get("citizen")
    explanation = citizen.get("explanation") if isinstance(citizen, dict) else None
    return explanation if isinstance(explanation, str) else None


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
    """ONE audit action for every gate outcome, APP-SCOPED (ASM7). Commit-less.

    App-scoped is the whole point: the refusal row this replaces was scoped to the
    PROJECT with no app id anywhere in it, so it was invisible to the admin app audit
    drawer — which matches on `resource_id` or `detail->>'appId'` — and R22's "visible"
    was false for the only record the platform kept. Both are set here.

    One action with a `decision` field rather than four actions: the audit vocabulary is
    open (ASM6, no migration needed either way), but a reader asking "what did the gate
    decide for this app, and on what" wants one query, not a union of four. The
    `declaration` carries both answer sets, the differences and whether a review was
    available; `email` is denormalised because the actor REFERENCE is nulled when a user
    is removed and the trail must keep saying who published.

    TWO CALLERS SINCE U10, and the second one is not a request. The route writes the
    ladder's decision; the detached pipeline writes its post-re-check decision on the
    drift path, with the same actor (the citizen who pressed Publish) and the same shape.
    A reader asking that one question still gets one query.
    """
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
