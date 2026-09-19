"""The two deploy routes — the PLUMBING around the publish gate.

The 202 is the load-bearing assertion: a deploy runs for minutes and the edge gateway
times out at twenty seconds, so a route that waited for the result would 504 on a deploy
going fine, the citizen would retry, and the second claim would 409. The 503 matters too —
a provider that RAISED when unconfigured would surface as a 500 in the wrong envelope.

THE DECISION ITSELF LIVES IN `test_publish_gate.py`. Every test here seeds a clean stored
review so the ladder lands on rule 7 (publish), and a failure in this file is never the
gate quietly routing."""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from sqlalchemy import event

from src.api.deps import storage_or_none_dependency
from src.api.v1.build_sessions.deps import (
    sandbox_dependency,
    sandbox_or_none_dependency,
    session_manager_dependency,
)
from src.api.v1.deploy.deps import deploy_service_or_none
from src.db.models.app_registry import ApprovalRoute, AppStatus
from src.services.build_sessions.manager import SaveOutcome, SessionManager
from src.services.classification import store as review_store
from src.services.deploy.classification import CLASSIFICATION_KEYS
from src.services.deploy.service import DeployNotPossibleError, StartedDeploy
from src.services.storage import StorageError, snapshot_key
from tests.api.v1.build_sessions.conftest import auth_headers
from tests.factories import AppRegistryFactory, ProjectFactory, UserFactory
from tests.fakes import FakeSandboxClient, FakeStorage, a_git_bundle

_DEPLOY = "/v1/projects/{pid}/deploy"
_STATUS = "/v1/projects/{pid}/deployment"
_HEAD_SHA = "7e" * 20


def _answers(**overrides: object) -> dict[str, object]:
    """A data-classification declaration, all-No by default (score 0).

    camelCase keys: asserting through the alias is the only way these tests would catch a
    `CamelModel` misconfiguration that `populate_by_name` would let a snake_case body past."""
    body: dict[str, object] = {
        "credentialsSecrets": False,
        "healthData": False,
        "personalInformation": False,
        "financialData": False,
        "confidentialBusinessData": False,
        "publicData": False,
    }
    body.update(overrides)
    return body


def _body(*, save_first: bool = False, **overrides: object) -> dict[str, object]:
    """A whole deploy request. The answers are NESTED under `answers` — a flat body is a
    422, which is the shape a client that forgot the questionnaire entirely would send."""
    request: dict[str, object] = {"answers": _answers(**overrides)}
    if save_first:
        request["saveFirst"] = True
    return request


# All-No, score 0 — the ONE shape of declaration that auto-deploys (LOW score = safe =
# auto-deploy, HIGH score = needs a human; see classification.py). Every test that is NOT
# about the gate sends this, so a failure elsewhere is never the gate quietly refusing.
#
# Carries a voluntary explanation (still scores 0) so
# `test_the_declaration_is_handed_to_the_service_to_record` can assert `notes` reaches
# `service.start()` — `_NEEDS_REVIEW` can't cover that, since it 409s before `start()` runs.
_QUALIFIES: dict[str, object] = _body(notes="Reads the public flight board only.")

# 40 + 15 = 55, well above AUTO_DEPLOY_MAX_SCORE (0) — the case the gate tests below
# exercise a refusal with.
_NEEDS_REVIEW: dict[str, object] = _body(
    credentialsSecrets=True,
    confidentialBusinessData=True,
    notes="Holds the vendor API key used by the nightly sync.",
)


class FakeService:
    """Records what the route asked for; can refuse like the real claim does."""

    def __init__(self, *, refuse: DeployNotPossibleError | None = None) -> None:
        self.started: list[dict[str, object]] = []
        self._refuse = refuse

    async def start(
        self,
        db,
        *,
        user_id,
        app_id,
        project_id,
        conversation_id,
        classification=None,
        classification_score=None,
        expected_commit_sha=None,
        recheck=None,
    ) -> StartedDeploy:
        if self._refuse is not None:
            raise self._refuse
        self.started.append(
            {
                "user_id": user_id,
                "app_id": app_id,
                "conversation_id": conversation_id,
                "classification": classification,
                "classification_score": classification_score,
                "expected_commit_sha": expected_commit_sha,
                "recheck": recheck,
            }
        )
        return StartedDeploy(deployment_id=uuid.uuid4(), app_id=app_id)


class CleanSaveState:
    """A save-state view with nothing outstanding, at the version the store holds."""

    dirty = False
    saved_head = _HEAD_SHA


@pytest.fixture
def wire(app: FastAPI, db_session, monkeypatch):
    @contextlib.asynccontextmanager
    async def _session():
        yield db_session

    manager = SessionManager(session_factory=lambda: _session())
    monkeypatch.setattr(
        SessionManager,
        "project_save_state",
        lambda self, db, user, project_id, *, sandbox_client: _clean(),
    )
    sbx = FakeSandboxClient()
    service = FakeService()
    store = FakeStorage()
    app.dependency_overrides[session_manager_dependency] = lambda: manager
    app.dependency_overrides[sandbox_dependency] = lambda: sbx
    app.dependency_overrides[sandbox_or_none_dependency] = lambda: sbx
    app.dependency_overrides[deploy_service_or_none] = lambda: service
    # The gate reads the stored review off the snapshot blob's metadata stamp, so storage
    # is no longer optional plumbing — an unbound store is a documented 503 on every branch.
    app.dependency_overrides[storage_or_none_dependency] = lambda: store
    return SimpleNamespace(app=app, service=service, manager=manager, store=store)


async def _clean() -> CleanSaveState:
    return CleanSaveState()


