"""The deploy pipeline, end to end against fakes.

No Azure, no registry, no sandbox. What is under test is the ORDER and the OUTCOMES: that a
successful deploy leaves a settled row with a URL and tells the citizen, that each distinct
failure leaves a distinct code and a sentence they can act on, and — the one that would be
expensive to get wrong — that a failed deploy never claims success.

The sandbox assertions are the sharpest tests here. The pipeline must never provision or
restore one: `restore_from_snapshot` tears a container down BEFORE it pulls the bundle, and
a confirmed-absent snapshot falls through to a blank golden template that would build
cleanly, deploy successfully, and replace the citizen's app with the starter — with a green
checkmark on it.
"""

from __future__ import annotations

import contextlib
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
from pydantic import SecretStr

from src.core.errors import AppApiError
from src.db.models.app_registry import AppRegistry, ApprovalRoute, AppStatus
from src.db.models.audit import AuditLog
from src.db.models.deployment import Deployment, DeploymentStatus
from src.db.models.message import Message, MessageEntryKind
from src.services.approvals import submit as submit_module
from src.services.classification import store as review_store
from src.services.classification.service import ReviewReadout
from src.services.deploy import service as service_module
from src.services.deploy.aca_publish import RevisionState, _state_of
from src.services.deploy.classification import CLASSIFICATION_KEYS
from src.services.deploy.config import DeployConfig
from src.services.deploy.images import BuiltImage, ImageBuildError
from src.services.deploy.service import DeployNotPossibleError, DeployService, VersionRecheck
from src.services.storage import snapshot_key
from src.services.storage.snapshot_read import ExtractedSnapshot, NoAppYet
from tests.factories import AppRegistryFactory, ConversationFactory, UserFactory
from tests.fakes import FakeStorage, a_git_bundle

_DIGEST = "sha256:" + "cd" * 32
_HEAD = "a" * 40
_OLDER = "b" * 40


def _config() -> DeployConfig:
    values: dict[str, Any] = {
        "acr_server": "bialgenaicr.azurecr.io",
        "acr_name": "bialgenaicr",
        "acr_resource_group": "rg-acr",
        "acr_subscription_id": "sub-acr",
        "acr_username": "bialgenaicr",
        "acr_password": SecretStr("pw"),
        "subscription_id": "sub",
        "resource_group": "rg",
        "region": "centralindia",
        "managed_environment_name": "env",
        "ready_timeout_s": 2,
    }
    return DeployConfig(**values)


@dataclass
class FakeImages:
    """Records what it was asked to build; can be told to fail like the registry does."""

    digest: str = _DIGEST
    error: ImageBuildError | None = None
    contexts: list[bytes] = field(default_factory=list)

    async def build(self, *, app_id: uuid.UUID, deployment_id: uuid.UUID, context: bytes):
        self.contexts.append(context)
        if self.error is not None:
            raise self.error
        return BuiltImage(digest=self.digest, tag="citizen-apps/x:y", run_id="run1")

    async def aclose(self) -> None:
        return None


class ScriptedReviewer:
    """The review runner's two verbs, over the REAL review store.

    No model and no detached task — the run settles inside `start` — but everything the
    pipeline then reads is a genuine row written through `classification/store`, in U6's
    document shape. A reviewer that simply handed back a dataclass would green the
    re-check while proving nothing about how a stored review is read.

    It records what it was ASKED, which is where two of U10's obligations are pinned: the
    root it was handed (the pipeline's own extraction, never a second download) and the
    deployment's step at that moment (the re-check has a phase of its own)."""

    def __init__(self) -> None:
        self.verdicts: dict[str, Any] | None = None
        self.fail_code: str | None = None
        self.asked: list[dict[str, Any]] = []

    async def start(self, db, *, app_id, user_id, head_sha, extracted=None):
        self.asked.append(
            {
                "head_sha": head_sha,
                "root": None if extracted is None else extracted.root,
                "step": await db.scalar(
                    sa.select(Deployment.step).where(Deployment.app_id == app_id)
                ),
            }
        )
        outcome = await review_store.claim(db, app_id=app_id, user_id=user_id, head_sha=head_sha)
        if self.fail_code is not None:
            await review_store.fail(
                db,
                review_id=outcome.review.review_id,
                head_sha=head_sha,
                attempt=outcome.review.attempt,
                code=self.fail_code,
            )
        else:
            await review_store.succeed(
                db,
                review_id=outcome.review.review_id,
                head_sha=head_sha,
                attempt=outcome.review.attempt,
                verdicts=self.verdicts if self.verdicts is not None else review_doc(),
                evidence={"questions": {}, "scan_hits": [], "downgraded": []},
                answers_complete=True,
            )
        return outcome.review

    async def read(self, db, *, app_id):
        record = await review_store.get_for_app(db, app_id=app_id)
        return None if record is None else ReviewReadout(review=record, aged_out=False)


