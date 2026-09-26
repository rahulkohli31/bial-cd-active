"""The two classification review routes: ensure and read, never the browser's copy.

WHY THIS EXISTS
The service under these routes is the REAL one with the run disarmed (`_run` records and
returns): claim-or-return, the attempt cap and the aged-out un-wedge are the genuine
store-backed logic — what a route test must not fake, because the routes' promises ("an
unchanged version returns the stored answers without a run") are promises about that
logic being REACHED — while the run itself is separately-tested territory, and an
inert run cannot interleave on the test's single DB session.

Two assertions carry the security posture and get deliberate setups:

* the cross-user 404 is proven WITH STORAGE UNBOUND — ownership must resolve before any
  storage question, or a stranger probing a project id learns from the 503 that the
  platform's storage is down (and, ordered the other way, a real project id would leak
  through timing);
* the storage override is proven TO BIND — the routes must consume the one shared
  `storage_or_none_dependency` key, because a split provider forks the override key and
  the fixture then binds a fake the route never sees (the documented green-tests trap).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa

import src.db.base as db_base
from src.api.deps import storage_or_none_dependency
from src.api.v1.classification.deps import review_service_dependency
from src.api.v1.classification.router import REVIEW_RATE_LIMIT
from src.db.models.app_registry import AppRegistry
from src.db.models.classification_config import ClassificationClass
from src.db.models.classification_review import ClassificationReview, ClassificationReviewStatus
from src.db.models.project import Project
from src.db.models.user import User
from src.db.session import get_db
from src.services.classification import store
from src.services.classification.config import load_live_config
from src.services.classification.constants import REVIEW_WALL_CLOCK_CEILING_S
from src.services.classification.service import (
    FAIL_ABANDONED,
    FAIL_BUNDLE_UNREADABLE,
    FAIL_REVIEW,
    FAIL_STORAGE,
    MAX_MODEL_RUNS_PER_VERSION,
    ClassificationReviewService,
)
from src.services.storage import snapshot_key
from tests.api.v1.build_sessions.conftest import auth_headers
from tests.factories import AppRegistryFactory, ProjectFactory, UserFactory
from tests.fakes import FakeStorage

_PATH = "/v1/projects/{pid}/classification-review"

_V1 = "a" * 40
_V2 = "b" * 40

# Markers seeded into the INTERNAL evidence document — if any of these strings ever
# appears in a response body, a location or a value leaked.
_SECRET_PATH = "src/lib/super-secret-auth.ts"
_SECRET_FAMILY = "stripe_live_leak_marker"


def _no_sessions() -> Any:
    raise AssertionError("the disarmed run must never open a session")


def _no_model() -> Any:
    raise AssertionError("the disarmed run must never build a model")


class NoRunService(ClassificationReviewService):
    """The real claim / cap / un-wedge logic with the RUN disarmed: `_run` records the
    claimed row and returns. `runs` is the started-a-run (or didn't) assertion."""

    def __init__(self) -> None:
        super().__init__(session_factory=_no_sessions, model_factory=_no_model)
        self.runs: list[store.ReviewRecord] = []

    async def _run(self, *, review, classes) -> None:
        self.runs.append(review)


class CountingStorage(FakeStorage):
    """FakeStorage that counts reads: `gets` is the poll-must-not-download assertion,
    `heads` is the override-actually-binds probe."""

    def __init__(self) -> None:
        super().__init__()
        self.gets = 0
        self.heads = 0

    async def get(self, key):
        self.gets += 1
        return await super().get(key)

    async def head(self, key):
        self.heads += 1
        return await super().head(key)


@pytest.fixture
def wire(app):
    """Both overrides bound through the SAME keys the routes consume — the storage one
    deliberately `storage_or_none_dependency` (never a fresh provider), see the module
    docstring."""
    service = NoRunService()
    storage = CountingStorage()
    app.dependency_overrides[review_service_dependency] = lambda: service
    app.dependency_overrides[storage_or_none_dependency] = lambda: storage
    return SimpleNamespace(service=service, storage=storage)


# --- seeding ------------------------------------------------------------------------


async def _owner_with_app(db):
    user = await UserFactory.create(db)
    app_row = await AppRegistryFactory.create(db, user_id=user.id)
    return user, app_row


async def _save_bundle(storage: FakeStorage, app_id, *, head_sha: str = _V1) -> None:
    """What a real Save leaves behind, as far as these routes can see: a blob whose
    metadata carries the commit stamp and whose `last_modified` is the save time."""
    await storage.put(snapshot_key(app_id), b"not-a-real-bundle", metadata={"head_sha": head_sha})


async def _live_keys(db) -> list[str]:
    return [entry.key for entry in (await load_live_config(db)).classes]


async def _fingerprint(db) -> str:
    return (await load_live_config(db)).fingerprint


async def _stored_verdicts(db) -> dict[str, Any]:
    """The runner's stored shape, for every live class."""
    answers: dict[str, Any] = {
        key: {"verdict": "no", "reason": "Nothing of this kind was found in the app."}
        for key in await _live_keys(db)
    }
    answers["credentials_keys"] = {
        "verdict": "yes",
        "reason": "The app's saved code contains what looks like a real sign-in secret.",
    }
    return {"classes": answers}


async def _stored_evidence(db) -> dict[str, Any]:
    """The INTERNAL half: cited locations and scan hits, marker-laden."""
    return {
        "classes": {
            key: ([{"path": _SECRET_PATH, "kind": "code"}] if key == "credentials_keys" else [])
            for key in await _live_keys(db)
        },
        "scan_hits": [{"path": _SECRET_PATH, "family": _SECRET_FAMILY, "tier": "a", "line": 12}],
    }


async def _complete_row(
    db, *, app_id, user_id, head_sha: str = _V1, fingerprint: str | None = None
) -> store.ReviewRecord:
    fingerprint = fingerprint or await _fingerprint(db)
    outcome = await store.claim(
        db, app_id=app_id, user_id=user_id, head_sha=head_sha, fingerprint=fingerprint
    )
    assert outcome.claimed is True
    settled = await store.succeed(
        db,
        review_id=outcome.review.review_id,
        head_sha=head_sha,
        fingerprint=fingerprint,
        attempt=outcome.review.attempt,
        verdicts=await _stored_verdicts(db),
        evidence=await _stored_evidence(db),
        answers_complete=True,
    )
    assert settled is True
    return outcome.review


async def _failed_row(
    db,
    *,
    app_id,
    user_id,
    head_sha: str = _V1,
    code: str = FAIL_REVIEW,
    times: int = 1,
) -> None:
    """`times` failed attempts, claimed and settled the way the real runner would —
    three of them is the attempt cap, reached legitimately rather than poked in."""
    fingerprint = await _fingerprint(db)
    for _ in range(times):
        outcome = await store.claim(
            db, app_id=app_id, user_id=user_id, head_sha=head_sha, fingerprint=fingerprint
        )
        assert outcome.claimed is True
        settled = await store.fail(
            db,
            review_id=outcome.review.review_id,
            head_sha=head_sha,
            fingerprint=fingerprint,
            attempt=outcome.review.attempt,
            code=code,
            detail="scripted failure",
        )
        assert settled is True


async def _row(db, *, app_id) -> ClassificationReview:
    row = (
        await db.execute(
            sa.select(ClassificationReview).where(ClassificationReview.app_id == app_id)
        )
    ).scalar_one()
    return row


# --- the ensure route ---------------------------------------------------------------


async def test_a_stored_complete_review_for_the_current_version_returns_without_a_run(
    wire, client, db_session
) -> None:
    """Re-opening the dialog for an unchanged version is a read, not a re-run."""
    user, app_row = await _owner_with_app(db_session)
    await _save_bundle(wire.storage, app_row.id, head_sha=_V1)
    await _complete_row(db_session, app_id=app_row.id, user_id=user.id, head_sha=_V1)

    resp = await client.post(_PATH.format(pid=app_row.project_id), headers=auth_headers(user))

    assert resp.status_code == 200  # settled — never the 202 poll invitation
    body = resp.json()
    assert body["status"] == "complete"
    assert body["headSha"] == _V1
    assert body["reviewedSha"] == _V1
    assert body["savedAt"] is not None
    assert body["checkedAt"] is not None
    assert body["current"] is True
    verdicts = body["verdicts"]
    assert set(verdicts) == set(await _live_keys(db_session))  # class keys, as stored
    assert verdicts["credentials_keys"]["verdict"] == "yes"
    assert verdicts["credentials_keys"]["reason"]
    assert verdicts["financial_data"]["verdict"] == "no"
    assert wire.service.runs == []


async def test_a_newer_version_starts_a_run_and_reports_running_with_the_new_stamp(
    wire, client, db_session
) -> None:
    """The stored answer is stamped V1; the blob says V2 — the stale answer must be
    replaced, never returned wearing the new version's dialog."""
    user, app_row = await _owner_with_app(db_session)
    await _complete_row(db_session, app_id=app_row.id, user_id=user.id, head_sha=_V1)
    await _save_bundle(wire.storage, app_row.id, head_sha=_V2)

    resp = await client.post(_PATH.format(pid=app_row.project_id), headers=auth_headers(user))

    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "running"
    assert body["headSha"] == _V2
    assert body["reviewedSha"] == _V2
    assert body["verdicts"] is None
    (run,) = wire.service.runs
    assert run.head_sha == _V2
    row = await _row(db_session, app_id=app_row.id)
    assert row.head_sha == _V2
    assert row.status is ClassificationReviewStatus.RUNNING
    assert row.attempt == 1  # the counter belongs to the version, and the version moved


async def test_an_app_with_no_saved_code_is_the_nothing_to_review_state(
    wire, client, db_session
) -> None:
    """With no bundle, both verbs return no answers, no run, and not an error."""
    user, app_row = await _owner_with_app(db_session)

    for call in (client.post, client.get):
        resp = await call(_PATH.format(pid=app_row.project_id), headers=auth_headers(user))
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "nothing_to_review"
        assert body["verdicts"] is None
        assert body["headSha"] is None
    assert wire.service.runs == []


async def test_a_project_with_no_app_is_also_nothing_to_review_and_mints_nothing(
    wire, client, db_session
) -> None:
    """The build path's resolver UPSERTS a draft app row; a review request must not."""
    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user.id)

    resp = await client.post(_PATH.format(pid=project.id), headers=auth_headers(user))

    assert resp.status_code == 200
    assert resp.json()["status"] == "nothing_to_review"
    minted = await db_session.scalar(
        sa.select(sa.func.count())
        .select_from(AppRegistry)
        .where(AppRegistry.project_id == project.id)
    )
    assert minted == 0