async def _owner_with_app(db, wire=None):
    """An owner and their app, saved at `_HEAD_SHA` with a CLEAN stored review for it.

    Seeding the review is what keeps this file about the plumbing: all-No lands the ladder
    on rule 7 and publishes, so a 202 here means the route worked, not the gate bypassed.
    Tests that never reach the gate (owner scoping, CSRF) pass `wire=None` and skip it."""
    user = await UserFactory.create(db)
    app_row = await AppRegistryFactory.create(db, user_id=user.id)
    if wire is not None:
        key = snapshot_key(app_row.id)
        wire.store.objects[key] = a_git_bundle(_HEAD_SHA)
        wire.store.meta[key] = {"head_sha": _HEAD_SHA}
        outcome = await review_store.claim(
            db, app_id=app_row.id, user_id=user.id, head_sha=_HEAD_SHA
        )
        await review_store.succeed(
            db,
            review_id=outcome.review.review_id,
            head_sha=_HEAD_SHA,
            attempt=outcome.review.attempt,
            verdicts={
                "source": "review",
                "questions": {
                    key: {
                        "verdict": "no",
                        "reason": "Nothing of this kind found.",
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
            },
            evidence={"questions": {}, "scan_hits": [], "downgraded": []},
            answers_complete=True,
        )
    return user, app_row


# --- starting a deploy -------------------------------------------------------------


async def test_a_deploy_returns_202_immediately(wire, client, db_session) -> None:
    """Never 200-after-waiting: the work takes minutes and the edge gives it twenty seconds."""
    user, app_row = await _owner_with_app(db_session, wire)

    resp = await client.post(
        _DEPLOY.format(pid=app_row.project_id), headers=auth_headers(user), json=_QUALIFIES
    )

    assert resp.status_code == 202
    body = resp.json()
    assert body["appId"] == str(app_row.id)
    assert body["deploymentId"]
    assert body["status"] == "running"


async def test_the_deploy_is_scoped_to_the_owner(wire, client, db_session) -> None:
    _owner, app_row = await _owner_with_app(db_session)
    stranger = await UserFactory.create(db_session, email="stranger@rvaiglobal.com")

    resp = await client.post(
        _DEPLOY.format(pid=app_row.project_id), headers=auth_headers(stranger), json=_QUALIFIES
    )

    # A non-leaking 404 — never a 403 that confirms the project exists.
    assert resp.status_code == 404
    assert wire.service.started == []


async def test_a_project_with_no_app_is_refused_not_provisioned(wire, client, db_session) -> None:
    """The build path's resolver UPSERTS a draft app; deploy must not, or a Deploy on an
    empty project would quietly mint one and then fail on the missing snapshot. The refusal
    is CODED (`no_saved_build`, the pipeline's own name for `FAIL_NO_SNAPSHOT`), so a client
    asserts on `error.code` rather than parsing this sentence."""
    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user.id)

    resp = await client.post(
        _DEPLOY.format(pid=project.id), headers=auth_headers(user), json=_QUALIFIES
    )

    assert resp.status_code == 409
    assert "nothing to deploy" in resp.json()["error"]["message"].lower()
    assert resp.json()["error"]["code"] == "no_saved_build"


async def test_an_app_with_nothing_ever_saved_is_refused_with_the_same_code(
    wire, client, db_session
) -> None:
    """The OTHER "nothing saved" site (`_shipping_head`'s `meta is None` branch): an app
    row exists but no snapshot was ever written for it — a different code path from the
    test above, the same citizen-facing fact, and now the same machine code."""
    user, app_row = await _owner_with_app(db_session)  # no `wire` arg: nothing is seeded

    resp = await client.post(
        _DEPLOY.format(pid=app_row.project_id), headers=auth_headers(user), json=_QUALIFIES
    )

    assert resp.status_code == 409
    assert "nothing to deploy" in resp.json()["error"]["message"].lower()
    assert resp.json()["error"]["code"] == "no_saved_build"


async def test_a_deploy_already_in_flight_is_a_409(
    wire, app, client, db_session, monkeypatch
) -> None:
    """The claim's own refusal, surfaced with its code. Built on `wire` so the ladder
    reaches the pipeline at all, then swaps in a refusing service — the 409 has to come
    from the CLAIM, not from the gate declining to get that far."""
    user, app_row = await _owner_with_app(db_session, wire)

    @contextlib.asynccontextmanager
    async def _session():
        yield db_session

    monkeypatch.setattr(
        SessionManager,
        "project_save_state",
        lambda self, db, user, project_id, *, sandbox_client: _clean(),
    )
    app.dependency_overrides[session_manager_dependency] = lambda: SessionManager(
        session_factory=lambda: _session()
    )
    app.dependency_overrides[sandbox_or_none_dependency] = lambda: FakeSandboxClient()
    app.dependency_overrides[deploy_service_or_none] = lambda: FakeService(
        refuse=DeployNotPossibleError("already deploying", code="deploy_in_flight")
    )

    resp = await client.post(
        _DEPLOY.format(pid=app_row.project_id), headers=auth_headers(user), json=_QUALIFIES
    )

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "deploy_in_flight"


async def test_unsaved_work_is_refused_unless_save_first_is_asked_for(
    app, client, db_session, monkeypatch
) -> None:
    """A deploy ships the last SAVED version. Publishing while the workspace is ahead of it
    would ship something the citizen never chose, with no way to notice."""
    user, app_row = await _owner_with_app(db_session)

    class Dirty:
        dirty = True

    async def _dirty() -> Dirty:
        return Dirty()

    @contextlib.asynccontextmanager
    async def _session():
        yield db_session

    monkeypatch.setattr(
        SessionManager,
        "project_save_state",
        lambda self, db, user, project_id, *, sandbox_client: _dirty(),
    )
    app.dependency_overrides[session_manager_dependency] = lambda: SessionManager(
        session_factory=lambda: _session()
    )
    app.dependency_overrides[sandbox_or_none_dependency] = lambda: FakeSandboxClient()
    service = FakeService()
    app.dependency_overrides[deploy_service_or_none] = lambda: service

    resp = await client.post(
        _DEPLOY.format(pid=app_row.project_id), headers=auth_headers(user), json=_QUALIFIES
    )

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "unsaved_changes"
    assert service.started == []


async def test_csrf_is_required(wire, client, db_session) -> None:
    user, app_row = await _owner_with_app(db_session)

    resp = await client.post(
        _DEPLOY.format(pid=app_row.project_id),
        headers=auth_headers(user, with_csrf=False),
        json=_QUALIFIES,
    )

    assert resp.status_code == 403
    assert wire.service.started == []


async def test_publishing_unconfigured_is_a_503_with_the_right_envelope(
    app, client, db_session
) -> None:
    """The provider yields None rather than raising. A raising one would resolve BEFORE the
    route body and escape its error handling, producing a 500 with `{"detail": ...}` instead
    of the `{"error": {...}}` shape every other route on this surface returns."""
    user, app_row = await _owner_with_app(db_session)
    app.dependency_overrides[deploy_service_or_none] = lambda: None

    resp = await client.post(
        _DEPLOY.format(pid=app_row.project_id), headers=auth_headers(user), json=_QUALIFIES
    )

    assert resp.status_code == 503
    assert "error" in resp.json()
    assert "message" in resp.json()["error"]


# --- the retired terminal refusal ----------------------------------------------------


async def test_the_terminal_classification_refusal_is_gone(wire, client, db_session) -> None:
    """A GUARD, not a deletion. The retired 409 `classification_below_threshold` was a dead
    end: nothing queued, nobody notified. The same declaration is now ROUTED, so that 409
    must not come back — and it must not answer 403 either, since `chatErrors.ts` reads a
    403 on this surface as "your session lapsed". The ladder's own outcomes are pinned in
    `test_publish_gate.py`."""
    user, app_row = await _owner_with_app(db_session, wire)

    resp = await client.post(
        _DEPLOY.format(pid=app_row.project_id),
        headers=auth_headers(user),
        json=_body(confidentialBusinessData=True, notes="Vendor contact list only."),
    )

    assert resp.status_code != 409
    assert resp.status_code != 403
    assert resp.json().get("error", {}).get("code") != "classification_below_threshold"
    # It ROUTED (the review is clean but the citizen's own weighted Yes stands), so
    # the pipeline was correctly not started.
    assert resp.status_code == 200
    assert resp.json()["outcome"] == "routed_for_review"
    assert wire.service.started == []


async def test_a_routed_deploy_leaves_the_app_queued_at_the_version_examined(
    app, client, db_session, monkeypatch
) -> None:
    """The gate's ordering is deliberate: the ladder's version-dependent rules must run
    against the POST-save commit, so a save-and-publish saves first, before the gate runs.

    What that buys the citizen: a routed deploy leaves the app queued at exactly the
    version examined, and publishes nothing. The save is the thing they asked for, not a
    side effect of a declined request."""
    user, app_row = await _owner_with_app(db_session)
    saved: list[uuid.UUID] = []
    store = FakeStorage()
    key = snapshot_key(app_row.id)
    store.objects[key] = a_git_bundle(_HEAD_SHA)
    store.meta[key] = {"head_sha": _HEAD_SHA}

    class Dirty:
        dirty = True

    async def _dirty() -> Dirty:
        return Dirty()

    async def _record_save(self, db, user, project_id, *, sandbox_client) -> SaveOutcome:
        saved.append(project_id)
        # The save reports the commit it landed at, which is what the route threads into
        # the pipeline as the expected commit. The store's stamp is the same one.
        return SaveOutcome(app_id=app_row.id, head_sha=_HEAD_SHA)

    @contextlib.asynccontextmanager
    async def _session():
        yield db_session

    monkeypatch.setattr(
        SessionManager,
        "project_save_state",
        lambda self, db, user, project_id, *, sandbox_client: _dirty(),
    )
    monkeypatch.setattr(SessionManager, "save_project_snapshot", _record_save)
    app.dependency_overrides[session_manager_dependency] = lambda: SessionManager(
        session_factory=lambda: _session()
    )
    app.dependency_overrides[sandbox_or_none_dependency] = lambda: FakeSandboxClient()
    app.dependency_overrides[storage_or_none_dependency] = lambda: store
    service = FakeService()
    app.dependency_overrides[deploy_service_or_none] = lambda: service

    resp = await client.post(
        _DEPLOY.format(pid=app_row.project_id),
        headers=auth_headers(user),
        json=_body(
            confidentialBusinessData=True, save_first=True, notes="Vendor contact list only."
        ),
    )

    assert saved == [app_row.project_id]
    assert resp.status_code == 200
    assert resp.json()["outcome"] == "routed_for_review"
    assert resp.json()["commitSha"] == _HEAD_SHA
    assert service.started == []


async def test_a_weighted_declaration_without_an_explanation_is_still_a_422(
    wire, client, db_session
) -> None:
    """Incomplete, not refused. This fires inside the ladder, on the MERGED answers — the
    only place the review can be taken into account — but the distinction still holds: an
    unexplained sensitive declaration is an incomplete submission, never a rejected one."""
    user, app_row = await _owner_with_app(db_session, wire)

    resp = await client.post(
        _DEPLOY.format(pid=app_row.project_id),
        headers=auth_headers(user),
        json=_body(credentialsSecrets=True, confidentialBusinessData=True),
    )

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "explanation_required"
    assert wire.service.started == []


async def test_a_deploy_with_no_answers_at_all_is_rejected(wire, client, db_session) -> None:
    """What makes the questionnaire a gate rather than a prompt: there is no shape of this
    request that deploys without a declaration, so a caller cannot reach the pipeline by
    simply never rendering the modal."""
    user, app_row = await _owner_with_app(db_session)

    resp = await client.post(
        _DEPLOY.format(pid=app_row.project_id), headers=auth_headers(user), json={}
    )

    assert resp.status_code == 422
    assert wire.service.started == []


async def test_the_declaration_is_handed_to_the_service_to_record(
    wire, client, db_session
) -> None:
    """The score that authorised the deploy travels with it — it is stored, never recomputed
    later, because the weights are policy and policy changes."""
    user, app_row = await _owner_with_app(db_session, wire)

    resp = await client.post(
        _DEPLOY.format(pid=app_row.project_id), headers=auth_headers(user), json=_QUALIFIES
    )

    assert resp.status_code == 202
    (started,) = wire.service.started
    assert started["classification_score"] == 0
    declared = started["classification"]
    assert isinstance(declared, dict)
    assert declared["credentials_secrets"] is False
    assert declared["personal_information"] is False
    assert declared["notes"] == "Reads the public flight board only."


# --- reading the status ------------------------------------------------------------


async def test_a_never_deployed_app_reads_as_empty_not_missing(wire, client, db_session) -> None:
    """ "Never deployed" is a normal state a client renders as a Deploy button, not a 404."""
    user, app_row = await _owner_with_app(db_session)

    resp = await client.get(_STATUS.format(pid=app_row.project_id), headers=auth_headers(user))

    assert resp.status_code == 200
    body = resp.json()
    assert body["appId"] == str(app_row.id)
    assert body["deploymentId"] is None
    assert body["status"] is None
    # Never deployed, never submitted — `draft`, not a neighbouring state's lie.
    assert body["publishState"] == "draft"


async def test_the_status_is_owner_scoped(wire, client, db_session) -> None:
    _owner, app_row = await _owner_with_app(db_session)
    stranger = await UserFactory.create(db_session, email="nosy@rvaiglobal.com")

    resp = await client.get(_STATUS.format(pid=app_row.project_id), headers=auth_headers(stranger))

    assert resp.status_code == 404


async def test_the_status_carries_the_apps_approval_state(wire, client, db_session) -> None:
    """The pending state reaches BOTH citizen publish surfaces through this one
    response — the toolbar button has no app id to make a second call with, and a status
    card that reads its lifecycle once on mount is stale the moment a publish routes.

    Mutation receipt: drop `approval=` from either `DeploymentResponse` construction in
    `latest_deployment` and this goes red on `body["approval"]` being None."""
    user, app_row = await _owner_with_app(db_session, wire)
    submitted = datetime(2026, 8, 19, 10, 0, tzinfo=UTC)
    app_row.status = AppStatus.PENDING
    app_row.source_commit_sha = _HEAD_SHA
    app_row.submitted_at = submitted
    app_row.approval_route = ApprovalRoute.SELF_PUBLISH
    app_row.rejection_note = "Explain where the vendor key is stored."
    await db_session.commit()

    resp = await client.get(_STATUS.format(pid=app_row.project_id), headers=auth_headers(user))

    assert resp.status_code == 200
    assert resp.json()["approval"] == {
        "status": "pending",
        "approvedCommitSha": None,
        # NULL for the same reason `approvedCommitSha` is: nobody has approved this yet.
        # Asserted as an exact dict on purpose — a field added to the wire should have to
        # come through here.
        "approvedAt": None,
        "approvalRoute": "self_publish",
        "rejectionNote": "Explain where the vendor key is stored.",
        "submittedSha": _HEAD_SHA,
        "submittedAt": "2026-08-19T10:00:00Z",
    }
    # PENDING wins outright, whatever a deployment row (there is none here) says.
    assert resp.json()["publishState"] == "in_review"


async def test_the_approval_state_is_null_only_when_the_project_has_no_app(
    wire, client, db_session
) -> None:
    """NULL means one thing and one thing only: there is no app row yet. A client that
    renders "we couldn't read your review state" for a plain never-built project would be
    inventing a failure out of a normal state."""
    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user_id=user.id)

    resp = await client.get(_STATUS.format(pid=project.id), headers=auth_headers(user))

    assert resp.status_code == 200
    assert resp.json()["approval"] is None
    assert resp.json()["appId"] is None
    # The only `PublishState` member with no approval block behind it.
    assert resp.json()["publishState"] == "nothing_built"


