"""The publish gate — the precedence ladder (U9).

Every cell of the ladder's state table resolves to exactly one branch, and this file
pins them one rung at a time, in ladder order, then the properties that hold across
rungs (the declaration, the audit trail, the 503 that must NOT fire on a routed
publish, and the impossibility of a browser-supplied review).

Two fixtures carry the load. `wire` binds the whole composition — a dict-backed store,
a recording pipeline, and the REAL classification review service (no override): the
ladder reads the stored row through the same singleton production resolves, so a mock
returning what it was fed cannot green these tests. `_seed_review` writes rows through
the real store, in the exact document shape U6's runner produces.
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi import FastAPI

from src.api.deps import storage_or_none_dependency
from src.api.v1.build_sessions.deps import sandbox_or_none_dependency, session_manager_dependency
from src.api.v1.deploy.deps import deploy_service_or_none
from src.db.models.app_registry import AppRegistry, ApprovalRoute, AppStatus
from src.db.models.audit import AuditLog
from src.db.models.classification_review import ClassificationReview
from src.services.build_sessions.manager import SaveOutcome, SessionManager
from src.services.classification import store as review_store
from src.services.classification.constants import REVIEW_WALL_CLOCK_CEILING_S
from src.services.deploy.classification import CLASSIFICATION_KEYS
from src.services.deploy.service import StartedDeploy, VersionRecheck
from src.services.storage import snapshot_key
from tests.api.v1.build_sessions.conftest import auth_headers
from tests.factories import AppRegistryFactory, UserFactory
from tests.fakes import FakeStorage, a_git_bundle

_DEPLOY = "/v1/projects/{pid}/deploy"
_SHA = "ab" * 20
_OLDER_SHA = "cd" * 20


# --- wiring --------------------------------------------------------------------------


class _RecordingPipeline:
    """The deploy service, recording instead of reaching Azure.

    `expected_commit_sha` and `recheck` are U10's widening: the gate hands the pipeline the
    commit it decided about on EVERY branch, and the drift branch additionally hands it the
    order to re-check that version before packing."""

    def __init__(self) -> None:
        self.started: list[dict[str, Any]] = []

    async def start(
        self,
        db: object,
        *,
        user_id: uuid.UUID,
        app_id: uuid.UUID,
        project_id: uuid.UUID,
        conversation_id: uuid.UUID | None,
        classification: dict[str, Any] | None = None,
        classification_score: int | None = None,
        expected_commit_sha: str | None = None,
        recheck: VersionRecheck | None = None,
    ) -> StartedDeploy:
        self.started.append(
            {
                "app_id": app_id,
                "classification": classification,
                "classification_score": classification_score,
                "expected_commit_sha": expected_commit_sha,
                "recheck": recheck,
            }
        )
        return StartedDeploy(deployment_id=uuid.uuid4(), app_id=app_id)


class _Wiring:
    def __init__(self, app: FastAPI, store: FakeStorage, pipeline: _RecordingPipeline) -> None:
        self.app = app
        self.store = store
        self.pipeline = pipeline


@pytest.fixture
def wire(app: FastAPI, db_session) -> _Wiring:
    store = FakeStorage()
    pipeline = _RecordingPipeline()

    @contextlib.asynccontextmanager
    async def _session():
        yield db_session

    app.dependency_overrides[storage_or_none_dependency] = lambda: store
    app.dependency_overrides[deploy_service_or_none] = lambda: pipeline
    # No live workspace: nothing can be dirty, so the saved version IS the version.
    app.dependency_overrides[sandbox_or_none_dependency] = lambda: None
    app.dependency_overrides[session_manager_dependency] = lambda: SessionManager(
        session_factory=lambda: _session()
    )
    return _Wiring(app, store, pipeline)


def _answers(**yes: object) -> dict[str, object]:
    body: dict[str, object] = {
        "credentialsSecrets": False,
        "healthData": False,
        "personalInformation": False,
        "financialData": False,
        "confidentialBusinessData": False,
        "publicData": False,
    }
    body.update(yes)
    return body


def _body(*, notes: str | None = None, save_first: bool = False, **yes: object) -> dict[str, Any]:
    answers = _answers(**yes)
    if notes is not None:
        answers["notes"] = notes
    request: dict[str, Any] = {"answers": answers}
    if save_first:
        request["saveFirst"] = True
    return request


_CLEAN = _body()


async def _owner_with_saved_app(db, store: FakeStorage, *, sha: str = _SHA, **overrides):
    user = await UserFactory.create(db)
    app_row = await AppRegistryFactory.create(db, user_id=user.id, **overrides)
    store.objects[snapshot_key(app_row.id)] = a_git_bundle(sha)
    store.meta[snapshot_key(app_row.id)] = {"head_sha": sha}
    return user, app_row


def _verdicts(**by_key: str) -> dict[str, Any]:
    """A stored verdicts document in U6's exact shape."""
    return {
        "source": "review",
        "questions": {
            key: {
                "verdict": by_key.get(key, "no"),
                "reason": f"Plain-language reason for {key}.",
                "agreed_with_scan": None,
                "downgraded_from_yes": False,
            }
            for key in CLASSIFICATION_KEYS
        },
        "scan": {
            "tier_a_hit": False,
            "tier_b_hit": False,
            "incomplete": False,
            "tier_a_dispute": False,
        },
    }