async def test_asking_again_after_a_failure_is_the_same_route_and_a_fresh_attempt(
    wire, client, db_session
) -> None:
    """Re-requesting after a failure is THIS route again, not a separate verb —
    and it claims attempt 2 rather than returning the stored failure."""
    user, app_row = await _owner_with_app(db_session)
    await _save_bundle(wire.storage, app_row.id, head_sha=_V1)
    await _failed_row(db_session, app_id=app_row.id, user_id=user.id, head_sha=_V1)

    resp = await client.post(_PATH.format(pid=app_row.project_id), headers=auth_headers(user))

    assert resp.status_code == 202
    assert resp.json()["status"] == "running"
    (run,) = wire.service.runs
    assert run.attempt == 2
    row = await _row(db_session, app_id=app_row.id)
    assert row.status is ClassificationReviewStatus.RUNNING
    assert row.attempt == 2


async def test_the_attempt_cap_returns_the_stored_failure_without_a_run(
    wire, client, db_session
) -> None:
    """The fourth ask: the cap is the review's real spend bound, and the answer is the
    stored failure — presented as not retryable, because the re-check button would only
    hand back this same row."""
    user, app_row = await _owner_with_app(db_session)
    await _save_bundle(wire.storage, app_row.id, head_sha=_V1)
    await _failed_row(
        db_session,
        app_id=app_row.id,
        user_id=user.id,
        head_sha=_V1,
        times=MAX_MODEL_RUNS_PER_VERSION,
    )

    resp = await client.post(_PATH.format(pid=app_row.project_id), headers=auth_headers(user))

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "failed"
    assert body["failureCode"] == FAIL_REVIEW
    assert body["retryable"] is False
    assert wire.service.runs == []
    row = await _row(db_session, app_id=app_row.id)
    assert row.attempt == MAX_MODEL_RUNS_PER_VERSION
    assert row.status is ClassificationReviewStatus.FAILED


