"""Save-and-publish, where the version moves under the answers (U10, R12/R13/R18).

The gate's rungs are pinned in `test_publish_gate.py` and the pipeline's own decisions in
`tests/services/deploy/test_service.py`. What lives here is the SEAM between them, which
neither of those files can see: a citizen presses "Save and publish", the save mints a
commit no review has ever looked at, the request answers 202 anyway — and the version that
actually goes live (or into the queue) is the one the pipeline extracted and checked.

So this file wires the REAL deploy pipeline behind the real route, with fakes only at the
outward edges (the image registry, ARM, the object store, and a reviewer that writes real
review rows without a model). A recording pipeline would prove the route said "defer" and
nothing whatsoever about what deferring costs the citizen.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi import FastAPI

from src.api.deps import storage_or_none_dependency
from src.api.v1.build_sessions.deps import sandbox_or_none_dependency, session_manager_dependency
from src.api.v1.deploy.deps import deploy_service_or_none
from src.db.models.app_registry import AppRegistry, AppStatus
from src.db.models.classification_review import ClassificationReviewStatus
from src.db.models.deployment import Deployment, DeploymentStatus
from src.services.build_sessions.manager import SaveOutcome, SessionManager
from src.services.classification import store as review_store
from src.services.classification.service import ReviewReadout
from src.services.deploy import service as service_module
from src.services.deploy.classification import CLASSIFICATION_KEYS
from src.services.deploy.service import DeployService
from src.services.storage import snapshot_key
from src.services.storage.snapshot_read import ExtractedSnapshot
from tests.api.v1.build_sessions.conftest import auth_headers
from tests.factories import AppRegistryFactory, UserFactory
from tests.fakes import FakeStorage, a_git_bundle
from tests.services.deploy.test_service import FakeAca, FakeImages

_DEPLOY = "/v1/projects/{pid}/deploy"
_STATUS = "/v1/projects/{pid}/deployment"

# The commit the SAVE mints, and the one the citizen's answers were written about.
_NEW = "ab" * 20
_ANSWERED_ABOUT = "cd" * 20


def _body(**yes: object) -> dict[str, Any]:
    answers: dict[str, object] = {
        "credentialsSecrets": False,
        "healthData": False,
        "personalInformation": False,
        "financialData": False,
        "confidentialBusinessData": False,
        "publicData": False,
    }
    answers.update(yes)
    return {"answers": answers, "saveFirst": True}


def _verdicts(**by_key: str) -> dict[str, Any]:
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


class _GatedReviewer:
    """The review runner's two verbs over the real review store, with a LATCH.

    The latch is what makes AE5 provable rather than plausible: while it is closed the
    review cannot finish, so a request that answers at all is a request that did not wait
    for one. If the route ever went back to waiting, this file would hang instead of
    quietly passing — which is why every request below is bounded by `_answer`."""

    def __init__(self) -> None:
        self.verdicts: dict[str, Any] | None = None
        self.latch = asyncio.Event()
        self.asked: list[str] = []

    async def start(self, db, *, app_id, user_id, head_sha, extracted=None):
        self.asked.append(head_sha)
        outcome = await review_store.claim(db, app_id=app_id, user_id=user_id, head_sha=head_sha)
        await self.latch.wait()
        await review_store.succeed(
            db,
            review_id=outcome.review.review_id,
            head_sha=head_sha,
            attempt=outcome.review.attempt,
            verdicts=self.verdicts if self.verdicts is not None else _verdicts(),
            evidence={"questions": {}, "scan_hits": [], "downgraded": []},
            answers_complete=True,
        )
        return outcome.review

    async def read(self, db, *, app_id):
        record = await review_store.get_for_app(db, app_id=app_id)
        return None if record is None else ReviewReadout(review=record, aged_out=False)


@pytest.fixture
async def wire(app: FastAPI, db_session, monkeypatch, tmp_path):
    """The real pipeline behind the real route: fakes only where the platform leaves the
    process (registry, ARM, object store, model)."""
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "package.json").write_text("{}")

    async def _extract(app_id, *, cache_root=None):
        # The tree the pipeline extracts IS the commit the save minted.
        return ExtractedSnapshot(app_id=app_id, head_sha=_NEW, root=tree)

    monkeypatch.setattr(service_module, "extract_snapshot", _extract)
    monkeypatch.setattr(
        service_module,
        "build_published_env",
        lambda db, *, app_id, project_id: _immediate(({"BIAL_APP_ID": str(app_id)}, None)),
    )
    monkeypatch.setattr(service_module, "_HEARTBEAT_S", 3600.0)
    monkeypatch.setattr(service_module, "_REVISION_POLL_S", 0.01)
    monkeypatch.setattr(service_module, "_REVIEW_POLL_S", 0.01)

    store = FakeStorage()
    # The pipeline reaches the store through the accessor (it has no request to hang a
    # dependency on); the route reaches it through the override below. Same fake, so the
    # queue copy and the gate's stamp read the same bytes.
    monkeypatch.setattr(service_module, "get_storage", lambda: store)

    @contextlib.asynccontextmanager
    async def _session():
        yield db_session

    reviewer = _GatedReviewer()
    pipeline = DeployService(
        session_factory=lambda: _session(),
        image_builder=FakeImages(),
        published_apps=FakeAca(),
        reviewer=reviewer,
    )

    app.dependency_overrides[storage_or_none_dependency] = lambda: store
    app.dependency_overrides[deploy_service_or_none] = lambda: pipeline
    app.dependency_overrides[sandbox_or_none_dependency] = lambda: object()
    app.dependency_overrides[session_manager_dependency] = lambda: SessionManager(
        session_factory=lambda: _session()
    )
    yield SimpleWire(store=store, pipeline=pipeline, reviewer=reviewer, tree=tree)
    # The safety net for a test that failed before it drained: a latched pipeline still
    # holding the shared test session would otherwise poison the NEXT test's connection.
    reviewer.latch.set()
    await pipeline.drain()


class SimpleWire:
    def __init__(self, *, store, pipeline, reviewer, tree) -> None:
        self.store = store
        self.pipeline = pipeline
        self.reviewer = reviewer
        self.tree = tree


async def _immediate(value):
    return value


def _dirty_workspace(monkeypatch, app_id: uuid.UUID, *, saved_head: str = _NEW) -> list[uuid.UUID]:
    """A workspace with unsaved work, whose save lands at `saved_head`. Returns the list
    the save records into, so a test can prove the save actually happened."""
    saved: list[uuid.UUID] = []

    class _Dirty:
        dirty = True
        saved_head = None

    async def _state() -> _Dirty:
        return _Dirty()

    async def _save(self, db, user, project_id, *, sandbox_client) -> SaveOutcome:
        saved.append(project_id)
        return SaveOutcome(app_id=app_id, head_sha=saved_head)

    monkeypatch.setattr(
        SessionManager,
        "project_save_state",
        lambda self, db, user, project_id, *, sandbox_client: _state(),
    )
    monkeypatch.setattr(SessionManager, "save_project_snapshot", _save)
    return saved


async def _owner_with_saved_app(db, store: FakeStorage, *, sha: str = _NEW, **overrides):
    """An owner and their app, whose SAVED bundle is already at `sha` — the state the
    workspace is in the instant after "Save and publish" performed its save."""
    user = await UserFactory.create(db)
    app_row = await AppRegistryFactory.create(db, user_id=user.id, **overrides)
    store.objects[snapshot_key(app_row.id)] = a_git_bundle(sha)
    store.meta[snapshot_key(app_row.id)] = {"head_sha": sha}
    return user, app_row


async def _stale_review(db, *, app_id: uuid.UUID, user_id: uuid.UUID) -> None:
    """A completed review of the version the citizen's answers were written about — the
    one rule 3a finds stamped a commit other than the one about to ship."""
    outcome = await review_store.claim(
        db, app_id=app_id, user_id=user_id, head_sha=_ANSWERED_ABOUT
    )
    await review_store.succeed(
        db,
        review_id=outcome.review.review_id,
        head_sha=_ANSWERED_ABOUT,
        attempt=outcome.review.attempt,
        verdicts=_verdicts(),
        evidence={"questions": {}, "scan_hits": [], "downgraded": []},
        answers_complete=True,
    )


async def _answer(coro):
    """Every request in this file is bounded. A route that waited for the re-check would
    otherwise hang the suite rather than fail it, and a hang reads as an infrastructure
    problem instead of the regression it would be."""
    return await asyncio.wait_for(coro, timeout=10)


# --- AE5: the request returns, and the pipeline checks what it saved ------------------


async def test_save_and_publish_answers_before_the_new_version_is_reviewed(
    wire, app, client, db_session, monkeypatch
) -> None:
    """AE5 — the review of the new version runs on the publish control's own progress,
    not inside the request. The latch is closed for the whole exchange below: the 202
    arrives while the re-check is provably still unfinished."""
    user, app_row = await _owner_with_saved_app(db_session, wire.store)
    await _stale_review(db_session, app_id=app_row.id, user_id=user.id)
    _dirty_workspace(monkeypatch, app_row.id)

    resp = await _answer(
        client.post(
            _DEPLOY.format(pid=app_row.project_id), headers=auth_headers(user), json=_body()
        )
    )

    assert resp.status_code == 202
    assert resp.json()["outcome"] == "started"
    # Still latched: no review of the new version has landed — the stored row is either
    # still the stale one or claimed-and-running for the commit just saved.
    record = await review_store.get_for_app(db_session, app_id=app_row.id)
    assert record is not None
    assert not (record.head_sha == _NEW and record.status is ClassificationReviewStatus.COMPLETE)

    wire.reviewer.latch.set()
    await wire.pipeline.drain()

    settled = await review_store.get_for_app(db_session, app_id=app_row.id)
    assert settled is not None
    assert settled.head_sha == _NEW
    assert settled.status is ClassificationReviewStatus.COMPLETE

    row = await db_session.get(Deployment, uuid.UUID(resp.json()["deploymentId"]))
    await db_session.refresh(row)
    assert row.status is DeploymentStatus.SUCCEEDED
    assert row.head_sha == _NEW
    fresh = await db_session.get(AppRegistry, app_row.id, populate_existing=True)
    assert fresh.status is AppStatus.DRAFT


async def test_the_re_check_is_about_the_version_the_save_minted(
    wire, app, client, db_session, monkeypatch
) -> None:
    """R12/R13 — the whole point of deferring: the review the pipeline runs is stamped the
    POST-save commit, not the one the citizen's form was filled in against."""
    user, app_row = await _owner_with_saved_app(db_session, wire.store)
    await _stale_review(db_session, app_id=app_row.id, user_id=user.id)
    saved = _dirty_workspace(monkeypatch, app_row.id)
    wire.reviewer.latch.set()

    await _answer(
        client.post(
            _DEPLOY.format(pid=app_row.project_id), headers=auth_headers(user), json=_body()
        )
    )
    await wire.pipeline.drain()

    assert saved == [app_row.project_id]
    assert wire.reviewer.asked == [_NEW]