def review_doc(**by_key: str) -> dict[str, Any]:
    """A stored verdicts document in U6's exact shape, all-No unless told otherwise."""
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


def declaration(*, citizen_yes: tuple[str, ...] = (), merged_yes: tuple[str, ...] = ()):
    """The gate's declaration for a submitted decision, in `deploy/gate`'s shape — only
    the two blocks the re-check reads back (the citizen's answers, and the merged answers
    that are its baseline for "was this Yes already there")."""
    return {
        "commits": {"shipping": _HEAD, "reviewed": None},
        "citizen": {
            "answers": {key: key in citizen_yes for key in CLASSIFICATION_KEYS},
            "explanation": "Reads the public flight board only.",
        },
        "merged": {
            "answers": {key: key in merged_yes for key in CLASSIFICATION_KEYS},
            "anyWeightedYes": bool(merged_yes),
        },
        "differences": {},
    }


class FakeAca:
    """Records every provision, and reports whatever revision state it is told to."""

    def __init__(self, *, healthy: bool = True, failed: bool = False) -> None:
        self.config = _config()
        self.created: list[dict[str, Any]] = []
        self._healthy = healthy
        self._failed = failed

    async def create_or_update(self, *, app_id, deployment_id, image, env, container_url) -> str:
        self.created.append(
            {
                "app_id": app_id,
                "image": image,
                "env": env,
                "container_url": container_url,
            }
        )
        return f"pub-{app_id.hex[:28]}.example.azurecontainerapps.io"

    async def get_revision(self, *, app_id, deployment_id) -> RevisionState:
        # Built through `_state_of`, exactly as the real client does. ARM hands back ENUM
        # members whose `str()` is `RevisionProvisioningState.PROVISIONED`, so a fake that
        # hand-wrote the tidy string would stop modelling the thing under test — which is
        # how the enum bug reached a live deploy in the first place.
        raw = "Failed" if self._failed else ("Provisioned" if self._healthy else "Provisioning")
        return RevisionState(
            name="rev", provisioning_state=_state_of(raw), running_state=_state_of("Running")
        )


@pytest.fixture
def wire(db_session, monkeypatch, tmp_path):
    """A service whose every outward edge is a fake, and whose sessions are the rolled-back
    test session — so the pipeline's own short sessions land in the same transaction the test
    can read and the fixture discards."""
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "package.json").write_text("{}")

    async def _extract(app_id, *, cache_root=None):
        return ExtractedSnapshot(app_id=app_id, head_sha=_HEAD, root=tree)

    monkeypatch.setattr(service_module, "extract_snapshot", _extract)
    # The published env is exercised in its own tests; here it must not reach Azure.
    monkeypatch.setattr(
        service_module,
        "build_published_env",
        lambda db, *, app_id, project_id: _immediate(({"BIAL_APP_ID": str(app_id)}, None)),
    )
    # A heartbeat every 20s would never fire inside a test; make the absence explicit.
    monkeypatch.setattr(service_module, "_HEARTBEAT_S", 3600.0)
    monkeypatch.setattr(service_module, "_REVISION_POLL_S", 0.01)

    @contextlib.asynccontextmanager
    async def _session():
        yield db_session

    # The queue the drift re-check routes into reads the snapshot through the storage
    # accessor, not through a dependency — the pipeline has no request to hang one on.
    store = FakeStorage()
    monkeypatch.setattr(service_module, "get_storage", lambda: store)
    monkeypatch.setattr(service_module, "_REVIEW_POLL_S", 0.01)

    images = FakeImages()
    aca = FakeAca()
    reviewer = ScriptedReviewer()
    return SimpleNamespace(
        service=DeployService(
            session_factory=lambda: _session(),
            image_builder=images,
            published_apps=aca,
            reviewer=reviewer,
        ),
        images=images,
        aca=aca,
        reviewer=reviewer,
        store=store,
        tree=tree,
    )