async def test_a_burst_of_review_starts_is_rate_limited_per_user(wire, client, db_session) -> None:
    """The review's only PER-USER spend bound. Its daily-token exemption is deliberate,
    and `MAX_MODEL_RUNS_PER_VERSION` is per VERSION — and a version costs one Save —
    so without this a save/ensure loop mints premium-model runs without limit.

    Refusing here cannot open the gate: it only declines to START a review, and an app
    with no complete review for its version is routed to an administrator. The failure
    direction is toward a human."""
    user, app_row = await _owner_with_app(db_session)
    await _save_bundle(wire.storage, app_row.id)
    url = _PATH.format(pid=app_row.project_id)

    codes = [
        (await client.post(url, headers=auth_headers(user))).status_code
        for _ in range(REVIEW_RATE_LIMIT + 1)
    ]

    assert codes[-1] == 429
    assert codes[:-1] == [code for code in codes[:-1] if code != 429]  # only the last
    assert len(wire.service.runs) <= REVIEW_RATE_LIMIT  # the refusal reached the service


async def test_the_limiter_is_per_user_not_global(wire, client, db_session) -> None:
    """A second citizen is unaffected by the first's burst — the bucket is keyed by user,
    so one noisy developer cannot lock the publish dialog for everyone else."""
    noisy, noisy_app = await _owner_with_app(db_session)
    await _save_bundle(wire.storage, noisy_app.id)
    for _ in range(REVIEW_RATE_LIMIT + 1):
        await client.post(_PATH.format(pid=noisy_app.project_id), headers=auth_headers(noisy))

    quiet, quiet_app = await _owner_with_app(db_session)
    await _save_bundle(wire.storage, quiet_app.id)
    resp = await client.post(_PATH.format(pid=quiet_app.project_id), headers=auth_headers(quiet))

    assert resp.status_code != 429