async def test_publishing_with_nothing_unsaved_never_re_checks(
    wire, app, client, db_session, monkeypatch
) -> None:
    """The narrow branch stays narrow: with a current review and nothing to save there is
    no drift, so the pipeline neither re-checks nor spends a model run."""
    user, app_row = await _owner_with_saved_app(db_session, wire.store)
    outcome = await review_store.claim(
        db_session, app_id=app_row.id, user_id=user.id, head_sha=_NEW
    )
    await review_store.succeed(
        db_session,
        review_id=outcome.review.review_id,
        head_sha=_NEW,
        attempt=outcome.review.attempt,
        verdicts=_verdicts(),
        evidence={"questions": {}, "scan_hits": [], "downgraded": []},
        answers_complete=True,
    )

    class _Clean:
        dirty = False
        saved_head = _NEW

    monkeypatch.setattr(
        SessionManager,
        "project_save_state",
        lambda self, db, user, project_id, *, sandbox_client: _immediate(_Clean()),
    )

    resp = await _answer(
        client.post(
            _DEPLOY.format(pid=app_row.project_id), headers=auth_headers(user), json=_body()
        )
    )
    await wire.pipeline.drain()

    assert resp.status_code == 202
    assert wire.reviewer.asked == []
    row = await db_session.get(Deployment, uuid.UUID(resp.json()["deploymentId"]))
    await db_session.refresh(row)
    assert row.status is DeploymentStatus.SUCCEEDED


