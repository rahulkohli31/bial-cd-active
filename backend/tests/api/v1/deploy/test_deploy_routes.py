"""The two deploy routes — the PLUMBING around the publish gate.

The 202 is the load-bearing assertion. A deploy runs for minutes and the edge gateway times
out at twenty seconds, so a route that waited for the result would 504 on deploys that are
in fact going fine — and the citizen would retry, and the second claim would 409, and the
platform would look broken while doing exactly the right thing.

The 503 test is the other one worth having: FastAPI resolves every `Depends` BEFORE the
route body runs, so a provider that RAISED when publishing is unconfigured would escape the
body's own error handling and surface as a 500 with the wrong envelope. Asserting the status
AND the envelope shape is what pins that.

THE DECISION ITSELF LIVES IN `test_publish_gate.py` (U9). This file covers what surrounds
it — the 202 contract, the in-flight 409, unsaved work, CSRF, owner scoping, and the
declaration reaching the pipeline. Every test here therefore seeds a clean stored review
for the saved version so the ladder lands on rule 7 (publish) and a failure in this file
is never the gate quietly routing. The old terminal-refusal tests are retired below, as
guards rather than deletions.
"""

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

    camelCase keys because that is the wire shape — asserting through the alias is the only
    way these tests would catch a `CamelModel` misconfiguration that a snake_case body would
    sail straight past thanks to `populate_by_name`."""
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


# All-No, score 0 — the ONE shape of declaration that auto-deploys post-issue-#115 (the
# gate runs LOW score = safe = auto-deploy, HIGH score = needs a human; see
# classification.py). Every test that is NOT about the gate sends this, so a failure
# elsewhere is never the gate quietly refusing.
#
# Carries a voluntary explanation (still scores 0 — `notes` isn't a category) so
# `test_the_declaration_is_handed_to_the_service_to_record` can assert `notes` reaches
# `service.start()`: `_NEEDS_REVIEW` can't cover that, since it 409s before `start()` is
# ever called, which is otherwise the only shape in which `notes` reaches the service at all.
_QUALIFIES: dict[str, object] = _body(notes="Reads the public flight board only.")

# 40 + 15 = 55: well above AUTO_DEPLOY_MAX_SCORE (0), so an explanation is obligatory too
# (issue #117 follow-up: notes-required is now tied to the same threshold). The case the
# gate tests below actually exercise a refusal with.
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
    # The gate resolves the shipping commit from the snapshot blob's metadata stamp and
    # reads the stored review by that commit, so storage is no longer optional plumbing
    # for a publish — an unbound store is a documented 503 on every branch (ASM21).
    app.dependency_overrides[storage_or_none_dependency] = lambda: store
    return SimpleNamespace(app=app, service=service, manager=manager, store=store)


async def _clean() -> CleanSaveState:
    return CleanSaveState()


async def _owner_with_app(db, wire=None):
    """An owner and their app, saved at `_HEAD_SHA` with a CLEAN stored review for it.

    Seeding the review is what keeps this file about the plumbing: with one present and
    all-No, the ladder lands on rule 7 and publishes, so a 202 here means the route
    worked rather than the gate having been bypassed. Tests that never reach the gate
    (owner scoping, CSRF) pass `wire=None` and skip the seeding."""
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
    empty project would quietly mint one and then fail on the missing snapshot.

    U15: this refusal is now CODED (`no_saved_build` — the pipeline's own name for the
    same fact, `FAIL_NO_SNAPSHOT`), so a client asserts on `error.code` rather than
    parsing this sentence."""
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
    row exists, but no snapshot has ever been written for it — a different code path
    from the test above (no app row at all), the same citizen-facing fact, and now the
    SAME machine code (U15's coded-refusal bullet)."""
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
    reaches the pipeline at all (a publish now needs the saved version and its review),
    then swapping in a refusing service — the 409 has to come from the CLAIM, not from
    the gate declining to get that far."""
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
    """A GUARD, not a deletion — this file's four gate tests collapse into this one.

    They pinned a 409 `classification_below_threshold` whose message named the score and
    told the citizen to "ask an administrator", and the behaviour they described was a
    dead end: nothing queued, nobody notified. U9 replaced it with the precedence ladder,
    so the same declaration that used to be refused is now ROUTED — a real queue entry
    an administrator will see — and the 409 that stood here would be the platform
    refusing to do the thing it now does.

    The one deliberately-kept assertion from the old block is the NOT-a-403 note:
    `chatErrors.ts` reads a 403 on this surface as "your session lapsed", so nothing
    here may answer 403. The ladder's own outcomes are pinned in `test_publish_gate.py`;
    what this test guards is that the retired code cannot come back."""
    user, app_row = await _owner_with_app(db_session, wire)

    resp = await client.post(
        _DEPLOY.format(pid=app_row.project_id),
        headers=auth_headers(user),
        json=_body(confidentialBusinessData=True, notes="Vendor contact list only."),
    )

    assert resp.status_code != 409
    assert resp.status_code != 403
    assert resp.json().get("error", {}).get("code") != "classification_below_threshold"
    # It ROUTED (the review is clean but the citizen's own weighted Yes stands, R9), so
    # the pipeline was correctly not started — the old refusal's one true assertion.
    assert resp.status_code == 200
    assert resp.json()["outcome"] == "routed_for_review"
    assert wire.service.started == []


async def test_a_routed_deploy_leaves_the_app_queued_at_the_version_examined(
    app, client, db_session, monkeypatch
) -> None:
    """THE REPLACEMENT INVARIANT (R13). The retired test here asserted "a refused deploy
    never saves the workspace", with the gate running before `_resolve_unsaved_work`.
    That ordering is deliberately reversed: the ladder's version-dependent rules must
    run against the POST-save commit, so a save-and-publish saves first.

    What replaces it is the property that actually protects the citizen: a routed deploy
    leaves the app in the queue at exactly the version examined, and publishes nothing.
    The save is no longer a side effect of a declined request — it is the thing they
    asked for."""
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
        # the pipeline as the expected commit (U10). The store's stamp is the same one.
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

    # The save happened — it is what was asked for — and the app is queued at exactly the
    # commit the gate examined, with nothing published.
    assert saved == [app_row.project_id]
    assert resp.status_code == 200
    assert resp.json()["outcome"] == "routed_for_review"
    assert resp.json()["commitSha"] == _HEAD_SHA
    assert service.started == []


async def test_a_weighted_declaration_without_an_explanation_is_still_a_422(
    wire, client, db_session
) -> None:
    """Incomplete, not refused — unchanged in status and meaning, moved in mechanism.

    It used to fire at the schema boundary from the citizen's own answers; it now fires
    inside the ladder, on the MERGED answers (ASM22), which is the only place the review
    can be taken into account. The distinction that mattered is preserved exactly: an
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
    # The only shape in which `notes` reaches `service.start()` at all: a voluntary
    # explanation on an otherwise-qualifying (score 0) declaration. `_NEEDS_REVIEW` can't
    # cover this — it 409s before `start()` is ever called.
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
    # U15: never deployed, never submitted — `draft`, not a lie borrowed from a
    # neighbouring state.
    assert body["publishState"] == "draft"


async def test_the_status_is_owner_scoped(wire, client, db_session) -> None:
    _owner, app_row = await _owner_with_app(db_session)
    stranger = await UserFactory.create(db_session, email="nosy@rvaiglobal.com")

    resp = await client.get(_STATUS.format(pid=app_row.project_id), headers=auth_headers(stranger))

    assert resp.status_code == 404


async def test_the_status_carries_the_apps_approval_state(wire, client, db_session) -> None:
    """U12: the pending state reaches BOTH citizen publish surfaces through this one
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
    # camelCase, because that is the wire shape the portal narrows — asserting through
    # the alias is the only way a `CamelModel` misconfiguration is caught here.
    assert resp.json()["approval"] == {
        "status": "pending",
        "approvedCommitSha": None,
        # Beside the pin, and NULL for the same reason it is: this app is pending, so
        # nobody has approved anything yet. The two are written together and are never
        # apart. Asserted as an exact dict on purpose — a field added to the wire without
        # a client that reads it should have to come through here.
        "approvedAt": None,
        "approvalRoute": "self_publish",
        "rejectionNote": "Explain where the vendor key is stored.",
        "submittedSha": _HEAD_SHA,
        "submittedAt": "2026-08-19T10:00:00Z",
    }
    # U15: PENDING wins outright, whatever a deployment row (there is none here) says.
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
    # U15: the only `PublishState` member with no approval block behind it.
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
    """The approved states name a DATE first and mute the build code beside it, because a
    date is what a person recognises — so the stamp has to reach the wire, not just the pin.

    It costs nothing: `approved_at` is a column on the registry row this route already
    selects in full. It is written in exactly one place, beside `approved_commit_sha`
    (`admin/router.py`'s `approve`), which is why the two are asserted together here.

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
    """A `DEPLOY__*`-less deployment is a SUPPORTED state, and this route used to 503 on it.

    Every field the response carries is a committed row, so the answer was always sitting in
    the database. Refusing to hand it over broke the one thing the citizen most needs when
    there is no pipeline: the ladder ROUTES without one (ASM10), so an app reaches an
    administrator, gets rejected with a note written for its developer — and the developer's
    "Review & approval" card rendered empty, because this is the only call that carries
    approval state.

    NO `wire` FIXTURE ON PURPOSE. That fixture binds a fake deploy service into
    `deploy_service_or_none`, which is exactly the configuration that hid the bug: with it
    always bound, the unconfigured branch is untestable by construction, not merely untested
    (`.claude/rules/testing.md`). This asserts the fixture-off baseline instead.

    U15: storage is UNBOUND here too (no `wire`, no `fake_storage`), which is exactly the
    other posture this route must tolerate — `_saved_head_for_publish_state` reads `None`
    from an unconfigured store the same way it reads one from a `StorageError`, so
    `publishState` still comes back rather than the route crashing on a `None` storage
    handle.
    """
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
    """The counterweight to the test above, and the reason it is safe.

    Reading status needs no pipeline; PUBLISHING does, and that refusal is load-bearing —
    without it this change would turn "publishing is switched off" into a silent no-op.

    IT USES `wire` AND THEN UNBINDS ONLY THE DEPLOY SERVICE. Written without the fixture it
    passed for the wrong reason: storage is checked FIRST, so an all-unbound request 503s as
    `storage_unavailable` and never reaches the pipeline branch at all — a green test
    asserting nothing about the thing it names. Everything else is wired, and the seeded
    all-No review carries the ladder to rule 7, so the only thing left to fail is the
    missing pipeline. The `code` assertion is what keeps the two 503s apart.
    """
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


# --- U15: the one computed publish state ----------------------------------------------


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
    """A store whose HEAD always raises — U15's named departure from the two shipped
    readers' 503 (ASM21)."""

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
    """U15's cost argument, pinned on the statement stream rather than trusted from a
    docstring. `latest_deployment` already issued the owner check, the `AppRegistry`
    select and `deployment_for_app`'s select before this unit; the ONLY I/O this unit
    may add is exactly one `storage.head()` — never a second SELECT, and never a
    `storage.get()` of the snapshot bytes (`build_sessions/manager.py:683-695` is the
    named anti-pattern this counts against). Asserted in BOTH directions: the head call
    happened once, and the download path never ran at all."""
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
    # Today's three reads (the project-ownership `get`, the `AppRegistry` select, and
    # `deployment_for_app`'s select) — unit U15 must not add a fourth.
    assert len(statements) == 3, f"expected exactly today's three SELECTs, got {statements}"
    assert store.head_calls == 1
    assert store.get_calls == 0, "the snapshot bytes must never be downloaded for this read"


async def test_a_storage_error_reading_the_saved_head_answers_200_not_503(
    wire, client, db_session
) -> None:
    """THE named departure from ASM21 (U15). `_shipping_head` and `classification`'s own
    reader both turn this exact exception into a 503 — correctly, because both are
    about to ACT on the bundle. This read only answers "is there newer work", and Plan G
    makes this endpoint the only publishing surface in the product, so a blob blip here
    must not blank the rest of the response: the approval block stays present, and the
    drift question alone falls back to `live_drift_unknown`."""
    user, app_row = await _owner_with_app(db_session, wire)
    await _live_deployment(db_session, app_id=app_row.id, user_id=user.id)
    app_row.status = AppStatus.PENDING
    app_row.rejection_note = "Explain the third-party API key."
    await db_session.commit()
    wire.app.dependency_overrides[storage_or_none_dependency] = lambda: _AlwaysBoomingStorage()

    resp = await client.get(_STATUS.format(pid=app_row.project_id), headers=auth_headers(user))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    # PENDING wins outright (see `compute_publish_state`'s ordering), so this particular
    # app does not itself land on `live_drift_unknown` — the point here is the STATUS
    # CODE and the fact that nothing else in the response went missing.
    assert body["publishState"] == "in_review"
    assert body["approval"]["rejectionNote"] == "Explain the third-party API key."


async def test_a_storage_error_on_a_live_app_reads_drift_unknown_not_current(
    wire, client, db_session
) -> None:
    """The mirror of the counting test above, with the store failing instead of
    answering: unknown must never be spelled "up to date" (L12's tri-state discipline)."""
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