async def test_a_never_submitted_app_still_reports_its_draft_lifecycle(
    wire, client, db_session
) -> None:
    """A draft app has no submission and no pin — but it DOES have a lifecycle, and the
    surfaces branch on `status`, so reporting nothing here would read as "no app"."""
    user, app_row = await _owner_with_app(db_session, wire)

    resp = await client.get(_STATUS.format(pid=app_row.project_id), headers=auth_headers(user))

    approval = resp.json()["approval"]
    assert approval["status"] == "draft"
    assert approval["submittedSha"] is None
    assert approval["approvedCommitSha"] is None
    assert approval["approvedAt"] is None
    assert approval["approvalRoute"] is None
    assert resp.json()["publishState"] == "draft"


async def test_the_approval_carries_when_it_was_approved_not_only_which_commit(
    wire, client, db_session
) -> None:
    """The approved states name a DATE first and mute the build code beside it, so the stamp
    has to reach the wire. `approved_at` is a column on the registry row this route already
    selects in full, written in exactly one place beside `approved_commit_sha`
    (`admin/router.py`'s `approve`) — which is why the two are asserted together here.

    Mutation receipt: drop `approved_at=row.approved_at` from `ApprovalState.of` and this
    goes red on `approvedAt` being None while the pin beside it is not."""
    user, app_row = await _owner_with_app(db_session, wire)
    approved = datetime(2026, 8, 19, 10, 0, tzinfo=UTC)
    app_row.status = AppStatus.APPROVED
    app_row.approved_commit_sha = _HEAD_SHA
    app_row.approved_at = approved
    app_row.approval_route = ApprovalRoute.SELF_PUBLISH
    await db_session.commit()

    resp = await client.get(_STATUS.format(pid=app_row.project_id), headers=auth_headers(user))

    approval = resp.json()["approval"]
    assert approval["approvedCommitSha"] == _HEAD_SHA
    assert approval["approvedAt"] == "2026-08-19T10:00:00Z"