async def test_csrf_is_required_on_the_ensure_route(wire, client, db_session) -> None:
    user, app_row = await _owner_with_app(db_session)
    await _save_bundle(wire.storage, app_row.id)

    resp = await client.post(
        _PATH.format(pid=app_row.project_id), headers=auth_headers(user, with_csrf=False)
    )

    assert resp.status_code == 403
    assert wire.service.runs == []


# --- the read route -----------------------------------------------------------------


async def test_a_failed_review_reads_as_its_bucket_with_no_answers(
    wire, client, db_session
) -> None:
    """The failure is a bucket, never readable as a set of No's."""
    user, app_row = await _owner_with_app(db_session)
    await _save_bundle(wire.storage, app_row.id, head_sha=_V1)
    await _failed_row(db_session, app_id=app_row.id, user_id=user.id, head_sha=_V1)

    resp = await client.get(_PATH.format(pid=app_row.project_id), headers=auth_headers(user))

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "failed"
    assert body["failureCode"] == FAIL_REVIEW
    assert body["failureMessage"] == "The automatic check couldn't run."
    assert body["retryable"] is True
    assert body["current"] is True
    assert body["checkedAt"] is not None
    assert body["verdicts"] is None
    assert wire.service.runs == []


async def test_polling_a_running_review_downloads_nothing_and_starts_nothing(
    wire, client, db_session
) -> None:
    """The poll target's whole contract: metadata reads only (the dialog polls for up
    to a minute — pulling the tree per poll is what the stamp exists to avoid), and a
    GET can never claim a run."""
    user, app_row = await _owner_with_app(db_session)
    await _save_bundle(wire.storage, app_row.id, head_sha=_V1)
    outcome = await store.claim(
        db_session,
        app_id=app_row.id,
        user_id=user.id,
        head_sha=_V1,
        fingerprint=await _fingerprint(db_session),
    )
    assert outcome.claimed is True

    for _ in range(3):
        resp = await client.get(_PATH.format(pid=app_row.project_id), headers=auth_headers(user))
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "running"
        assert body["reviewedSha"] == _V1

    assert wire.storage.gets == 0  # the bundle's bytes were never touched
    assert wire.service.runs == []
    row = await _row(db_session, app_id=app_row.id)
    assert row.status is ClassificationReviewStatus.RUNNING
    assert row.attempt == 1