# --- AE5a: the version moved and so did the answer ------------------------------------


async def test_a_new_yes_on_the_saved_version_queues_it_instead_of_publishing(
    wire, app, client, db_session, monkeypatch
) -> None:
    """AE5a — end to end. The citizen answered No to everything about the previous version
    and pressed Save and publish; the version they just saved handles health data. Nothing
    is published, the app waits for an administrator at exactly that commit, and the
    status the control polls says why."""
    user, app_row = await _owner_with_saved_app(db_session, wire.store)
    await _stale_review(db_session, app_id=app_row.id, user_id=user.id)
    _dirty_workspace(monkeypatch, app_row.id)
    wire.reviewer.verdicts = _verdicts(health_data="yes")
    wire.reviewer.latch.set()

    resp = await _answer(
        client.post(
            _DEPLOY.format(pid=app_row.project_id), headers=auth_headers(user), json=_body()
        )
    )
    await wire.pipeline.drain()

    assert resp.status_code == 202  # the request had already answered
    fresh = await db_session.get(AppRegistry, app_row.id, populate_existing=True)
    assert fresh.status is AppStatus.PENDING
    assert fresh.source_commit_sha == _NEW
    assert fresh.declaration["drift"] == {
        "answeredAbout": _ANSWERED_ABOUT,
        "shipping": _NEW,
        "newlyRaised": ["health_data"],
        "routedBy": "pipeline_recheck",
    }

    status = await _answer(
        client.get(_STATUS.format(pid=app_row.project_id), headers=auth_headers(user))
    )
    assert status.json()["status"] == "failed"
    assert status.json()["failureCode"] == "routed_for_review"
    assert status.json()["url"] is None