async def _seed_review(
    db,
    *,
    app_id: uuid.UUID,
    user_id: uuid.UUID,
    sha: str = _SHA,
    status: str = "complete",
    answers_complete: bool = True,
    verdicts: dict[str, Any] | None = None,
    failure_code: str = "review_failed",
) -> None:
    """Write one review row through the REAL store, in whichever terminal state the
    scenario needs. `status="running"` leaves the claim as it lands."""
    outcome = await review_store.claim(db, app_id=app_id, user_id=user_id, head_sha=sha)
    assert outcome.claimed
    if status == "running":
        return
    if status == "complete":
        await review_store.succeed(
            db,
            review_id=outcome.review.review_id,
            head_sha=sha,
            attempt=outcome.review.attempt,
            verdicts=verdicts if verdicts is not None else _verdicts(),
            evidence={"questions": {}, "scan_hits": [], "downgraded": []},
            answers_complete=answers_complete,
        )
        return
    await review_store.fail(
        db,
        review_id=outcome.review.review_id,
        head_sha=sha,
        attempt=outcome.review.attempt,
        code=failure_code,
        verdicts=verdicts,
    )


async def _gate_rows(db, app_id: uuid.UUID) -> list[AuditLog]:
    return list(
        (
            await db.execute(
                sa.select(AuditLog)
                .where(
                    AuditLog.resource_type == "app",
                    AuditLog.resource_id == str(app_id),
                    AuditLog.action == "publish_gate",
                )
                .order_by(AuditLog.created_at)
            )
        )
        .scalars()
        .all()
    )


# --- rule 7: nothing weighted anywhere publishes unattended (R14, AE8) ---------------


async def test_no_yes_anywhere_publishes_with_no_queue_entry(wire, client, db_session) -> None:
    """AE8 — the unattended path, unchanged by this feature: an all-No declaration over
    an all-No review reaches the pipeline with no administrator and no queue entry."""
    user, app_row = await _owner_with_saved_app(db_session, wire.store)
    await _seed_review(db_session, app_id=app_row.id, user_id=user.id)

    resp = await client.post(
        _DEPLOY.format(pid=app_row.project_id), headers=auth_headers(user), json=_CLEAN
    )

    assert resp.status_code == 202
    assert resp.json()["outcome"] == "started"
    assert len(wire.pipeline.started) == 1
    fresh = await db_session.get(AppRegistry, app_row.id, populate_existing=True)
    assert fresh is not None
    assert fresh.status is AppStatus.DRAFT  # never entered the queue
    (row,) = await _gate_rows(db_session, app_row.id)
    assert row.detail is not None
    assert row.detail["decision"] == "published"
    assert row.detail["rule"] == "all_clear"


async def test_public_data_only_yes_publishes_and_needs_no_explanation(
    wire, client, db_session
) -> None:
    """AE5d/ASM22 — Public Data carries weight zero, so it routes nothing and compels no
    explanation. The requirement is aligned to ROUTING, not to "any Yes"."""
    user, app_row = await _owner_with_saved_app(db_session, wire.store)
    await _seed_review(db_session, app_id=app_row.id, user_id=user.id)

    resp = await client.post(
        _DEPLOY.format(pid=app_row.project_id),
        headers=auth_headers(user),
        json=_body(publicData=True),  # deliberately NO notes
    )

    assert resp.status_code == 202
    assert len(wire.pipeline.started) == 1


# --- rule 6: a weighted merged Yes routes (R9, AE3) ----------------------------------