async def test_an_aged_out_running_review_reads_as_abandoned_and_the_next_ask_unwedges(
    wire, client, db_session
) -> None:
    """A restart kills the runner and leaves the row RUNNING. The read presents that as
    review-abandoned (same sentence as review-failed, distinct code) WITHOUT writing
    anything; the next ensure settles the wedge and claims attempt 2."""
    user, app_row = await _owner_with_app(db_session)
    await _save_bundle(wire.storage, app_row.id, head_sha=_V1)
    outcome = await store.claim(
        db_session,
        app_id=app_row.id,
        user_id=user.id,
        head_sha=_V1,
        fingerprint=await _fingerprint(db_session),
    )
    assert outcome.claimed is True
    await db_session.execute(
        sa.update(ClassificationReview)
        .where(ClassificationReview.app_id == app_row.id)
        .values(started_at=datetime.now(UTC) - timedelta(seconds=REVIEW_WALL_CLOCK_CEILING_S + 60))
    )
    await db_session.commit()

    resp = await client.get(_PATH.format(pid=app_row.project_id), headers=auth_headers(user))

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "failed"
    assert body["failureCode"] == FAIL_ABANDONED
    assert body["failureMessage"] == "The automatic check couldn't run."
    assert body["retryable"] is True
    # The read settled nothing — the row still says RUNNING until an ensure un-wedges it.
    row = await _row(db_session, app_id=app_row.id)
    assert row.status is ClassificationReviewStatus.RUNNING

    resp = await client.post(_PATH.format(pid=app_row.project_id), headers=auth_headers(user))
    assert resp.status_code == 202
    assert resp.json()["status"] == "running"
    (run,) = wire.service.runs
    assert run.attempt == 2


async def test_saved_code_with_no_review_yet_reads_as_not_reviewed(
    wire, client, db_session
) -> None:
    user, app_row = await _owner_with_app(db_session)
    await _save_bundle(wire.storage, app_row.id, head_sha=_V1)

    resp = await client.get(_PATH.format(pid=app_row.project_id), headers=auth_headers(user))

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "not_reviewed"
    assert body["headSha"] == _V1
    assert body["savedAt"] is not None
    assert body["verdicts"] is None
    assert wire.service.runs == []


async def test_the_read_surfaces_both_stamps_when_the_stored_review_is_stale(
    wire, client, db_session
) -> None:
    """A Save landed after the review: the answer rides with its own stamp so the dialog
    can ignore it (it filters by the stamp the dialog asked for) — the read itself never
    starts the replacement run."""
    user, app_row = await _owner_with_app(db_session)
    await _complete_row(db_session, app_id=app_row.id, user_id=user.id, head_sha=_V1)
    await _save_bundle(wire.storage, app_row.id, head_sha=_V2)

    resp = await client.get(_PATH.format(pid=app_row.project_id), headers=auth_headers(user))

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "complete"
    assert body["reviewedSha"] == _V1
    assert body["headSha"] == _V2
    assert body["current"] is False
    assert body["verdicts"] is None
    assert wire.service.runs == []


async def test_a_bundle_without_a_version_stamp_reads_as_unreadable(
    wire, client, db_session
) -> None:
    """A pre-stamp bundle: the commit cannot be named without downloading the tree —
    which these routes must never do — so no review can be claimed for it. Presented as
    the unreadable bucket (a fresh Save writes the stamp; retrying cannot)."""
    user, app_row = await _owner_with_app(db_session)
    await wire.storage.put(snapshot_key(app_row.id), b"stampless-bundle", metadata={})

    for call in (client.post, client.get):
        resp = await call(_PATH.format(pid=app_row.project_id), headers=auth_headers(user))
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "failed"
        assert body["failureCode"] == FAIL_BUNDLE_UNREADABLE
        assert body["retryable"] is False
    assert wire.service.runs == []
    assert wire.storage.gets == 0


# --- the live configuration and the class definitions --------------------------------