# --- the status read does not need the pipeline ------------------------------------


async def test_the_status_read_answers_without_a_deploy_pipeline(
    app: FastAPI, client, db_session
) -> None:
    """A `DEPLOY__*`-less deployment is a SUPPORTED state: every field this route answers
    with is a committed row, and the ladder routes without a pipeline, so a rejection note
    has to reach its developer through this call regardless.

    No `wire` and no `fake_storage` — deploy service and store both unbound, the two
    postures this route must tolerate at once. `publishState` still comes back, the same
    as from a `StorageError`."""
    assert deploy_service_or_none not in app.dependency_overrides, (
        "this test is only meaningful with the deploy service UNBOUND — a fixture that "
        "binds it makes the branch under test unreachable"
    )

    user = await UserFactory.create(db_session)
    app_row = await AppRegistryFactory.create(db_session, user_id=user.id)
    note = "Move the hardcoded database URL and API key out of lib/db.ts, then re-submit."
    app_row.status = AppStatus.REJECTED
    app_row.rejection_note = note
    app_row.source_commit_sha = _HEAD_SHA
    await db_session.commit()

    resp = await client.get(_STATUS.format(pid=app_row.project_id), headers=auth_headers(user))

    assert resp.status_code == 200, resp.text
    approval = resp.json()["approval"]
    assert approval["status"] == "rejected"
    # The whole point: the administrator's words reach the person who has to act on them.
    assert approval["rejectionNote"] == note
    assert approval["submittedSha"] == _HEAD_SHA
    assert resp.json()["publishState"] == "changes_requested"