async def _immediate(value):
    return value


async def _project(db):
    user = await UserFactory.create(db)
    app = await AppRegistryFactory.create(db, user_id=user.id)
    conversation = await ConversationFactory.create(db, user_id=user.id, project_id=app.project_id)
    return user, app, conversation


async def _run(wire, db, user, app, conversation_id=None, **extra):
    started = await wire.service.start(
        db,
        user_id=user.id,
        app_id=app.id,
        project_id=app.project_id,
        conversation_id=conversation_id,
        **extra,
    )
    await wire.service.drain()
    row = await db.get(Deployment, started.deployment_id)
    await db.refresh(row)
    return started, row


async def _saved_bundle(wire, app, sha: str = _HEAD) -> None:
    """The immutable copy the queue forks is made from the app's saved bundle, so a routed
    re-check needs a real one in the store."""
    wire.store.objects[snapshot_key(app.id)] = a_git_bundle(sha)
    wire.store.meta[snapshot_key(app.id)] = {"head_sha": sha}


async def _gate_rows(db, app_id) -> list[AuditLog]:
    rows = await db.execute(
        sa.select(AuditLog).where(
            AuditLog.resource_type == "app",
            AuditLog.resource_id == str(app_id),
            AuditLog.action == "publish_gate",
        )
    )
    return list(rows.scalars().all())


# --- the happy path ---------------------------------------------------------------


async def test_a_deploy_settles_with_a_url(wire, db_session) -> None:
    user, app, _conversation = await _project(db_session)

    _started, row = await _run(wire, db_session, user, app)

    assert row.status is DeploymentStatus.SUCCEEDED
    assert row.url.startswith("https://pub-")
    assert row.step == "live"
    assert row.finished_at is not None
    assert row.failure_code is None


async def test_the_commit_that_went_live_is_recorded(wire, db_session) -> None:
    """The one question nothing else in the schema can answer."""
    user, app, _conversation = await _project(db_session)
    _started, row = await _run(wire, db_session, user, app)
    assert row.head_sha == _HEAD


async def test_the_image_is_recorded_and_digest_pinned(wire, db_session) -> None:
    user, app, _conversation = await _project(db_session)
    _started, row = await _run(wire, db_session, user, app)

    assert row.image_digest == _DIGEST
    assert row.acr_run_id == "run1"
    # The container spec references the digest, never a tag.
    assert wire.aca.created[0]["image"].endswith(f"@{_DIGEST}")


async def test_the_citizen_is_told_where_the_app_is(wire, db_session) -> None:
    user, app, conversation = await _project(db_session)
    _started, row = await _run(wire, db_session, user, app, conversation.id)

    message = await db_session.scalar(
        sa.select(Message).where(
            Message.conversation_id == conversation.id,
            Message.entry_kind == MessageEntryKind.SYSTEM_EVENT,
        )
    )
    assert message is not None
    assert message.meta["kind"] == "deploy_outcome"
    assert message.meta["status"] == "succeeded"
    assert row.url in message.meta["url"]


async def test_a_deploy_with_no_conversation_still_succeeds(wire, db_session) -> None:
    """An app built through an API-only path has no thread. That is a deploy with no chat
    message, not a failed deploy."""
    user, app, _conversation = await _project(db_session)
    _started, row = await _run(wire, db_session, user, app, None)
    assert row.status is DeploymentStatus.SUCCEEDED


# --- the sandbox boundary ---------------------------------------------------------