async def test_a_review_yes_routes_and_carries_both_answer_sets(wire, client, db_session) -> None:
    """AE3 — the review found personal information the citizen declared absent. The Yes
    stands, the app routes, and the queue item carries BOTH answer sets plus the named
    disagreement, which is what the administrator's screen leads with (R15)."""
    user, app_row = await _owner_with_saved_app(db_session, wire.store)
    await _seed_review(
        db_session,
        app_id=app_row.id,
        user_id=user.id,
        verdicts=_verdicts(personal_information="yes"),
    )

    resp = await client.post(
        _DEPLOY.format(pid=app_row.project_id),
        headers=auth_headers(user),
        json=_body(notes="It only shows the public flight board."),
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["outcome"] == "routed_for_review"
    assert body["commitSha"] == _SHA
    assert wire.pipeline.started == []

    fresh = await db_session.get(AppRegistry, app_row.id, populate_existing=True)
    assert fresh is not None
    assert fresh.status is AppStatus.PENDING
    assert fresh.approval_route is ApprovalRoute.SELF_PUBLISH
    declaration = fresh.declaration
    assert declaration is not None
    assert declaration["citizen"]["answers"]["personal_information"] is False
    assert declaration["review"]["answers"]["personal_information"] == "yes"
    assert declaration["merged"]["answers"]["personal_information"] is True
    assert declaration["differences"]["personal_information"] == ["review_yes_over_citizen_no"]


async def test_the_citizens_own_yes_routes_on_a_question_the_review_left_unanswered(
    wire, client, db_session
) -> None:
    """AE5c/R5 — where the review had no evidence, the citizen's answer is the only one
    on record, and it routes on their word alone."""
    user, app_row = await _owner_with_saved_app(db_session, wire.store)
    await _seed_review(
        db_session,
        app_id=app_row.id,
        user_id=user.id,
        verdicts=_verdicts(financial_data="unanswered"),
    )

    resp = await client.post(
        _DEPLOY.format(pid=app_row.project_id),
        headers=auth_headers(user),
        json=_body(financialData=True, notes="Holds invoice totals for the kiosk."),
    )

    assert resp.status_code == 200
    assert resp.json()["outcome"] == "routed_for_review"
    fresh = await db_session.get(AppRegistry, app_row.id, populate_existing=True)
    assert fresh is not None
    declaration = fresh.declaration
    assert declaration is not None
    assert declaration["merged"]["answers"]["financial_data"] is True
    # An honest abstention is not a disagreement — nothing is recorded against it.
    assert "financial_data" not in declaration["differences"]


async def test_a_weighted_yes_without_an_explanation_is_a_422_not_a_refusal(
    wire, client, db_session
) -> None:
    """R10/ASM22 — incomplete, not rejected: conflating the two would tell someone whose
    answers are fine that they failed the gate. Enforced server-side, at the rung where
    the MERGED outcome exists (a schema validator could never see the review)."""
    user, app_row = await _owner_with_saved_app(db_session, wire.store)
    await _seed_review(db_session, app_id=app_row.id, user_id=user.id)

    resp = await client.post(
        _DEPLOY.format(pid=app_row.project_id),
        headers=auth_headers(user),
        json=_body(personalInformation=True),  # no notes
    )

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "explanation_required"
    assert wire.pipeline.started == []
    fresh = await db_session.get(AppRegistry, app_row.id, populate_existing=True)
    assert fresh is not None
    assert fresh.status is AppStatus.DRAFT


async def test_the_explanation_is_obliged_by_the_merged_answers_not_the_citizens(
    wire, client, db_session
) -> None:
    """The sharp edge of aligning R10 to routing: an all-No citizen declaration whose
    REVIEW raises a weighted Yes still needs an explanation, because that merged set is
    what routes. Nothing about the citizen's own answers could have predicted it."""
    user, app_row = await _owner_with_saved_app(db_session, wire.store)
    await _seed_review(
        db_session, app_id=app_row.id, user_id=user.id, verdicts=_verdicts(health_data="yes")
    )

    resp = await client.post(
        _DEPLOY.format(pid=app_row.project_id), headers=auth_headers(user), json=_CLEAN
    )

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "explanation_required"


# --- rule 4: no genuinely-COMPLETE review for H routes regardless (R20, AE6) ----------


async def test_no_review_at_all_routes_regardless_of_the_answers(wire, client, db_session) -> None:
    """AE6/R20 — an app submitted without a review for the version being published is
    routed whatever was answered. All-No here: only rule 4 can explain the routing."""
    user, app_row = await _owner_with_saved_app(db_session, wire.store)

    resp = await client.post(
        _DEPLOY.format(pid=app_row.project_id), headers=auth_headers(user), json=_CLEAN
    )

    assert resp.status_code == 200
    assert resp.json()["outcome"] == "routed_for_review"
    assert wire.pipeline.started == []
    (row,) = await _gate_rows(db_session, app_row.id)
    assert row.detail is not None
    assert row.detail["rule"] == "review_not_current"
    assert row.detail["declaration"]["review"]["available"] is False


async def test_a_failed_review_routes_regardless_of_the_answers(wire, client, db_session) -> None:
    """A failure is never stored as an answer (R19) and never reads as six No's."""
    user, app_row = await _owner_with_saved_app(db_session, wire.store)
    await _seed_review(db_session, app_id=app_row.id, user_id=user.id, status="failed")

    resp = await client.post(
        _DEPLOY.format(pid=app_row.project_id), headers=auth_headers(user), json=_CLEAN
    )

    assert resp.status_code == 200
    assert resp.json()["outcome"] == "routed_for_review"


async def test_a_review_still_running_routes_regardless_of_the_answers(
    wire, client, db_session
) -> None:
    """Rule 4 says COMPLETE, not "absent or failed", precisely for this row. A running
    review is neither — and falling through to rule 6 would find six unanswered verdicts
    and publish on the citizen's word alone, the exact bypass this feature closes,
    reachable by answering six questions faster than the review lands."""
    user, app_row = await _owner_with_saved_app(db_session, wire.store)
    await _seed_review(db_session, app_id=app_row.id, user_id=user.id, status="running")

    resp = await client.post(
        _DEPLOY.format(pid=app_row.project_id), headers=auth_headers(user), json=_CLEAN
    )

    assert resp.status_code == 200
    assert resp.json()["outcome"] == "routed_for_review"
    assert wire.pipeline.started == []


async def test_a_complete_but_partial_review_routes_the_same_as_a_failed_one(
    wire, client, db_session
) -> None:
    """The ladder reads the BUCKET, not the bare status word: U5 and U6 already class a
    partial answer set as a failure, so a complete-but-flagged-partial row that published
    would make the two disagree about the same row."""
    user, app_row = await _owner_with_saved_app(db_session, wire.store)
    await _seed_review(db_session, app_id=app_row.id, user_id=user.id, answers_complete=False)

    resp = await client.post(
        _DEPLOY.format(pid=app_row.project_id), headers=auth_headers(user), json=_CLEAN
    )

    assert resp.status_code == 200
    assert resp.json()["outcome"] == "routed_for_review"
    assert wire.pipeline.started == []


async def test_a_review_stamped_an_older_commit_routes(wire, client, db_session) -> None:
    """R6/R18 — a stored answer about an older version is not this version's answer. Any
    later Save produces a version the earlier review does not cover."""
    user, app_row = await _owner_with_saved_app(db_session, wire.store)
    await _seed_review(db_session, app_id=app_row.id, user_id=user.id, sha=_OLDER_SHA)

    resp = await client.post(
        _DEPLOY.format(pid=app_row.project_id), headers=auth_headers(user), json=_CLEAN
    )

    assert resp.status_code == 200
    assert resp.json()["outcome"] == "routed_for_review"


async def test_an_aged_out_running_review_routes(wire, client, db_session) -> None:
    """A restart kills the detached runner but leaves the row RUNNING. Past the ceiling
    that row is the review-abandoned state, never still-in-flight — and it routes."""
    user, app_row = await _owner_with_saved_app(db_session, wire.store)
    await _seed_review(db_session, app_id=app_row.id, user_id=user.id, status="running")
    stale = datetime.now(UTC) - timedelta(seconds=REVIEW_WALL_CLOCK_CEILING_S + 60)
    await db_session.execute(
        sa.update(ClassificationReview)
        .where(ClassificationReview.app_id == app_row.id)
        .values(started_at=stale)
    )

    resp = await client.post(
        _DEPLOY.format(pid=app_row.project_id), headers=auth_headers(user), json=_CLEAN
    )

    assert resp.status_code == 200
    assert resp.json()["outcome"] == "routed_for_review"


# --- rule 5: a rejection is sticky (P4) ----------------------------------------------


async def test_a_rejected_app_routes_even_with_a_clean_review_and_clean_answers(
    wire, client, db_session
) -> None:
    """P4 — a rejection is the only human signal in the system and the only one a re-roll
    could erase. It stands until an administrator lifts it, whatever a fresh review says."""
    user, app_row = await _owner_with_saved_app(
        db_session,
        wire.store,
        status=AppStatus.REJECTED,
        rejection_note="Please remove the staff phone list.",
        # Seeded alongside the status because `reject` writes BOTH, and rule 5 reads this
        # one: the status is where the app currently sits, this is whether a human refused
        # it. A fixture setting only the status would be a state production cannot produce.
        rejection_standing=True,
    )
    await _seed_review(db_session, app_id=app_row.id, user_id=user.id)

    resp = await client.post(
        _DEPLOY.format(pid=app_row.project_id), headers=auth_headers(user), json=_CLEAN
    )

    assert resp.status_code == 200
    assert resp.json()["outcome"] == "routed_for_review"
    assert wire.pipeline.started == []
    (row,) = await _gate_rows(db_session, app_row.id)
    assert row.detail is not None
    assert row.detail["rule"] == "rejection_standing"


async def test_a_rejection_survives_the_publish_then_withdraw_round_trip(
    wire, client, db_session
) -> None:
    """THE LAUNDERING CHAIN, end to end. Rule 5 used to read `status`, and two ordinary
    citizen calls walked the row out of it: publishing a REJECTED app ROUTES it (the
    submit service writes PENDING and nulls the note), and withdrawing a PENDING app
    writes DRAFT. By the third call the row had forgotten the refusal and published
    unattended — with a clean review and clean answers, exactly the state P4 says must
    still route. The rejection now lives in a column no citizen path writes.

    Deliberately walks the REAL routes rather than seeding the intermediate states: the
    bug lived in the seam between two handlers that were each correct alone, so a test
    that seeds DRAFT directly would go green against the very code this pins."""
    user, app_row = await _owner_with_saved_app(
        db_session,
        wire.store,
        status=AppStatus.REJECTED,
        rejection_note="Please remove the staff phone list.",
        rejection_standing=True,
    )
    await _seed_review(db_session, app_id=app_row.id, user_id=user.id)
    deploy_url = _DEPLOY.format(pid=app_row.project_id)

    # 1. Publish -> rule 5 routes it, and the submit service moves REJECTED -> PENDING.
    first = await client.post(deploy_url, headers=auth_headers(user), json=_CLEAN)
    assert first.status_code == 200
    assert first.json()["outcome"] == "routed_for_review"

    # 2. Withdraw -> PENDING -> DRAFT. Legal, and it clears the pin and the declaration.
    withdrawn = await client.post(f"/v1/apps/{app_row.id}/withdraw", headers=auth_headers(user))
    assert withdrawn.status_code == 200
    await db_session.refresh(app_row)
    assert app_row.status is AppStatus.DRAFT  # the status genuinely did forget
    assert app_row.rejection_standing is True  # the refusal did not

    # 3. Publish again. This is where it used to go live with nobody looking.
    second = await client.post(deploy_url, headers=auth_headers(user), json=_CLEAN)

    assert second.status_code == 200
    assert second.json()["outcome"] == "routed_for_review"
    assert wire.pipeline.started == []  # never reached the publish pipeline
    rules = [row.detail["rule"] for row in await _gate_rows(db_session, app_row.id) if row.detail]
    assert rules == ["rejection_standing", "rejection_standing"]


async def test_an_approval_is_what_lifts_a_standing_rejection(wire, client, db_session) -> None:
    """The other half of P4: it stands until an ADMINISTRATOR lifts it. `approve` is the
    only writer that lowers the flag, so an approved app stops routing on rule 5."""
    user, app_row = await _owner_with_saved_app(
        db_session,
        wire.store,
        status=AppStatus.REJECTED,
        rejection_note="Please remove the staff phone list.",
        rejection_standing=True,
    )
    await _seed_review(db_session, app_id=app_row.id, user_id=user.id)

    app_row.rejection_standing = False  # what `approve` writes
    app_row.status = AppStatus.DRAFT
    await db_session.commit()

    resp = await client.post(
        _DEPLOY.format(pid=app_row.project_id), headers=auth_headers(user), json=_CLEAN
    )

    assert resp.status_code == 202  # the pipeline accepted it
    assert resp.json()["outcome"] == "started"  # publishes, no human needed


# --- rule 3: the approval override (R17, P5) -----------------------------------------


async def test_an_approval_pinning_this_exact_version_publishes(wire, client, db_session) -> None:
    """AE5b — the citizen publishes the approved version themselves, and it goes live
    rather than routing back. Weighted-Yes answers, no fresh explanation, no review
    needed: the approval IS the decision for this commit."""
    user, app_row = await _owner_with_saved_app(
        db_session,
        wire.store,
        status=AppStatus.APPROVED,
        approval_route=ApprovalRoute.SELF_PUBLISH,
        approved_submission_id=uuid.uuid4(),
        approved_commit_sha=_SHA,
    )

    resp = await client.post(
        _DEPLOY.format(pid=app_row.project_id),
        headers=auth_headers(user),
        json=_body(personalInformation=True, notes="Traveller names, as approved."),
    )

    assert resp.status_code == 202
    assert len(wire.pipeline.started) == 1
    (row,) = await _gate_rows(db_session, app_row.id)
    assert row.detail is not None
    assert row.detail["rule"] == "approved_override"


async def test_an_approval_of_an_earlier_version_does_not_cover_a_later_save(
    wire, client, db_session
) -> None:
    """AE7/R18 — any later Save produces a version the earlier approval does not cover.
    The pin is compared to the commit ACTUALLY shipping, so a moved head routes."""
    user, app_row = await _owner_with_saved_app(
        db_session,
        wire.store,
        status=AppStatus.APPROVED,
        approval_route=ApprovalRoute.SELF_PUBLISH,
        approved_submission_id=uuid.uuid4(),
        approved_commit_sha=_OLDER_SHA,  # approved the PREVIOUS commit
    )

    resp = await client.post(
        _DEPLOY.format(pid=app_row.project_id),
        headers=auth_headers(user),
        json=_body(personalInformation=True, notes="Traveller names."),
    )

    assert resp.status_code == 200
    assert resp.json()["outcome"] == "routed_for_review"
    assert wire.pipeline.started == []


async def test_an_approval_predating_this_feature_is_inert_and_routes(
    wire, client, db_session
) -> None:
    """P5/OD-D — a runbook-lineage approval was granted for an out-of-band code review,
    which is a different decision. The 0030 backfill marked every pre-feature row
    `runbook`, and rule 3 requires `self_publish`, so those approvals authorise the
    manual go-live runbook and nothing here."""
    user, app_row = await _owner_with_saved_app(
        db_session,
        wire.store,
        status=AppStatus.APPROVED,
        approval_route=ApprovalRoute.RUNBOOK,
        approved_submission_id=uuid.uuid4(),
        approved_commit_sha=_SHA,  # the RIGHT commit — only the lineage is wrong
    )

    resp = await client.post(
        _DEPLOY.format(pid=app_row.project_id), headers=auth_headers(user), json=_CLEAN
    )

    assert resp.status_code == 200
    assert resp.json()["outcome"] == "routed_for_review"
    assert wire.pipeline.started == []


# --- rule 3a: the save-and-publish defer (R13) ---------------------------------------


async def test_save_and_publish_over_an_older_stamped_review_defers_to_the_pipeline(
    app, client, db_session
) -> None:
    """AE5/R13 — the save mints a NEW commit, so the stored review is stamped the
    previous one. Without rule 3a, rule 4 would route every single save-and-publish and
    R13 would be unreachable. This branch neither routes nor refuses: it returns 202 and
    lets the pipeline's own re-check (U10) decide."""
    store = FakeStorage()
    pipeline = _RecordingPipeline()
    saved: list[uuid.UUID] = []

    class _Dirty:
        dirty = True

    async def _dirty() -> _Dirty:
        return _Dirty()

    async def _save(self, db, user, project_id, *, sandbox_client) -> SaveOutcome:
        saved.append(project_id)
        # The save mints a new commit: the stamp moves to _SHA, off the review's _OLDER_SHA,
        # and the save reports the commit it landed at (U10's expected commit).
        return SaveOutcome(app_id=uuid.uuid4(), head_sha=_SHA)

    @contextlib.asynccontextmanager
    async def _session():
        yield db_session

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            SessionManager,
            "project_save_state",
            lambda self, db, user, project_id, *, sandbox_client: _dirty(),
        )
        patch.setattr(SessionManager, "save_project_snapshot", _save)
        app.dependency_overrides[storage_or_none_dependency] = lambda: store
        app.dependency_overrides[deploy_service_or_none] = lambda: pipeline
        app.dependency_overrides[sandbox_or_none_dependency] = lambda: object()
        app.dependency_overrides[session_manager_dependency] = lambda: SessionManager(
            session_factory=lambda: _session()
        )

        user, app_row = await _owner_with_saved_app(db_session, store)
        await _seed_review(db_session, app_id=app_row.id, user_id=user.id, sha=_OLDER_SHA)

        resp = await client.post(
            _DEPLOY.format(pid=app_row.project_id),
            headers=auth_headers(user),
            json=_body(save_first=True),
        )

    assert saved == [app_row.project_id]
    # DEFERRED: the pipeline started, nothing routed, nothing refused.
    assert resp.status_code == 202
    assert resp.json()["outcome"] == "started"
    assert len(pipeline.started) == 1
    fresh = await db_session.get(AppRegistry, app_row.id, populate_existing=True)
    assert fresh is not None
    assert fresh.status is AppStatus.DRAFT
    (row,) = await _gate_rows(db_session, app_row.id)
    assert row.detail is not None
    assert row.detail["decision"] == "deferred_to_pipeline"
    assert row.detail["rule"] == "saved_over_stale_review"
    # The U10 seam: the commit examined and the stale stamp are both on record, so the
    # in-pipeline review knows which version it must re-check and which it supersedes.
    assert row.detail["declaration"]["commits"]["shipping"] == _SHA
    assert row.detail["staleReviewSha"] == _OLDER_SHA