async def test_publishing_still_refuses_without_a_deploy_pipeline(
    wire, client, db_session
) -> None:
    """Reading status needs no pipeline; PUBLISHING does, and that refusal is load-bearing —
    without it "publishing is switched off" becomes a silent no-op.

    Uses `wire` and unbinds ONLY the deploy service: storage is checked FIRST, so an
    all-unbound request 503s as `storage_unavailable` without ever reaching the pipeline
    branch. With everything else wired, the missing pipeline is the only failure left, and
    the `code` assertion keeps the two 503s apart."""
    wire.app.dependency_overrides[deploy_service_or_none] = lambda: None
    user, app_row = await _owner_with_app(db_session, wire)

    resp = await client.post(
        _DEPLOY.format(pid=app_row.project_id), headers=auth_headers(user), json=_QUALIFIES
    )

    assert resp.status_code == 503, resp.text
    body = resp.json()["error"]
    assert body.get("code") != "storage_unavailable", (
        "reached the storage guard, not the pipeline guard — this test would pass with the "
        "pipeline check deleted"
    )
    assert "not switched on" in body["message"]


# --- the one computed publish state ------------------------------------------------


class _CountingStorage(FakeStorage):
    """Counts calls to `head` and `get` separately — the unit's whole cost argument is
    that the publish-state read is a metadata HEAD and never a download of the snapshot
    bytes."""

    def __init__(self) -> None:
        super().__init__()
        self.head_calls = 0
        self.get_calls = 0

    async def head(self, key):
        self.head_calls += 1
        return await super().head(key)

    async def get(self, key):
        self.get_calls += 1
        return await super().get(key)


class _AlwaysBoomingStorage(FakeStorage):
    """A store whose HEAD always raises — the named departure from the two shipped
    readers' 503."""

    async def head(self, key):
        raise StorageError("blob head blipped", provider="fake", key=key)


async def _live_deployment(
    db, *, app_id: uuid.UUID, user_id: uuid.UUID, head_sha: str = _HEAD_SHA
):
    from src.db.models.deployment import Deployment, DeploymentStatus

    row = Deployment(
        app_id=app_id, user_id=user_id, status=DeploymentStatus.SUCCEEDED, head_sha=head_sha
    )
    db.add(row)
    await db.flush()
    return row


async def test_the_status_read_issues_no_new_query_and_exactly_one_metadata_head(
    wire, client, db_session, test_engine
) -> None:
    """The cost argument, pinned on the statement stream rather than trusted from a
    docstring: the ONLY I/O this unit may add beyond the three SELECTs already issued is
    exactly one `storage.head()` — never a second SELECT, and never a `storage.get()` of
    the snapshot bytes (`build_sessions/manager.py::_saved_head` is the named anti-pattern
    this counts against). Asserted in BOTH directions."""
    user, app_row = await _owner_with_app(db_session, wire)
    await _live_deployment(db_session, app_id=app_row.id, user_id=user.id)
    await db_session.commit()

    store = _CountingStorage()
    key = snapshot_key(app_row.id)
    store.objects[key] = a_git_bundle(_HEAD_SHA)
    store.meta[key] = {"head_sha": _HEAD_SHA}
    wire.app.dependency_overrides[storage_or_none_dependency] = lambda: store

    statements: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany) -> None:
        if statement.strip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(test_engine.sync_engine, "before_cursor_execute", _record)
    try:
        resp = await client.get(_STATUS.format(pid=app_row.project_id), headers=auth_headers(user))
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", _record)

    assert resp.status_code == 200, resp.text
    assert resp.json()["publishState"] == "live_current"
    # The three: the project-ownership `get`, the `AppRegistry` select, and
    # `deployment_for_app`'s select. A fourth is what this unit refuses.
    assert len(statements) == 3, f"expected exactly today's three SELECTs, got {statements}"
    assert store.head_calls == 1
    assert store.get_calls == 0, "the snapshot bytes must never be downloaded for this read"


async def test_a_storage_error_reading_the_saved_head_answers_200_not_503(
    wire, client, db_session
) -> None:
    """THE named departure from the storage-down rule: `_shipping_head` turns this same
    exception into a 503 because it's about to ACT on the bundle. This read only answers
    "is there newer work", so a blip here must not blank the response — the approval block
    stays present, and the drift question alone falls back to `live_drift_unknown`."""
    user, app_row = await _owner_with_app(db_session, wire)
    await _live_deployment(db_session, app_id=app_row.id, user_id=user.id)
    app_row.status = AppStatus.PENDING
    app_row.rejection_note = "Explain the third-party API key."
    await db_session.commit()
    wire.app.dependency_overrides[storage_or_none_dependency] = lambda: _AlwaysBoomingStorage()

    resp = await client.get(_STATUS.format(pid=app_row.project_id), headers=auth_headers(user))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    # PENDING wins outright, so this app doesn't itself land on `live_drift_unknown` — the
    # point is the STATUS CODE, and that nothing else in the response went missing.
    assert body["publishState"] == "in_review"
    assert body["approval"]["rejectionNote"] == "Explain the third-party API key."