async def test_the_pipeline_never_provisions_or_restores_a_sandbox(wire, db_session) -> None:
    """`restore_from_snapshot` tears the container down BEFORE it pulls the bundle, and a
    confirmed-absent snapshot falls through to a blank golden template — which would build,
    deploy, and replace the citizen's app with the starter under a green checkmark. The
    pipeline reads the bundle from object storage and leaves the sandbox alone."""
    user, app, _conversation = await _project(db_session)
    await _run(wire, db_session, user, app)

    # The service was constructed with no sandbox client at all — if the pipeline ever grew
    # a dependency on one, it could not have run.
    assert not hasattr(wire.service, "_sandbox")


# --- failures ---------------------------------------------------------------------


async def test_a_build_failure_is_reported_with_the_error_the_agent_can_fix(
    wire, db_session
) -> None:
    user, app, conversation = await _project(db_session)
    wire.images.error = ImageBuildError(
        "the image build failed",
        log_tail=(
            "   ▲ Next.js 16.2.10\n"
            "Failed to compile.\n\n"
            "./app/page.tsx:12:5\n"
            "Type error: Property 'foo' does not exist on type 'Item'.\n"
        ),
    )

    _started, row = await _run(wire, db_session, user, app, conversation.id)

    assert row.status is DeploymentStatus.FAILED
    assert row.failure_code == "build_failed"
    assert row.url is None

    message = await db_session.scalar(
        sa.select(Message).where(Message.conversation_id == conversation.id)
    )
    # The TITLE is the actionable line, not the Next.js banner.
    assert "Type error:" in message.payload[0]["parts"][0]["content"]
    assert "Next.js 16.2.10" not in message.payload[0]["parts"][0]["content"]


async def test_a_failed_deploy_says_the_previous_version_still_runs(wire, db_session) -> None:
    user, app, conversation = await _project(db_session)
    wire.images.error = ImageBuildError("boom", log_tail="Failed to compile.\nType error: x\n")

    await _run(wire, db_session, user, app, conversation.id)

    message = await db_session.scalar(
        sa.select(Message).where(Message.conversation_id == conversation.id)
    )
    assert "previous version is still running" in message.payload[0]["parts"][0]["content"]


async def test_a_build_failure_with_no_log_still_reports_something_useful(
    wire, db_session
) -> None:
    user, app, _conversation = await _project(db_session)
    wire.images.error = ImageBuildError("the image build timed out", log_tail=None)

    _started, row = await _run(wire, db_session, user, app)

    assert row.failure_code == "build_failed"
    assert "timed out" in (row.failure_detail or "")


async def test_nothing_saved_yet_is_a_named_outcome(wire, db_session, monkeypatch) -> None:
    """ "Never built" is a normal state, not a crash — and it must not read the same as a
    bundle that exists but cannot be parsed."""

    async def _absent(app_id, *, cache_root=None):
        return NoAppYet(app_id=app_id)

    monkeypatch.setattr(service_module, "extract_snapshot", _absent)
    user, app, _conversation = await _project(db_session)

    _started, row = await _run(wire, db_session, user, app)

    assert row.failure_code == "no_saved_build"
    assert row.status is DeploymentStatus.FAILED


async def test_an_unhealthy_revision_fails_the_deploy(wire, db_session) -> None:
    """`create_or_update` returning an FQDN proves the APP exists, not that the new REVISION
    came up. Without this check a deploy reports success over a URL that 5xx's."""
    user, app, _conversation = await _project(db_session)
    wire.aca._failed = True

    _started, row = await _run(wire, db_session, user, app)

    assert row.status is DeploymentStatus.FAILED
    assert row.failure_code == "revision_unhealthy"
    assert row.url is None


async def test_a_revision_that_never_becomes_healthy_is_bounded(wire, db_session) -> None:
    user, app, _conversation = await _project(db_session)
    wire.aca._healthy = False

    _started, row = await _run(wire, db_session, user, app)

    assert row.failure_code == "revision_unhealthy"