async def test_a_pending_app_cannot_defer_by_saving_first(app, client, db_session) -> None:
    """Rule 3a is narrow BECAUSE rules 1, 2 and 5 are evaluated before the save: a
    disabled, pending or rejected app must never reach the pipeline by this door."""
    store = FakeStorage()
    pipeline = _RecordingPipeline()

    class _Dirty:
        dirty = True

    async def _dirty() -> _Dirty:
        return _Dirty()

    saved: list[uuid.UUID] = []

    async def _save(self, db, user, project_id, *, sandbox_client) -> None:
        saved.append(project_id)

    @contextlib.asynccontextmanager
    async def _session():
        yield db_session

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            SessionManager,
            "project_save_state",
            lambda self, db, user, project_id, *, sandbox_client: _dirty(),
        )
        patch.setattr(SessionManager, "save_project_snapshot", _save)
        app.dependency_overrides[storage_or_none_dependency] = lambda: store
        app.dependency_overrides[deploy_service_or_none] = lambda: pipeline
        app.dependency_overrides[sandbox_or_none_dependency] = lambda: object()
        app.dependency_overrides[session_manager_dependency] = lambda: SessionManager(
            session_factory=lambda: _session()
        )

        user, app_row = await _owner_with_saved_app(
            db_session, store, status=AppStatus.PENDING, source_commit_sha=_OLDER_SHA
        )
        resp = await client.post(
            _DEPLOY.format(pid=app_row.project_id),
            headers=auth_headers(user),
            json=_body(save_first=True),
        )

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "waiting_for_review"
    # Refused BEFORE the save — the plain refusals still change nothing.
    assert saved == []
    assert pipeline.started == []


