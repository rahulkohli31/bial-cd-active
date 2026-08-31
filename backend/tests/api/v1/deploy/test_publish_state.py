"""`compute_publish_state` — U15's pure mapping from `(registry row, newest deployment
row, saved head)` to one of thirteen `PublishState` values.

Every case below is built WITHOUT a database session and WITHOUT an event loop: the
function reads nothing but plain columns off two ORM instances it never persists. That
is not an incidental convenience — it is the property the unit's "technical design"
note asks for ("no I/O, no storage handle in the signature, so it cannot acquire a
hidden input later"), and constructing the inputs by hand rather than through
`AppRegistryFactory`/a live `deployments` row is what actually proves it, rather than
merely asserting it in a docstring.

The single object-store metadata HEAD this depends on, and the storage-error /
no-app-row cases that only make sense at the route, are covered where the I/O lives:
`test_deploy_routes.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.api.v1.deploy.schemas import PublishState, compute_publish_state
from src.db.models.app_registry import AppRegistry, ApprovalRoute, AppStatus
from src.db.models.deployment import Deployment, DeploymentStatus
from src.services.deploy.service import FAIL_ROUTED_FOR_REVIEW

_LIVE_SHA = "aa" * 20
_SAVED_SHA = "bb" * 20
_SUBMITTED_SHA = "cc" * 20


def _app(**overrides: object) -> AppRegistry:
    data: dict[str, object] = {"status": AppStatus.DRAFT}
    data.update(overrides)
    return AppRegistry(**data)


def _deployment(**overrides: object) -> Deployment:
    data: dict[str, object] = {"status": DeploymentStatus.SUCCEEDED}
    data.update(overrides)
    return Deployment(**data)


# --- the thirteen values, one row combination per value --------------------------------
#
# `NOTHING_BUILT` is the thirteenth and is not here: `compute_publish_state` takes an
# `AppRegistry` as a required argument, so "no app row at all" is decided in the route
# BEFORE the function is ever called — see `test_deploy_routes.py`'s
# `test_a_project_with_no_app_reads_nothing_built_with_no_approval_block`.


@pytest.mark.parametrize(
    ("expected", "app", "deployment", "saved_head"),
    [
        pytest.param(
            PublishState.DRAFT,
            _app(status=AppStatus.DRAFT),
            None,
            None,
            id="draft: app row, never submitted, no deployment",
        ),
        pytest.param(
            PublishState.IN_REVIEW,
            _app(status=AppStatus.PENDING),
            None,
            None,
            id="in_review: submitted and awaiting an administrator",
        ),
        pytest.param(
            PublishState.CHANGES_REQUESTED,
            _app(status=AppStatus.REJECTED),
            None,
            None,
            id="changes_requested: rejected",
        ),
        pytest.param(
            PublishState.APPROVED_READY_TO_PUBLISH,
            _app(
                status=AppStatus.APPROVED,
                approval_route=ApprovalRoute.SELF_PUBLISH,
                approved_commit_sha=_SAVED_SHA,
            ),
            None,
            _SAVED_SHA,
            id="approved_ready_to_publish: self-publish, pin matches, never deployed",
        ),
        pytest.param(
            PublishState.APPROVED_NEEDS_REVIEW_AGAIN,
            _app(
                status=AppStatus.APPROVED,
                approval_route=ApprovalRoute.RUNBOOK,
                approved_commit_sha=_LIVE_SHA,
            ),
            None,
            _LIVE_SHA,
            id="approved_needs_review_again: the runbook lineage never self-publishes",
        ),
        pytest.param(
            PublishState.STARTING_UP,
            _app(status=AppStatus.DRAFT),
            _deployment(status=DeploymentStatus.RUNNING),
            None,
            id="starting_up: a deployment row in flight",
        ),
        pytest.param(
            PublishState.LIVE_CURRENT,
            _app(status=AppStatus.APPROVED, approval_route=ApprovalRoute.SELF_PUBLISH),
            _deployment(status=DeploymentStatus.SUCCEEDED, head_sha=_LIVE_SHA),
            _LIVE_SHA,
            id="live_current: the saved head matches the commit that went live",
        ),
        pytest.param(
            PublishState.LIVE_DRIFT_UNKNOWN,
            _app(status=AppStatus.DRAFT, source_commit_sha=None),
            _deployment(status=DeploymentStatus.SUCCEEDED, head_sha=_LIVE_SHA),
            None,
            id="live_drift_unknown: saved head unreadable, no submitted-commit signal either",
        ),
        pytest.param(
            PublishState.LIVE_NEWER_WORK,
            _app(status=AppStatus.APPROVED, approval_route=ApprovalRoute.SELF_PUBLISH),
            _deployment(status=DeploymentStatus.SUCCEEDED, head_sha=_LIVE_SHA),
            _SAVED_SHA,
            id="live_newer_work: the saved head differs from what went live",
        ),
        pytest.param(
            PublishState.TAKEN_OFFLINE,
            _app(status=AppStatus.APPROVED, approval_route=ApprovalRoute.SELF_PUBLISH),
            _deployment(
                status=DeploymentStatus.SUCCEEDED,
                head_sha=_LIVE_SHA,
                unpublished_at=datetime(2026, 8, 20, tzinfo=UTC),
            ),
            _LIVE_SHA,
            id="taken_offline: the newest deployment's unpublished_at is set",
        ),
        pytest.param(
            PublishState.SWITCHED_OFF,
            _app(status=AppStatus.DISABLED),
            _deployment(status=DeploymentStatus.SUCCEEDED, head_sha=_LIVE_SHA),
            _LIVE_SHA,
            id="switched_off: disabled, whatever its deployment row says",
        ),
        pytest.param(
            PublishState.DID_NOT_START,
            _app(status=AppStatus.DRAFT),
            _deployment(status=DeploymentStatus.FAILED, failure_code="build_failed"),
            None,
            id="did_not_start: the newest deployment failed with a non-routed code",
        ),
    ],
)
def test_each_of_the_remaining_twelve_values_is_reachable(
    expected: PublishState,
    app: AppRegistry,
    deployment: Deployment | None,
    saved_head: str | None,
) -> None:
    """The thirteen-case table, twelve rows deep (see the module note on `NOTHING_BUILT`).
    Each row is a row combination that can actually occur, not a synthetic corner no
    real app reaches — the parametrize id says which product situation it is."""
    assert compute_publish_state(app, deployment, saved_head) is expected


# --- the drift bullet: named scenarios worth pinning on their own -----------------------


def test_ae24_a_live_app_with_four_saves_and_no_new_submission_reads_live_newer_work() -> None:
    """THE case that motivated reading the saved head at all (R39/AE24). The submitted
    commit (`source_commit_sha`) has NOT moved since approval — a Save never touches it
    — so a check that only ever compared the submitted commit against the live head
    would see no difference and answer `live_drift_unknown`. The saved snapshot's head
    HAS moved (four Saves since), and that is the signal that must win."""
    app = _app(
        status=AppStatus.APPROVED,
        approval_route=ApprovalRoute.SELF_PUBLISH,
        approved_commit_sha=_LIVE_SHA,
        source_commit_sha=_LIVE_SHA,  # unchanged since approval
    )
    deployment = _deployment(status=DeploymentStatus.SUCCEEDED, head_sha=_LIVE_SHA)

    assert compute_publish_state(app, deployment, _SAVED_SHA) is PublishState.LIVE_NEWER_WORK


def test_an_unreadable_saved_head_still_reads_newer_work_off_the_submitted_commit() -> None:
    """The secondary positive signal (`source_commit_sha`) fires on its own when the
    primary one (the saved head) could not be read at all — it must not be swallowed
    into `live_drift_unknown` just because the stronger signal is missing."""
    app = _app(status=AppStatus.APPROVED, source_commit_sha=_SUBMITTED_SHA)
    deployment = _deployment(status=DeploymentStatus.SUCCEEDED, head_sha=_LIVE_SHA)

    assert compute_publish_state(app, deployment, None) is PublishState.LIVE_NEWER_WORK


def test_ladder_rule_7_unattended_publish_reads_live_off_the_saved_head_never_the_pin() -> None:
    """An app published unattended (ladder rule 7) has `approved_commit_sha` NULL — it
    never went through an administrator — and that column must play no part in
    deciding whether it reads as live. Deciding on the saved head against the
    deployment head, with the pin absent throughout, is the whole point."""
    app = _app(status=AppStatus.DRAFT, approved_commit_sha=None)
    deployment = _deployment(status=DeploymentStatus.SUCCEEDED, head_sha=_LIVE_SHA)

    assert compute_publish_state(app, deployment, _LIVE_SHA) is PublishState.LIVE_CURRENT


def test_approved_with_a_matching_pin_and_no_deployment_reads_ready_to_publish() -> None:
    """The earlier draft's two lies, named: a never-published approved app is neither
    `draft` (it has a real, actionable lifecycle) nor `starting_up` (nothing is
    running — approval starts no pipeline)."""
    app = _app(
        status=AppStatus.APPROVED,
        approval_route=ApprovalRoute.SELF_PUBLISH,
        approved_commit_sha=_SAVED_SHA,
    )

    state = compute_publish_state(app, None, _SAVED_SHA)

    assert state is PublishState.APPROVED_READY_TO_PUBLISH
    assert state not in (PublishState.DRAFT, PublishState.STARTING_UP)


def test_a_routed_failure_code_resolves_above_the_failure_arm() -> None:
    """The `failure_code` bullet, exercised through the fallthrough arm rather than the
    `AppStatus.PENDING` shortcut (`app.status` here is `APPROVED`, not `PENDING`, so
    this pins the deployment-row check itself, not the status-level one that would
    also produce `IN_REVIEW` for the ordinary case). Without this rule a citizen
    correctly routed to an administrator would read `did_not_start`."""
    app = _app(status=AppStatus.APPROVED, approval_route=ApprovalRoute.SELF_PUBLISH)
    deployment = _deployment(status=DeploymentStatus.FAILED, failure_code=FAIL_ROUTED_FOR_REVIEW)

    assert compute_publish_state(app, deployment, None) is PublishState.IN_REVIEW


def test_a_non_routed_failure_code_reads_did_not_start() -> None:
    app = _app(status=AppStatus.DRAFT)
    deployment = _deployment(status=DeploymentStatus.FAILED, failure_code="revision_unhealthy")

    assert compute_publish_state(app, deployment, None) is PublishState.DID_NOT_START


def test_switched_off_and_taken_offline_are_told_apart_ae23() -> None:
    """AE23: a disabled app reads `switched_off` regardless of its deployment row; a
    live app an administrator merely unpublished reads `taken_offline`. Different
    remedies, both durable, and neither may stand in for the other."""
    disabled = _app(status=AppStatus.DISABLED)
    still_running = _deployment(status=DeploymentStatus.SUCCEEDED, head_sha=_LIVE_SHA)
    assert compute_publish_state(disabled, still_running, _LIVE_SHA) is PublishState.SWITCHED_OFF

    live_app = _app(status=AppStatus.APPROVED, approval_route=ApprovalRoute.SELF_PUBLISH)
    unpublished = _deployment(
        status=DeploymentStatus.SUCCEEDED,
        head_sha=_LIVE_SHA,
        unpublished_at=datetime(2026, 8, 20, tzinfo=UTC),
    )
    assert compute_publish_state(live_app, unpublished, _LIVE_SHA) is PublishState.TAKEN_OFFLINE