async def test_both_verbs_carry_the_live_policy_and_classes_but_never_a_description(
    wire, client, db_session
) -> None:
    """The dialog renders and scores from this readout alone. A description is the reviewer's
    instruction and never reaches an owner."""
    user, app_row = await _owner_with_app(db_session)
    await _save_bundle(wire.storage, app_row.id, head_sha=_V1)
    await _complete_row(db_session, app_id=app_row.id, user_id=user.id, head_sha=_V1)
    descriptions = (await db_session.scalars(sa.select(ClassificationClass.description))).all()
    assert descriptions

    for call in (client.post, client.get):
        resp = await call(_PATH.format(pid=app_row.project_id), headers=auth_headers(user))
        assert resp.status_code == 200
        body = resp.json()
        assert body["policy"] == {"threshold": 100, "ownersCanChangeAnswers": True}
        assert [(c["key"], c["kind"], c["weight"]) for c in body["classes"]] == [
            ("pii", "hard_block", None),
            ("financial_data", "hard_block", None),
            ("credentials_keys", "scored", 20),
            ("confidential_business_data", "scored", 20),
            ("ai_usage", "scored", 20),
            ("integrations", "scored", 20),
            ("public_data", "scored", 20),
        ]
        assert body["classes"][0]["title"] == "PII"
        assert all(set(entry) == {"key", "title", "kind", "weight"} for entry in body["classes"])
        for description in descriptions:
            assert description not in resp.text


async def test_a_review_under_older_class_definitions_reads_not_current_without_answers(
    wire, client, db_session
) -> None:
    user, app_row = await _owner_with_app(db_session)
    await _save_bundle(wire.storage, app_row.id, head_sha=_V1)
    await _complete_row(
        db_session, app_id=app_row.id, user_id=user.id, head_sha=_V1, fingerprint="0" * 64
    )

    resp = await client.get(_PATH.format(pid=app_row.project_id), headers=auth_headers(user))

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "complete"
    assert body["reviewedSha"] == _V1
    assert body["headSha"] == _V1
    assert body["current"] is False
    assert body["verdicts"] is None
    assert wire.service.runs == []


async def test_turning_a_class_off_makes_the_stored_review_stale(wire, client, db_session) -> None:
    user, app_row = await _owner_with_app(db_session)
    await _save_bundle(wire.storage, app_row.id, head_sha=_V1)
    await _complete_row(db_session, app_id=app_row.id, user_id=user.id, head_sha=_V1)
    await db_session.execute(
        sa.update(ClassificationClass)
        .where(ClassificationClass.key == "public_data")
        .values(active=False)
    )

    resp = await client.get(_PATH.format(pid=app_row.project_id), headers=auth_headers(user))

    body = resp.json()
    assert body["current"] is False
    assert "public_data" not in [entry["key"] for entry in body["classes"]]
    assert "pii" in [entry["key"] for entry in body["classes"]]


async def test_asking_after_the_class_definitions_changed_claims_a_fresh_review(
    wire, client, db_session
) -> None:
    user, app_row = await _owner_with_app(db_session)
    await _save_bundle(wire.storage, app_row.id, head_sha=_V1)
    await _complete_row(
        db_session, app_id=app_row.id, user_id=user.id, head_sha=_V1, fingerprint="0" * 64
    )

    resp = await client.post(_PATH.format(pid=app_row.project_id), headers=auth_headers(user))

    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "running"
    assert body["current"] is True
    (run,) = wire.service.runs
    assert run.attempt == 1
    row = await _row(db_session, app_id=app_row.id)
    assert row.definitions_fingerprint == await _fingerprint(db_session)
    assert row.head_sha == _V1


# --- nothing from evidence ----------------------------------------------------------


async def test_the_response_never_carries_evidence_for_any_verdict(
    wire, client, db_session
) -> None:
    """On the wire: the stored evidence is marker-laden, and no marker — nor
    the admin-only fields of the verdicts document — may appear in either verb's body.
    The per-question projection is pinned to exactly {verdict, reason}."""
    user, app_row = await _owner_with_app(db_session)
    await _save_bundle(wire.storage, app_row.id, head_sha=_V1)
    await _complete_row(db_session, app_id=app_row.id, user_id=user.id, head_sha=_V1)

    for call in (client.post, client.get):
        resp = await call(_PATH.format(pid=app_row.project_id), headers=auth_headers(user))
        assert resp.status_code == 200
        text = resp.text
        assert _SECRET_PATH not in text
        assert _SECRET_FAMILY not in text
        for entry in resp.json()["verdicts"].values():
            assert set(entry) == {"verdict", "reason"}


# --- ownership and the storage seam -------------------------------------------------