async def test_an_unexpected_crash_still_settles_the_row(wire, db_session, monkeypatch) -> None:
    """A pipeline that raised would leave the row `running` until the stale window expires —
    half an hour of a Deploy button that 409s, with nothing to explain it."""

    async def _boom(app_id, *, cache_root=None):
        raise RuntimeError("something nobody anticipated")

    monkeypatch.setattr(service_module, "extract_snapshot", _boom)
    user, app, _conversation = await _project(db_session)

    _started, row = await _run(wire, db_session, user, app)

    assert row.status is DeploymentStatus.FAILED
    assert row.failure_code == "internal_error"


async def test_a_secret_in_a_failure_detail_is_redacted(wire, db_session) -> None:
    user, app, _conversation = await _project(db_session)
    wire.images.error = ImageBuildError(
        "boom",
        log_tail="Failed to compile.\nError: DB_PASSWORD=hunter2 was rejected\n",
    )

    _started, row = await _run(wire, db_session, user, app)

    assert "hunter2" not in (row.failure_detail or "")


# --- U10: the expected commit -----------------------------------------------------


async def test_a_deploy_whose_tree_is_not_the_examined_commit_fails_closed(
    wire, db_session
) -> None:
    """THE PIN (R12/R13). The gate decided about one commit; a save landed before the
    pipeline extracted, so the tree is another. Publishing it would put unexamined code
    behind a decision made about something else — the single thing this feature exists to
    prevent — so it fails closed and nothing is built."""
    user, app, conversation = await _project(db_session)

    _started, row = await _run(
        wire, db_session, user, app, conversation.id, expected_commit_sha=_OLDER
    )

    assert row.status is DeploymentStatus.FAILED
    assert row.failure_code == "snapshot_moved"
    assert wire.images.contexts == []  # never packed, never built
    assert wire.aca.created == []
    message = await db_session.scalar(
        sa.select(Message).where(Message.conversation_id == conversation.id)
    )
    assert "saved again" in message.payload[0]["parts"][0]["content"]


async def test_the_expected_commit_matching_the_tree_publishes_normally(wire, db_session) -> None:
    """The assertion is a guard, not a gate: the commit the gate examined IS the tree."""
    user, app, _conversation = await _project(db_session)

    _started, row = await _run(wire, db_session, user, app, expected_commit_sha=_HEAD)

    assert row.status is DeploymentStatus.SUCCEEDED
    assert row.head_sha == _HEAD


async def test_a_publish_with_no_drift_never_asks_for_a_review(wire, db_session) -> None:
    """No unsaved work means no drift, so the ladder already had a current review and the
    pipeline skips the re-check entirely — no model, no extra step, no cost."""
    user, app, _conversation = await _project(db_session)

    _started, row = await _run(wire, db_session, user, app, expected_commit_sha=_HEAD)

    assert wire.reviewer.asked == []
    assert row.status is DeploymentStatus.SUCCEEDED


# --- U10: the drift re-check ------------------------------------------------------


async def test_a_re_checked_version_the_review_agrees_with_goes_live(wire, db_session) -> None:
    """AE5 — the save-and-publish happy path. The new version's review raises nothing the
    submitted answers did not already carry, so publishing continues to a live URL."""
    user, app, _conversation = await _project(db_session)
    await _saved_bundle(wire, app)

    _started, row = await _run(
        wire,
        db_session,
        user,
        app,
        expected_commit_sha=_HEAD,
        recheck=VersionRecheck(answered_about=_OLDER, declaration=declaration()),
    )

    assert row.status is DeploymentStatus.SUCCEEDED
    assert row.url.startswith("https://pub-")
    fresh = await db_session.get(AppRegistry, app.id, populate_existing=True)
    assert fresh.status is AppStatus.DRAFT  # never entered the queue


async def test_the_re_check_reads_the_tree_the_pipeline_already_extracted(
    wire, db_session
) -> None:
    """The review does not download and clone a second copy of the same commit in the same
    minute — it is handed the pipeline's own root. And because it never deletes a root it
    did not create, that root is still there to be packed afterwards."""
    user, app, _conversation = await _project(db_session)
    await _saved_bundle(wire, app)

    await _run(
        wire,
        db_session,
        user,
        app,
        expected_commit_sha=_HEAD,
        recheck=VersionRecheck(answered_about=_OLDER, declaration=declaration()),
    )

    assert wire.reviewer.asked[0]["root"] == wire.tree
    assert wire.reviewer.asked[0]["head_sha"] == _HEAD
    assert wire.tree.exists()
    assert wire.images.contexts  # the packing step still had its files