async def test_a_queued_re_check_publishes_nothing(
    wire, app, client, db_session, monkeypatch
) -> None:
    """The replacement invariant, on the far side of the 202: a routed deploy leaves the
    app in the queue at exactly the version examined, and publishes NOTHING — no image, no
    container app, no URL."""
    user, app_row = await _owner_with_saved_app(db_session, wire.store)
    await _stale_review(db_session, app_id=app_row.id, user_id=user.id)
    _dirty_workspace(monkeypatch, app_row.id)
    wire.reviewer.verdicts = _verdicts(credentials_secrets="yes")
    wire.reviewer.latch.set()

    resp = await _answer(
        client.post(
            _DEPLOY.format(pid=app_row.project_id), headers=auth_headers(user), json=_body()
        )
    )
    await wire.pipeline.drain()

    row = await db_session.get(Deployment, uuid.UUID(resp.json()["deploymentId"]))
    await db_session.refresh(row)
    assert row.status is DeploymentStatus.FAILED
    assert row.failure_code == "routed_for_review"
    assert row.url is None
    assert row.image_digest is None
    assert row.container_app_name is None


# --- the pin, from the request's side -------------------------------------------------


async def test_a_save_landing_between_the_save_and_the_stamp_is_refused(
    wire, app, client, db_session, monkeypatch
) -> None:
    """Two readings of the same tree — the save's own answer and the snapshot blob's
    metadata stamp — are written by one operation, so a disagreement is a THIRD save
    landing in between. Refused before anything is claimed, copied or built."""
    user, app_row = await _owner_with_saved_app(db_session, wire.store, sha=_ANSWERED_ABOUT)
    await _stale_review(db_session, app_id=app_row.id, user_id=user.id)
    # The save reports the commit it made; the store already holds a different one.
    _dirty_workspace(monkeypatch, app_row.id, saved_head=_NEW)

    resp = await _answer(
        client.post(
            _DEPLOY.format(pid=app_row.project_id), headers=auth_headers(user), json=_body()
        )
    )

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "snapshot_moved"
    assert await db_session.scalar(sa.select(sa.func.count()).select_from(Deployment)) == 0
    fresh = await db_session.get(AppRegistry, app_row.id, populate_existing=True)
    assert fresh.status is AppStatus.DRAFT


async def test_the_gate_hands_the_pipeline_the_commit_it_examined(
    wire, app, client, db_session, monkeypatch
) -> None:
    """R18's mechanism, recorded: the deploy's own row names the commit that was reviewed
    and shipped, and it is the one the save minted rather than the one on the form."""
    user, app_row = await _owner_with_saved_app(db_session, wire.store)
    await _stale_review(db_session, app_id=app_row.id, user_id=user.id)
    _dirty_workspace(monkeypatch, app_row.id)
    wire.reviewer.latch.set()

    resp = await _answer(
        client.post(
            _DEPLOY.format(pid=app_row.project_id), headers=auth_headers(user), json=_body()
        )
    )
    await wire.pipeline.drain()

    row = await db_session.get(Deployment, uuid.UUID(resp.json()["deploymentId"]))
    await db_session.refresh(row)
    assert row.head_sha == _NEW
    assert row.status is DeploymentStatus.SUCCEEDED