async def test_a_storage_error_on_a_live_app_reads_drift_unknown_not_current(
    wire, client, db_session
) -> None:
    """The mirror of the counting test above, with the store failing instead of
    answering: unknown must never be spelled "up to date" — the tri-state discipline
    this route holds to."""
    user, app_row = await _owner_with_app(db_session, wire)
    await _live_deployment(db_session, app_id=app_row.id, user_id=user.id)
    await db_session.commit()
    wire.app.dependency_overrides[storage_or_none_dependency] = lambda: _AlwaysBoomingStorage()

    resp = await client.get(_STATUS.format(pid=app_row.project_id), headers=auth_headers(user))

    assert resp.status_code == 200, resp.text
    assert resp.json()["publishState"] == "live_drift_unknown"


async def test_an_unstamped_bundle_also_reads_drift_unknown(wire, client, db_session) -> None:
    """The mirror the unit itself names: a bundle saved before the metadata stamp
    existed reads the same as a store that refused to answer — `head_sha_from_metadata`
    returns `None` for "no claim", and this endpoint must not tell the two apart."""
    user, app_row = await _owner_with_app(db_session, wire)
    await _live_deployment(db_session, app_id=app_row.id, user_id=user.id)
    await db_session.commit()
    key = snapshot_key(app_row.id)
    wire.store.objects[key] = a_git_bundle(_HEAD_SHA)
    wire.store.meta[key] = {}  # present blob, no `head_sha` stamp

    resp = await client.get(_STATUS.format(pid=app_row.project_id), headers=auth_headers(user))

    assert resp.status_code == 200, resp.text
    assert resp.json()["publishState"] == "live_drift_unknown"


# --- the citizen's own last save, on the wire ------------------------------------------
#
# The rail draws three provenance rows — LIVE NOW, APPROVED, YOUR LATEST — pinned here
# through the three traps the unit sits between: the read must not wake a container, it
# must not invent a head for an unstamped bundle, and it must read the CITIZEN'S save
# key rather than the platform's autosave key.

_SAVED_AT = datetime(2026, 8, 25, 14, 20, tzinfo=UTC)
_SAVED_SHA = "f9" * 20
_AUTOSAVED_SHA = "11" * 20


class _NoContainersHere:
    """A sandbox nobody may touch. Any attribute access is recorded AND raises, so a
    container call in this path fails as itself rather than as a mystery 500 — `__getattr__`
    rather than an override list, because a method-name list goes stale the moment
    `SandboxClient` grows one."""

    def __init__(self) -> None:
        self.reached_for: list[str] = []

    def __getattr__(self, name: str) -> object:
        self.reached_for.append(name)
        raise AssertionError(f"the deployment status read reached into the container: {name}")


async def test_the_published_approved_and_saved_rows_arrive_together(
    wire, client, db_session
) -> None:
    """The rail's three provenance rows, from ONE response — asserted together on purpose,
    since a client that had to make three calls to fill them would render them at three
    different moments.

    Mutation receipt: drop `saved_head=`/`saved_at=` from `DeploymentResponse.of` and this
    goes red on the saved row while the two above it stay green."""
    user, app_row = await _owner_with_app(db_session, wire)
    await _live_deployment(db_session, app_id=app_row.id, user_id=user.id)
    app_row.status = AppStatus.APPROVED
    app_row.approved_commit_sha = _HEAD_SHA
    app_row.approved_at = datetime(2026, 8, 24, 9, 0, tzinfo=UTC)
    app_row.approval_route = ApprovalRoute.SELF_PUBLISH
    await db_session.commit()
    key = snapshot_key(app_row.id)
    wire.store.objects[key] = a_git_bundle(_SAVED_SHA)
    wire.store.meta[key] = {"head_sha": _SAVED_SHA}
    wire.store.mtimes[key] = _SAVED_AT

    resp = await client.get(_STATUS.format(pid=app_row.project_id), headers=auth_headers(user))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    # LIVE NOW — the commit that actually went live, off the deployment row.
    assert body["headSha"] == _HEAD_SHA
    assert body["startedAt"] is not None
    # APPROVED — the date first, the pin beside it.
    assert body["approval"]["approvedAt"] == "2026-08-24T09:00:00Z"
    assert body["approval"]["approvedCommitSha"] == _HEAD_SHA
    # YOUR LATEST — the new pair, and the reason the row can exist at all.
    assert body["savedHead"] == _SAVED_SHA
    assert body["savedAt"] == "2026-08-25T14:20:00Z"
    # And the state that follows from the three: saved past what is live.
    assert body["publishState"] == "live_newer_work"


async def test_a_stopped_project_still_reports_its_saved_row_and_wakes_no_container(
    app: FastAPI, client, db_session, monkeypatch
) -> None:
    """THE POINT OF THE FIELD: a citizen whose workspace was reclaimed still gets the saved
    row, because both halves come from object-store metadata, never a container.

    No `wire`: the sandbox providers are bound to a tripwire instead, asserted in BOTH
    directions — never resolved, and no attribute ever reached for."""
    tripwire = _NoContainersHere()
    resolved: list[str] = []

    def _provide() -> object:
        resolved.append("sandbox")
        return tripwire

    store = FakeStorage()
    app.dependency_overrides[storage_or_none_dependency] = lambda: store
    app.dependency_overrides[sandbox_dependency] = _provide
    app.dependency_overrides[sandbox_or_none_dependency] = _provide

    user = await UserFactory.create(db_session)
    app_row = await AppRegistryFactory.create(db_session, user_id=user.id)
    await db_session.commit()
    key = snapshot_key(app_row.id)
    store.objects[key] = a_git_bundle(_SAVED_SHA)
    store.meta[key] = {"head_sha": _SAVED_SHA}
    store.mtimes[key] = _SAVED_AT

    resp = await client.get(_STATUS.format(pid=app_row.project_id), headers=auth_headers(user))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["savedHead"] == _SAVED_SHA
    assert body["savedAt"] == "2026-08-25T14:20:00Z"
    assert resolved == [], "the status read asked for a sandbox client it must never need"
    assert tripwire.reached_for == [], (
        f"the status read touched the container: {tripwire.reached_for}"
    )