async def test_the_re_check_runs_under_a_step_of_its_own(wire, db_session) -> None:
    """The progress control must be able to NAME the wait. `checking` is advanced before
    the review is asked and before anything is packed, so the citizen is not left watching
    a generic label (or the previous phase) through the longest part of the deploy."""
    user, app, _conversation = await _project(db_session)
    await _saved_bundle(wire, app)

    await _run(
        wire,
        db_session,
        user,
        app,
        expected_commit_sha=_HEAD,
        recheck=VersionRecheck(answered_about=_OLDER, declaration=declaration()),
    )

    assert wire.reviewer.asked[0]["step"] == service_module.STEP_CHECKING
    # Its own phase, distinct from every other one — a duplicate would render as the
    # wrong sentence rather than as a missing one.
    assert service_module.STEP_CHECKING not in {
        service_module.STEP_PACKING,
        service_module.STEP_BUILDING,
        service_module.STEP_PROVISIONING,
        service_module.STEP_STARTING,
    }


async def test_a_new_yes_stops_publishing_and_queues_that_exact_version(wire, db_session) -> None:
    """AE5a — the re-check raises a weighted Yes the submitted answers lacked. Publishing
    stops, the app is queued at the commit that was examined, and the citizen is told what
    changed in the words they saw on the form."""
    user, app, conversation = await _project(db_session)
    await _saved_bundle(wire, app)
    wire.reviewer.verdicts = review_doc(health_data="yes")

    _started, row = await _run(
        wire,
        db_session,
        user,
        app,
        conversation.id,
        expected_commit_sha=_HEAD,
        recheck=VersionRecheck(answered_about=_OLDER, declaration=declaration()),
    )

    assert row.status is DeploymentStatus.FAILED
    assert row.failure_code == "routed_for_review"
    assert row.url is None
    assert wire.images.contexts == []  # stopped BEFORE packing
    assert wire.aca.created == []

    fresh = await db_session.get(AppRegistry, app.id, populate_existing=True)
    assert fresh.status is AppStatus.PENDING
    assert fresh.source_commit_sha == _HEAD  # the version examined, not a later one
    assert fresh.approval_route is ApprovalRoute.SELF_PUBLISH

    message = await db_session.scalar(
        sa.select(Message).where(Message.conversation_id == conversation.id)
    )
    assert "Health Data" in message.payload[0]["parts"][0]["content"]


async def test_the_queued_declaration_carries_the_drift_facts(wire, db_session) -> None:
    """U13 renders the distinction this block exists for: the citizen's answers — and the
    explanation R10 compelled — were written about ANOTHER commit, and nobody was at the
    form when this version was examined."""
    user, app, _conversation = await _project(db_session)
    await _saved_bundle(wire, app)
    wire.reviewer.verdicts = review_doc(health_data="yes")

    await _run(
        wire,
        db_session,
        user,
        app,
        expected_commit_sha=_HEAD,
        recheck=VersionRecheck(answered_about=_OLDER, declaration=declaration()),
    )

    fresh = await db_session.get(AppRegistry, app.id, populate_existing=True)
    drift = fresh.declaration["drift"]
    assert drift["answeredAbout"] == _OLDER
    assert drift["shipping"] == _HEAD
    assert drift["newlyRaised"] == ["health_data"]
    assert drift["routedBy"] == "pipeline_recheck"
    # U9's shape is kept whole, and `reviewed` is now the version actually reviewed.
    assert fresh.declaration["commits"] == {"shipping": _HEAD, "reviewed": _HEAD}
    assert fresh.declaration["merged"]["answers"]["health_data"] is True
    assert fresh.declaration["citizen"]["explanation"] is not None
    # U13's plain-language reasons come from THIS re-check, about the version actually
    # queued — and the row they were read from is overwritten by the citizen's very next
    # save, so the admin screen has no other correct source for them.
    assert fresh.declaration["review"]["reasons"]["health_data"] == (
        "Plain-language reason for health_data."
    )