async def test_a_strangers_project_id_is_a_non_leaking_404_even_with_storage_unbound(
    app, client, db_session
) -> None:
    """Ownership resolves FIRST: a cross-user id answers 404 — never a 403 (which
    confirms the project exists) and never the storage 503 (which would tell a stranger
    about this deployment's storage posture). Storage is deliberately left unbound, so
    an ordering regression would answer 503 here and fail loudly."""
    service = NoRunService()
    app.dependency_overrides[review_service_dependency] = lambda: service
    _owner, app_row = await _owner_with_app(db_session)
    stranger = await UserFactory.create(db_session, email="stranger@rvaiglobal.com")

    for call in (client.post, client.get):
        resp = await call(_PATH.format(pid=app_row.project_id), headers=auth_headers(stranger))
        assert resp.status_code == 404
    assert service.runs == []


async def test_storage_unconfigured_is_the_documented_503_from_the_body(
    app, client, db_session
) -> None:
    """The provider yields None rather than raising: a raising provider resolves BEFORE
    the route body and escapes its error handling as a 500 in the `{"detail"}` envelope.
    Both verbs must answer the documented 503 in the `{"error"}` shape instead."""
    service = NoRunService()
    app.dependency_overrides[review_service_dependency] = lambda: service
    user, app_row = await _owner_with_app(db_session)

    for call in (client.post, client.get):
        resp = await call(_PATH.format(pid=app_row.project_id), headers=auth_headers(user))
        assert resp.status_code == 503
        body = resp.json()
        assert body["error"]["code"] == FAIL_STORAGE
        assert "message" in body["error"]
    assert service.runs == []


async def test_the_storage_override_binds_through_the_shared_provider(
    wire, client, db_session
) -> None:
    """The fixture-binds assertion: the routes must consume `storage_or_none_dependency`
    itself. If a later change split off a second provider, this override would stop
    binding, the head-count would stay zero and the route would 503 — loudly."""
    user, app_row = await _owner_with_app(db_session)
    await _save_bundle(wire.storage, app_row.id, head_sha=_V1)

    resp = await client.get(_PATH.format(pid=app_row.project_id), headers=auth_headers(user))

    assert resp.status_code == 200
    assert wire.storage.heads >= 1


# --- concurrency --------------------------------------------------------------------


async def test_two_concurrent_opens_leave_one_row_and_both_carry_the_same_stamp(
    app, client
) -> None:
    """Two dialogs opened together: Postgres settles the claim, one row exists, and both
    answers name the same version. Runs on REAL committed rows with a fresh session per
    request — genuine concurrency is impossible on the suite's single savepointed
    session, and faking it here would fake the very race under test."""
    service = NoRunService()
    storage = CountingStorage()
    app.dependency_overrides[review_service_dependency] = lambda: service
    app.dependency_overrides[storage_or_none_dependency] = lambda: storage

    async def _fresh_db():
        async with db_base.async_session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _fresh_db

    async with db_base.async_session_factory() as setup:
        user = await UserFactory.create(setup)
        app_row = await AppRegistryFactory.create(setup, user_id=user.id)
        await setup.commit()
        user_id, app_id, project_id = user.id, app_row.id, app_row.project_id

    await storage.put(snapshot_key(app_id), b"not-a-real-bundle", metadata={"head_sha": _V1})
    headers = auth_headers(user)
    url = _PATH.format(pid=project_id)
    try:
        first, second = await asyncio.gather(
            client.post(url, headers=headers), client.post(url, headers=headers)
        )

        assert first.status_code == 202
        assert second.status_code == 202
        one, two = first.json(), second.json()
        assert one["status"] == two["status"] == "running"
        assert one["reviewedSha"] == two["reviewedSha"] == _V1
        # Exactly one of the two claimed the run; the other got the in-flight row back.
        assert len(service.runs) == 1
        async with db_base.async_session_factory() as check:
            rows = await check.scalar(
                sa.select(sa.func.count())
                .select_from(ClassificationReview)
                .where(ClassificationReview.app_id == app_id)
            )
            assert rows == 1
    finally:
        # These rows were REALLY committed (no savepoint to roll back) — remove them so
        # the shared test database does not accrete one orphan per run.
        async with db_base.async_session_factory() as cleanup:
            await cleanup.execute(
                sa.delete(ClassificationReview).where(ClassificationReview.app_id == app_id)
            )
            await cleanup.execute(sa.delete(AppRegistry).where(AppRegistry.id == app_id))
            await cleanup.execute(sa.delete(Project).where(Project.id == project_id))
            await cleanup.execute(sa.delete(User).where(User.id == user_id))
            await cleanup.commit()