async def test_a_bundle_with_no_stamped_head_reports_null_rather_than_a_guess(
    wire, client, db_session
) -> None:
    """A bundle written before the metadata stamp existed has NO claim about which commit
    it holds, and the wire says so — an invented value (the deployment's head, the approved
    pin, an empty string) would make a missing fact look like a present one.

    THE TWO HALVES ARE INDEPENDENT: the store still knows WHEN the bundle was written, so
    the date survives while the id does not. Nulling both would throw away a fact nobody
    lost."""
    user, app_row = await _owner_with_app(db_session, wire)
    key = snapshot_key(app_row.id)
    wire.store.objects[key] = a_git_bundle(_SAVED_SHA)
    wire.store.meta[key] = {}  # a present blob carrying no `head_sha` stamp
    wire.store.mtimes[key] = _SAVED_AT

    resp = await client.get(_STATUS.format(pid=app_row.project_id), headers=auth_headers(user))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["savedHead"] is None
    assert body["savedAt"] == "2026-08-25T14:20:00Z"


async def test_the_saved_row_names_the_bundle_rather_than_a_newer_object_beside_it(
    wire, client, db_session
) -> None:
    """The row says "YOUR LATEST", so it reads the one key a Save writes and nothing else — a
    key-agnostic "newest object for this app" read would name whatever else the store happens
    to hold. Seeded so a wrong read is unmistakable: the decoy is a DIFFERENT, NEWER object.

    Mutation receipt: read the newest object under the app's prefix in
    `_saved_version_for_publish_state` and both assertions below go red."""
    user, app_row = await _owner_with_app(db_session, wire)
    saved = snapshot_key(app_row.id)
    wire.store.objects[saved] = a_git_bundle(_SAVED_SHA)
    wire.store.meta[saved] = {"head_sha": _SAVED_SHA}
    wire.store.mtimes[saved] = _SAVED_AT
    decoy = f"quarantine/{app_row.id}/20260826T110500000000Z.bundle"
    wire.store.objects[decoy] = a_git_bundle(_AUTOSAVED_SHA)
    wire.store.meta[decoy] = {"head_sha": _AUTOSAVED_SHA}
    wire.store.mtimes[decoy] = datetime(2026, 8, 26, 11, 5, tzinfo=UTC)

    resp = await client.get(_STATUS.format(pid=app_row.project_id), headers=auth_headers(user))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["savedHead"] == _SAVED_SHA, "a parked tree is not the citizen's save"
    assert body["savedAt"] == "2026-08-25T14:20:00Z"


async def test_another_citizen_never_learns_the_saved_head_or_when_it_was_saved(
    wire, client, db_session
) -> None:
    """Owner scoping, asserted on the LEAK rather than only on the status code — a field
    added to a response is exactly the kind of change that can widen what a wrong answer
    says. Neither the commit id nor the save time may appear anywhere in the stranger's
    body."""
    owner, app_row = await _owner_with_app(db_session, wire)
    key = snapshot_key(app_row.id)
    wire.store.objects[key] = a_git_bundle(_SAVED_SHA)
    wire.store.meta[key] = {"head_sha": _SAVED_SHA}
    wire.store.mtimes[key] = _SAVED_AT
    stranger = await UserFactory.create(db_session, email="nosy@rvaiglobal.com")

    resp = await client.get(_STATUS.format(pid=app_row.project_id), headers=auth_headers(stranger))

    assert resp.status_code == 404
    assert _SAVED_SHA not in resp.text
    assert "2026-08-25" not in resp.text
    assert "savedHead" not in resp.text
    assert "savedAt" not in resp.text

    # The owner, same project, does get both — else the leak checks above would pass just
    # as well against a field never sent to anyone.
    owner_resp = await client.get(
        _STATUS.format(pid=app_row.project_id), headers=auth_headers(owner)
    )
    assert owner_resp.json()["savedHead"] == _SAVED_SHA
    assert owner_resp.json()["savedAt"] == "2026-08-25T14:20:00Z"


# --- WHY the saved pair is absent, which the pair itself cannot say ---------------------
#
# `savedHead`/`savedAt` both read null in three different situations, and until this unit
# the response could not tell them apart: one sentinel answered "no store bound", "the
# store would not answer" and "there is no bundle". All three are one answer to the DRIFT
# question — never spelled "up to date" — and three answers to "has this citizen ever
# saved". The rail could only render the union of them, so it told somebody who had never
# saved that their last save could not be found, on the panel they open precisely when
# they are unsure their work is safe.
#
# The invariant every test below re-checks: `publishState` is UNCHANGED by the split. If
# a mutation makes the drift answer move, that is the regression, not the fix.


class _StoreThatKnowsNothing(FakeStorage):
    """A store with no objects in it at all — the never-saved case, which is `head()`
    answering `None` rather than raising."""