async def test_the_pipeline_records_its_own_gate_decision(wire, db_session) -> None:
    """R22 — every gate decision is on record, including the ones made minutes after the
    request that started them, under the same action and the same actor."""
    user, app, _conversation = await _project(db_session)
    await _saved_bundle(wire, app)
    wire.reviewer.verdicts = review_doc(health_data="yes")

    _started, _row = await _run(
        wire,
        db_session,
        user,
        app,
        expected_commit_sha=_HEAD,
        recheck=VersionRecheck(answered_about=_OLDER, declaration=declaration()),
    )

    (audit,) = await _gate_rows(db_session, app.id)
    assert audit.detail is not None
    assert audit.actor_id == user.id
    assert audit.detail["decision"] == "routed"
    assert audit.detail["rule"] == "recheck_weighted_yes"
    assert audit.detail["commitSha"] == _HEAD
    assert audit.detail["declaration"]["drift"]["newlyRaised"] == ["health_data"]


async def test_a_review_that_clears_a_yes_the_citizen_declared_still_routes(
    wire, db_session
) -> None:
    """A review No does NOT clear a citizen Yes — it never has anywhere else in this
    feature, and this branch is no exception.

    THIS TEST ASSERTED THE OPPOSITE UNTIL THE BYPASS WAS FOUND, on the plan's U10 scenario
    "the new version's review clears a Yes the citizen declared — publishing continues".
    That scenario contradicts two things the plan itself fixes harder: its own merge table,
    where citizen Yes + review No merges to Yes with `citizen_yes_over_review_no` recorded
    (a review can never talk a citizen out of their own declaration — ASM17: the merge only
    ever ADDS routing), and ladder rule 6, which this branch stands in for. Publishing here
    would mean a weighted Yes reached a live URL with no administrator, which is the whole
    thing the feature exists to prevent. So it routes, and the disagreement travels with
    it for the administrator to rule on."""
    user, app, conversation = await _project(db_session)
    await _saved_bundle(wire, app)
    wire.reviewer.verdicts = review_doc()  # the review now says No to everything

    _started, row = await _run(
        wire,
        db_session,
        user,
        app,
        conversation.id,
        expected_commit_sha=_HEAD,
        recheck=VersionRecheck(
            answered_about=_OLDER,
            declaration=declaration(citizen_yes=("health_data",), merged_yes=("health_data",)),
        ),
    )

    assert row.status is DeploymentStatus.FAILED
    assert row.failure_code == "routed_for_review"
    fresh = await db_session.get(AppRegistry, app.id, populate_existing=True)
    assert fresh.status is AppStatus.PENDING
    # The citizen's Yes stands and the merge says so, with the disagreement recorded.
    assert fresh.declaration["merged"]["answers"]["health_data"] is True
    assert fresh.declaration["differences"]["health_data"] == ["citizen_yes_over_review_no"]
    # Nothing NEW was raised — it routed on what was already declared, and the drift block
    # says exactly that rather than inventing a finding.
    assert fresh.declaration["drift"]["newlyRaised"] == []
    # And the citizen is told the truth: the check found nothing they had not covered, so
    # the sentence must name what the app HANDLES, never claim a discovery.
    told = await db_session.scalar(
        sa.select(Message).where(Message.conversation_id == conversation.id)
    )
    said = told.payload[0]["parts"][0]["content"]
    assert "Health Data" in said
    assert "had not covered" not in said