# --- rules 1 and 2: the plain refusals ------------------------------------------------


async def test_a_disabled_app_cannot_publish(wire, client, db_session) -> None:
    user, app_row = await _owner_with_saved_app(db_session, wire.store, status=AppStatus.DISABLED)
    await _seed_review(db_session, app_id=app_row.id, user_id=user.id)

    resp = await client.post(
        _DEPLOY.format(pid=app_row.project_id), headers=auth_headers(user), json=_CLEAN
    )

    assert resp.status_code == 409
    error = resp.json()["error"]
    assert error["code"] == "app_disabled"
    # A refusal with nothing structured to render carries NO `detail` key at all — the
    # handler builds the body key by key rather than dumping a model, so a client can
    # test presence instead of having to distinguish absent from null.
    assert "detail" not in error
    assert wire.pipeline.started == []


async def test_a_pending_app_is_refused_with_the_state_the_surfaces_must_render(
    wire, client, db_session
) -> None:
    """R15b — while a version waits, the publish control says so and cannot submit again.
    The refusal carries the state, the submitted version and the rejection note when one
    exists, so BOTH citizen surfaces render it without a second call."""
    submitted_at = datetime.now(UTC)
    user, app_row = await _owner_with_saved_app(
        db_session,
        wire.store,
        status=AppStatus.PENDING,
        source_submission_id=uuid.uuid4(),
        source_commit_sha=_OLDER_SHA,
        submitted_at=submitted_at,
        rejection_note="Earlier round: please drop the ID numbers.",
    )

    resp = await client.post(
        _DEPLOY.format(pid=app_row.project_id), headers=auth_headers(user), json=_CLEAN
    )

    assert resp.status_code == 409
    error = resp.json()["error"]
    assert error["code"] == "waiting_for_review"
    assert error["detail"]["status"] == "pending"
    assert error["detail"]["submittedSha"] == _OLDER_SHA
    assert error["detail"]["submittedAt"] is not None
    assert error["detail"]["rejectionNote"] == "Earlier round: please drop the ID numbers."
    assert wire.pipeline.started == []


