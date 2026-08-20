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
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from src.api.deps import storage_or_none_dependency
from src.api.v1.build_sessions.deps import (
    sandbox_dependency,
    sandbox_or_none_dependency,
    session_manager_dependency,
)
from src.api.v1.deploy.deps import deploy_service_or_none
from src.services.build_sessions.manager import SessionManager
from src.services.classification import store as review_store
from src.services.deploy.classification import CLASSIFICATION_KEYS
from src.services.deploy.service import DeployNotPossibleError, StartedDeploy
from src.services.storage import snapshot_key
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
            }
        )
        return StartedDeploy(deployment_id=uuid.uuid4(), app_id=app_id)


class CleanSaveState:
    """A save-state view with nothing outstanding."""

    dirty = False


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
    empty project would quietly mint one and then fail on the missing snapshot."""
    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user.id)

    resp = await client.post(
        _DEPLOY.format(pid=project.id), headers=auth_headers(user), json=_QUALIFIES
    )

    assert resp.status_code == 409
    assert "nothing to deploy" in resp.json()["error"]["message"].lower()


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

    async def _record_save(self, db, user, project_id, *, sandbox_client) -> None:
        saved.append(project_id)

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


async def test_the_status_is_owner_scoped(wire, client, db_session) -> None:
    _owner, app_row = await _owner_with_app(db_session)
    stranger = await UserFactory.create(db_session, email="nosy@rvaiglobal.com")

    resp = await client.get(_STATUS.format(pid=app_row.project_id), headers=auth_headers(stranger))

    assert resp.status_code == 404