async def test_a_project_that_never_saved_says_so_rather_than_saying_nothing(
    wire, client, db_session
) -> None:
    """The store answers and there is no bundle: `never_saved`, which is the one
    value on which the rail omits its LAST SAVED row entirely.

    Mutation receipt: fold the `meta is None` arm back into the storage-error sentinel and
    this goes red on `savedState` while every `publishState` assertion in the file stays
    green — which is exactly the shape of the bug, a fact nobody could see."""
    # `wire=None` so `_owner_with_app` seeds NOTHING at `snapshot_key` — the store is still
    # bound (the `wire` fixture binds it), it simply has no bundle for this app. That is the
    # never-saved case, and it is `head()` answering `None` rather than raising.
    user, app_row = await _owner_with_app(db_session)
    await db_session.commit()

    resp = await client.get(_STATUS.format(pid=app_row.project_id), headers=auth_headers(user))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["savedState"] == "never_saved"
    assert body["savedHead"] is None
    assert body["savedAt"] is None
    # The drift answer is untouched by the split.
    assert body["publishState"] == "draft"


async def test_a_storage_error_is_distinguishable_from_a_project_that_never_saved(
    wire, client, db_session
) -> None:
    """THE WHOLE POINT OF THE UNIT, asserted as a DIFFERENCE rather than as two values.

    A store that would not answer and a citizen who has never saved produce identical
    `savedHead`/`savedAt` (both null) and an identical `publishState`. If the two are ever
    equal on this axis too, the presenter is back to guessing — and the guess it has to
    make is the one that tells a citizen their work is missing.

    Mutation receipt: return the same `SavedState` from both arms of
    `_saved_version_for_publish_state` and this fails on the inequality, whichever value
    the mutation picks."""
    user, app_row = await _owner_with_app(db_session, wire)
    await _live_deployment(db_session, app_id=app_row.id, user_id=user.id)
    await db_session.commit()

    wire.app.dependency_overrides[storage_or_none_dependency] = lambda: _AlwaysBoomingStorage()
    refused = (
        await client.get(_STATUS.format(pid=app_row.project_id), headers=auth_headers(user))
    ).json()

    wire.app.dependency_overrides[storage_or_none_dependency] = lambda: _StoreThatKnowsNothing()
    empty = (
        await client.get(_STATUS.format(pid=app_row.project_id), headers=auth_headers(user))
    ).json()

    assert refused["savedState"] == "storage_error"
    assert empty["savedState"] == "never_saved"
    assert refused["savedState"] != empty["savedState"]
    # …and the two are otherwise INDISTINGUISHABLE, which is why the field had to exist.
    assert refused["savedHead"] == empty["savedHead"] is None
    assert refused["savedAt"] == empty["savedAt"] is None
    assert refused["publishState"] == empty["publishState"] == "live_drift_unknown"


async def test_an_unconfigured_store_reports_its_own_reason_not_the_citizens(
    app: FastAPI, client, db_session
) -> None:
    """The third arm, and the one a fixture makes invisible by construction: with storage
    always bound there is no request in which `storage is None`, so the branch is
    untestable rather than merely untested.

    NO `wire`, NO `fake_storage` — the unconfigured posture this route already documents
    as supported. The platform cannot see this citizen's saves at all, and must not report
    that as their absence."""
    assert storage_or_none_dependency not in app.dependency_overrides, (
        "this test is only meaningful with storage UNBOUND"
    )
    user = await UserFactory.create(db_session)
    app_row = await AppRegistryFactory.create(db_session, user_id=user.id)
    await db_session.commit()

    resp = await client.get(_STATUS.format(pid=app_row.project_id), headers=auth_headers(user))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["savedState"] == "store_unconfigured"
    assert body["savedState"] != "never_saved"
    assert body["publishState"] == "draft"


async def test_a_saved_project_reports_saved_with_its_real_timestamp(
    wire, client, db_session
) -> None:
    """The ordinary case, and the guard against a split that reports every project as
    never-saved: a bundle exists, so `saved` — with the date and the id the rail draws."""
    user, app_row = await _owner_with_app(db_session, wire)
    await db_session.commit()
    key = snapshot_key(app_row.id)
    wire.store.objects[key] = a_git_bundle(_SAVED_SHA)
    wire.store.meta[key] = {"head_sha": _SAVED_SHA}
    wire.store.mtimes[key] = _SAVED_AT

    resp = await client.get(_STATUS.format(pid=app_row.project_id), headers=auth_headers(user))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["savedState"] == "saved"
    assert body["savedHead"] == _SAVED_SHA
    assert body["savedAt"] == "2026-08-25T14:20:00Z"


async def test_an_unstamped_bundle_is_a_save_the_platform_cannot_describe(
    wire, client, db_session
) -> None:
    """THE TRAP IN THE MIDDLE. A bundle written before the metadata stamp existed has no
    head and — if the store lost its last-modified too — no date either, so both halves
    read exactly like a project that never saved. It is not one: the object is there. The
    citizen HAS saved and the platform cannot describe which version, which is the case
    "We could not tell" was written for and the case this three-way split deliberately
    leaves saying it.

    Mutation receipt: key `SavedState` off `head is None` instead of off the object's
    existence and this goes red while every other test in this section stays green."""
    user, app_row = await _owner_with_app(db_session, wire)
    await db_session.commit()
    key = snapshot_key(app_row.id)
    wire.store.objects[key] = a_git_bundle(_SAVED_SHA)
    wire.store.meta[key] = {}  # a present blob carrying no `head_sha` stamp
    wire.store.mtimes.pop(key, None)  # …and a store that does not know when, either

    resp = await client.get(_STATUS.format(pid=app_row.project_id), headers=auth_headers(user))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["savedState"] == "saved"
    assert body["savedHead"] is None
    assert body["savedAt"] is None


async def test_a_project_with_no_app_row_has_nothing_that_could_have_been_saved(
    wire, client, db_session
) -> None:
    """The one path that never reaches the store at all. `NOTHING_BUILT` is returned before
    any storage read, and it KNOWS: there is no app, so there is no bundle that could have
    been saved. `never_saved` rather than an "unknown" — the rail draws no saved row, which
    is the true thing, instead of one that cannot tell."""
    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user_id=user.id)
    await db_session.commit()

    resp = await client.get(_STATUS.format(pid=project.id), headers=auth_headers(user))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["publishState"] == "nothing_built"
    assert body["savedState"] == "never_saved"