# --- properties that hold across the rungs -------------------------------------------


async def test_routing_works_with_the_deploy_service_unbound(app, client, db_session) -> None:
    """ASM10 — the 503 moved DOWN. It used to be `deploy_project`'s first body statement,
    which shut the door before the ladder ran and stranded exactly the citizens ASM10
    says are not stranded: routing needs object storage and the queue, never the deploy
    service. With the pipeline unbound, a weighted Yes still reaches the queue."""
    store = FakeStorage()

    @contextlib.asynccontextmanager
    async def _session():
        yield db_session

    app.dependency_overrides[storage_or_none_dependency] = lambda: store
    app.dependency_overrides[deploy_service_or_none] = lambda: None  # UNBOUND
    app.dependency_overrides[sandbox_or_none_dependency] = lambda: None
    app.dependency_overrides[session_manager_dependency] = lambda: SessionManager(
        session_factory=lambda: _session()
    )

    user, app_row = await _owner_with_saved_app(db_session, store)
    await _seed_review(
        db_session,
        app_id=app_row.id,
        user_id=user.id,
        verdicts=_verdicts(credentials_secrets="yes"),
    )

    resp = await client.post(
        _DEPLOY.format(pid=app_row.project_id),
        headers=auth_headers(user),
        json=_body(notes="The vendor key is in an environment variable."),
    )

    assert resp.status_code == 200
    assert resp.json()["outcome"] == "routed_for_review"
    fresh = await db_session.get(AppRegistry, app_row.id, populate_existing=True)
    assert fresh is not None
    assert fresh.status is AppStatus.PENDING


async def test_publishing_still_503s_when_the_deploy_service_is_unbound(
    app, client, db_session
) -> None:
    """The other half of moving the 503 down: a branch that genuinely needs the pipeline
    still refuses with the documented status and envelope."""
    store = FakeStorage()

    @contextlib.asynccontextmanager
    async def _session():
        yield db_session

    app.dependency_overrides[storage_or_none_dependency] = lambda: store
    app.dependency_overrides[deploy_service_or_none] = lambda: None
    app.dependency_overrides[sandbox_or_none_dependency] = lambda: None
    app.dependency_overrides[session_manager_dependency] = lambda: SessionManager(
        session_factory=lambda: _session()
    )

    user, app_row = await _owner_with_saved_app(db_session, store)
    await _seed_review(db_session, app_id=app_row.id, user_id=user.id)

    resp = await client.post(
        _DEPLOY.format(pid=app_row.project_id), headers=auth_headers(user), json=_CLEAN
    )

    assert resp.status_code == 503
    assert "message" in resp.json()["error"]