async def test_a_failed_re_check_routes_rather_than_publishing(wire, db_session) -> None:
    """R20's rule 4, standing on the far side of the 202: no genuinely-complete review for
    this version is exactly the "unavailable" state the gate routes on. Letting it publish
    because a failed review names no categories would make failure the cheapest way
    through the gate."""
    user, app, conversation = await _project(db_session)
    await _saved_bundle(wire, app)
    wire.reviewer.fail_code = "review_failed"

    _started, row = await _run(
        wire,
        db_session,
        user,
        app,
        conversation.id,
        expected_commit_sha=_HEAD,
        recheck=VersionRecheck(answered_about=_OLDER, declaration=declaration()),
    )

    assert row.failure_code == "routed_for_review"
    fresh = await db_session.get(AppRegistry, app.id, populate_existing=True)
    assert fresh.status is AppStatus.PENDING
    assert fresh.declaration["drift"]["newlyRaised"] == []
    (audit,) = await _gate_rows(db_session, app.id)
    assert audit.detail is not None
    assert audit.detail["rule"] == "recheck_review_not_current"
    message = await db_session.scalar(
        sa.select(Message).where(Message.conversation_id == conversation.id)
    )
    assert "could not be completed" in message.payload[0]["parts"][0]["content"]


async def test_a_refused_routing_is_recorded_and_leaves_nothing_half_submitted(
    wire, db_session, monkeypatch
) -> None:
    """A guard inside the queue refusing (here: a build session went live while the review
    ran) must reach the citizen. The pipeline never raises out, so if this were swallowed
    the deploy would simply stop with no explanation anywhere — and the app must not be
    left half-submitted either."""
    user, app, conversation = await _project(db_session)
    await _saved_bundle(wire, app)
    wire.reviewer.verdicts = review_doc(health_data="yes")

    async def _live(user_id, *, conflict_message, app_id=None) -> None:
        raise AppApiError(409, conflict_message)

    monkeypatch.setattr(submit_module, "refuse_while_build_session_live", _live)

    _started, row = await _run(
        wire,
        db_session,
        user,
        app,
        conversation.id,
        expected_commit_sha=_HEAD,
        recheck=VersionRecheck(answered_about=_OLDER, declaration=declaration()),
    )

    assert row.status is DeploymentStatus.FAILED
    assert row.failure_code == "route_refused"
    assert "build session" in (row.failure_detail or "")
    fresh = await db_session.get(AppRegistry, app.id, populate_existing=True)
    assert fresh.status is AppStatus.DRAFT  # not pending, not half-submitted
    assert fresh.source_commit_sha is None
    message = await db_session.scalar(
        sa.select(Message).where(Message.conversation_id == conversation.id)
    )
    assert "not published" in message.payload[0]["parts"][0]["content"]


async def test_the_queue_copy_is_pinned_to_the_commit_the_pipeline_asserted(
    wire, db_session
) -> None:
    """The last gap: the submit forks whatever the mutable snapshot holds NOW, so a save
    landing while the review ran would queue a version nobody examined. Refused, and the
    savepoint leaves no half-submitted row behind."""
    user, app, _conversation = await _project(db_session)
    # The savepoint rollback expires the instance, so read the id while it is still cheap.
    app_id = app.id
    # The store holds a LATER version than the tree the pipeline extracted and reviewed.
    await _saved_bundle(wire, app, sha=_OLDER)
    wire.reviewer.verdicts = review_doc(health_data="yes")

    _started, row = await _run(
        wire,
        db_session,
        user,
        app,
        expected_commit_sha=_HEAD,
        recheck=VersionRecheck(answered_about=_OLDER, declaration=declaration()),
    )

    assert row.failure_code == "route_refused"
    fresh = await db_session.get(AppRegistry, app_id, populate_existing=True)
    assert fresh.status is AppStatus.DRAFT
    assert fresh.declaration is None


# --- concurrency ------------------------------------------------------------------


async def test_a_second_deploy_while_one_runs_is_refused(wire, db_session) -> None:
    user, app, _conversation = await _project(db_session)
    await wire.service.start(
        db_session,
        user_id=user.id,
        app_id=app.id,
        project_id=app.project_id,
        conversation_id=None,
    )

    with pytest.raises(DeployNotPossibleError) as caught:
        await wire.service.start(
            db_session,
            user_id=user.id,
            app_id=app.id,
            project_id=app.project_id,
            conversation_id=None,
        )
    assert caught.value.code == "deploy_in_flight"
    await wire.service.drain()