async def test_a_browser_supplied_review_cannot_influence_the_decision(
    wire, client, db_session
) -> None:
    """R12 — the gate reads the STORED review, and the request schema has no review
    field. Extra body keys are dropped at the boundary, so a caller cannot answer for
    the platform: the stored Yes still routes, whatever the body claims."""
    user, app_row = await _owner_with_saved_app(db_session, wire.store)
    await _seed_review(
        db_session,
        app_id=app_row.id,
        user_id=user.id,
        verdicts=_verdicts(personal_information="yes"),
    )

    body = _body(notes="Nothing sensitive, honestly.")
    # Every shape a hopeful client might try.
    body["review"] = {"questions": {key: {"verdict": "no"} for key in CLASSIFICATION_KEYS}}
    body["answers"]["review"] = {"personalInformation": "no"}
    body["reviewComplete"] = True
    body["classificationScore"] = 0

    resp = await client.post(
        _DEPLOY.format(pid=app_row.project_id), headers=auth_headers(user), json=body
    )

    assert resp.status_code == 200
    assert resp.json()["outcome"] == "routed_for_review"
    assert wire.pipeline.started == []


async def test_every_branch_writes_an_app_scoped_audit_row(wire, client, db_session) -> None:
    """ASM7/R22 — today's refusal row was PROJECT-scoped with no app id anywhere, so it
    was invisible to the admin app drawer (which matches `resource_id` OR
    `detail->>'appId'`). Every gate outcome now satisfies both halves of that match, and
    carries both answer sets, the differences, review availability and the decision."""
    user, app_row = await _owner_with_saved_app(db_session, wire.store)
    await _seed_review(
        db_session,
        app_id=app_row.id,
        user_id=user.id,
        verdicts=_verdicts(confidential_business_data="yes"),
    )

    resp = await client.post(
        _DEPLOY.format(pid=app_row.project_id),
        headers=auth_headers(user),
        json=_body(notes="Vendor contact list."),
    )
    assert resp.status_code == 200

    (row,) = await _gate_rows(db_session, app_row.id)
    assert row.resource_type == "app"
    assert row.resource_id == str(app_row.id)
    assert row.actor_id == user.id
    detail = row.detail
    assert detail is not None
    assert detail["appId"] == str(app_row.id)  # the drawer's OTHER match arm
    assert detail["email"] == user.email
    assert detail["decision"] == "routed"
    declaration = detail["declaration"]
    assert declaration["citizen"]["answers"]["confidential_business_data"] is False
    assert declaration["review"]["answers"]["confidential_business_data"] == "yes"
    assert declaration["review"]["available"] is True
    assert declaration["differences"]["confidential_business_data"] == [
        "review_yes_over_citizen_no"
    ]


async def test_the_published_outcome_is_audited_too(wire, client, db_session) -> None:
    """R22 says EVERY publish and every routing decision, so the quiet successes are on
    record as well — those are the ones nobody would think to look for later."""
    user, app_row = await _owner_with_saved_app(db_session, wire.store)
    await _seed_review(db_session, app_id=app_row.id, user_id=user.id)

    resp = await client.post(
        _DEPLOY.format(pid=app_row.project_id), headers=auth_headers(user), json=_CLEAN
    )
    assert resp.status_code == 202

    (row,) = await _gate_rows(db_session, app_row.id)
    assert row.detail is not None
    assert row.detail["decision"] == "published"
    assert row.detail["declaration"]["review"]["available"] is True


async def test_the_explanation_is_redacted_before_it_is_stored(wire, client, db_session) -> None:
    """ASM15 — the citizen's mandatory explanation lands in the same record the review's
    own text is carefully kept clean of, so it passes the shared redactor first."""
    user, app_row = await _owner_with_saved_app(db_session, wire.store)
    await _seed_review(db_session, app_id=app_row.id, user_id=user.id)

    resp = await client.post(
        _DEPLOY.format(pid=app_row.project_id),
        headers=auth_headers(user),
        json=_body(
            personalInformation=True,
            notes='It signs in with password = "hunter2plaintext" to the vendor API.',
        ),
    )

    assert resp.status_code == 200
    fresh = await db_session.get(AppRegistry, app_row.id, populate_existing=True)
    assert fresh is not None
    declaration = fresh.declaration
    assert declaration is not None
    assert "hunter2plaintext" not in declaration["citizen"]["explanation"]


async def test_the_gate_is_owner_scoped(wire, client, db_session) -> None:
    """A dropped ownership predicate is a cross-user leak, not a style nit (ADR-0004),
    and a stranger's probe must be a non-leaking 404 — never a 403 confirming the
    project exists."""
    _owner, app_row = await _owner_with_saved_app(db_session, wire.store)
    stranger = await UserFactory.create(db_session, email="stranger@rvaiglobal.com")

    resp = await client.post(
        _DEPLOY.format(pid=app_row.project_id), headers=auth_headers(stranger), json=_CLEAN
    )

    assert resp.status_code == 404
    assert wire.pipeline.started == []


# --- P8's two obligations, at the GATE rather than in the pure merge -------------------
#
# `test_merge.py` proves the truth table exhaustively, but it feeds `merge_question`
# directly-constructed `ScanSignal` / `Verdict` values. Nothing proved that `gate.py` reads
# a REAL stored review document and produces those inputs — and the translation is the only
# code path that turns a stored Tier A hit into a routing decision. These four drive the
# whole chain: stored document -> merge_inputs -> merge -> ladder -> declaration.


def _floor_verdicts() -> dict[str, Any]:
    """What the runner stores when the model never returned and the Tier A scan stood in
    as the credentials answer (P8's floor) — a FAILED row carrying a verdicts document,
    which is the one shape `merge_inputs` consults outside a completed review."""
    doc = _verdicts(credentials_secrets="yes")
    doc["source"] = "scan_floor"
    doc["scan"]["tier_a_hit"] = True
    return doc


def _overruled_verdicts() -> dict[str, Any]:
    """A COMPLETE review that was shown a Tier A hit and answered No anyway."""
    doc = _verdicts()  # every question "no", credentials included
    doc["scan"]["tier_a_hit"] = True
    doc["scan"]["tier_a_dispute"] = True
    return doc


async def test_the_scan_floor_stands_in_as_the_credentials_answer_and_routes(
    wire, client, db_session
) -> None:
    """P8's second obligation: the model never returned, so the high-confidence scan hit
    IS the credentials answer. Routing is rule 4's doing here (no complete review), but
    what this pins is that the FAILED row's floor document still reaches the merge and
    lands in the record as the scan standing in — not as a blank the citizen decided."""
    user, app_row = await _owner_with_saved_app(db_session, wire.store)
    await _seed_review(
        db_session,
        app_id=app_row.id,
        user_id=user.id,
        status="failed",
        verdicts=_floor_verdicts(),
    )

    # Notes supplied because the scan FLOOR still owes an explanation, unlike the two
    # dispute cases below. The asymmetry is deliberate and pre-dates this change: the
    # floor is the credentials ANSWER OF RECORD (the model never returned, so the scan is
    # the only answer there is), whereas an overrule/discard is a disagreement ABOUT an
    # answer the form already showed as No. Worth revisiting — a citizen meeting the floor
    # is also being asked to explain something no surface named — but it is shipped
    # behaviour and not what this change set out to alter.
    resp = await client.post(
        _DEPLOY.format(pid=app_row.project_id),
        headers=auth_headers(user),
        json=_body(notes="The key in that file is a rotated test value."),
    )

    assert resp.status_code == 200
    assert resp.json()["outcome"] == "routed_for_review"
    fresh = await db_session.get(AppRegistry, app_row.id, populate_existing=True)
    declaration = fresh.declaration
    assert declaration is not None
    assert declaration["merged"]["answers"]["credentials_secrets"] is True
    # THE LABEL, which is the half that was wrong. A floor row is a review that never
    # returned, so `merge_inputs` hands the merge no verdict for it and the merge's own
    # floor branch records SCAN_STOOD_IN. Passing the floor's stored `yes` through as a
    # VERDICT instead made this branch unreachable and told the administrator "the
    # automatic check found this kind of data" on the one path where none ever ran.
    assert declaration["differences"]["credentials_secrets"] == ["scan_stood_in"]
    # The floor answers ONLY credentials; every other question is the citizen's alone (R5).
    assert "personal_information" not in declaration["differences"]


async def test_a_tier_a_hit_the_review_overruled_routes_and_names_the_dispute(
    wire, client, db_session
) -> None:
    """P8's first obligation, and the cell it was written for. Both sides answered No —
    the review because it overruled the scan, the citizen on their own form — so before
    this the app published unattended and the recorded dispute rendered on a screen
    nobody would open for it. It routes, and the administrator is told why."""
    user, app_row = await _owner_with_saved_app(db_session, wire.store)
    await _seed_review(
        db_session, app_id=app_row.id, user_id=user.id, verdicts=_overruled_verdicts()
    )

    resp = await client.post(
        _DEPLOY.format(pid=app_row.project_id), headers=auth_headers(user), json=_CLEAN
    )

    assert resp.status_code == 200
    assert resp.json()["outcome"] == "routed_for_review"
    fresh = await db_session.get(AppRegistry, app_row.id, populate_existing=True)
    declaration = fresh.declaration
    assert declaration is not None
    assert declaration["review"]["answers"]["credentials_secrets"] == "no"
    assert declaration["citizen"]["answers"]["credentials_secrets"] is False
    assert declaration["merged"]["answers"]["credentials_secrets"] is True
    assert declaration["differences"]["credentials_secrets"] == ["tier_a_overrule"]


async def test_a_clean_review_with_no_scan_hit_still_publishes_unattended(
    wire, client, db_session
) -> None:
    """The counterweight to the two above: routing is the DISPUTE's doing, not the
    presence of a scan block. Same all-No review, no Tier A hit — AE8's unattended path
    is untouched, which is what stops the fix from routing every app."""
    user, app_row = await _owner_with_saved_app(db_session, wire.store)
    await _seed_review(db_session, app_id=app_row.id, user_id=user.id)

    resp = await client.post(
        _DEPLOY.format(pid=app_row.project_id), headers=auth_headers(user), json=_CLEAN
    )

    assert resp.status_code == 202
    assert resp.json()["outcome"] == "started"


async def test_a_yes_discarded_for_bad_evidence_routes_through_the_real_gate(
    wire, client, db_session
) -> None:
    """The R4-discard half, end to end. The runner turns a Yes whose every cited location
    was absent into UNANSWERED and records `downgraded_from_yes`; from the verdict alone
    that is indistinguishable from an honest abstention, so it used to fall to the
    citizen's No and publish. This pins that the stored flag survives `merge_inputs`."""
    user, app_row = await _owner_with_saved_app(db_session, wire.store)
    doc = _verdicts()
    doc["questions"]["health_data"]["verdict"] = "unanswered"
    doc["questions"]["health_data"]["downgraded_from_yes"] = True
    await _seed_review(db_session, app_id=app_row.id, user_id=user.id, verdicts=doc)

    resp = await client.post(
        _DEPLOY.format(pid=app_row.project_id), headers=auth_headers(user), json=_CLEAN
    )

    assert resp.status_code == 200
    assert resp.json()["outcome"] == "routed_for_review"
    fresh = await db_session.get(AppRegistry, app_row.id, populate_existing=True)
    declaration = fresh.declaration
    assert declaration is not None
    assert declaration["merged"]["answers"]["health_data"] is True
    assert declaration["differences"]["health_data"] == ["unevidenced_yes_routed"]
