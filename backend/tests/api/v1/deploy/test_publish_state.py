"""`compute_publish_state` — a pure mapping from `(registry row, newest deployment
row, saved head)` to one of eleven `PublishState` values — and `approved_retry_commit`, the
commit its one button posts when it republishes an approved copy.

Every case is built WITHOUT a database session and WITHOUT an event loop: the function
reads nothing but plain columns off two ORM instances it never persists. That is the
property the unit's technical-design note requires ("no I/O, no storage handle in the
signature, so it cannot acquire a hidden input later"), and building inputs by hand rather
than through `AppRegistryFactory` or a live `deployments` row is what proves it.

The object-store HEAD this depends on, and the storage-error / no-app-row cases that only
make sense at the route, are covered where the I/O lives: `test_deploy_routes.py`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from src.api.v1.deploy.schemas import (
    _NON_RETRYABLE_FAILURE_CODES,
    _RESTART_FAILURE_CODES,
    _RETRYABLE_FAILURE_CODES,
    _ROUTED_FAILURE_CODES,
    PublishState,
    RegistryStatus,
    approved_retry_commit,
    compute_publish_state,
    compute_registry_status,
    published_since_approval,
    retry_needs_last_publish,
)
from src.db.models.app_registry import AppRegistry, AppStatus
from src.db.models.deployment import Deployment, DeploymentStatus
from src.services.deploy import service as deploy_service
from src.services.deploy.service import (
    FAIL_RESTART,
    FAIL_RESTART_NOT_READY,
    FAIL_ROUTED_FOR_REVIEW,
)
from src.services.deploy.store import INTERRUPTED

_LIVE_SHA = "aa" * 20
_SAVED_SHA = "bb" * 20
_SUBMITTED_SHA = "cc" * 20
_APPROVED_AT = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)
_BEFORE_APPROVAL = _APPROVED_AT - timedelta(hours=1)
_SINCE_APPROVAL = _APPROVED_AT + timedelta(hours=1)


def _app(**overrides: object) -> AppRegistry:
    data: dict[str, object] = {"status": AppStatus.DRAFT}
    data.update(overrides)
    return AppRegistry(**data)


def _deployment(**overrides: object) -> Deployment:
    data: dict[str, object] = {"status": DeploymentStatus.SUCCEEDED, "created_at": _SINCE_APPROVAL}
    data.update(overrides)
    return Deployment(**data)


def _approved(sha: str = _SAVED_SHA, **overrides: object) -> AppRegistry:
    """Approved, with the reviewed submission copy on record."""
    return _app(
        status=AppStatus.APPROVED,
        approved_submission_id=uuid.uuid4(),
        approved_commit_sha=sha,
        approved_at=_APPROVED_AT,
        **overrides,
    )


# --- the eleven values, one row combination per value ----------------------------------
#
# `NOTHING_BUILT` is the eleventh and is not here: `compute_publish_state` takes an
# `AppRegistry` as a required argument, so "no app row at all" is decided in the route
# BEFORE the function is ever called — see `test_deploy_routes.py`'s
# `test_the_approval_state_is_null_only_when_the_project_has_no_app`.


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
            PublishState.STARTING_UP,
            _app(status=AppStatus.DRAFT),
            _deployment(status=DeploymentStatus.RUNNING),
            None,
            id="starting_up: a deployment row in flight",
        ),
        pytest.param(
            PublishState.LIVE_CURRENT,
            _app(status=AppStatus.APPROVED),
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
            _app(status=AppStatus.APPROVED),
            _deployment(status=DeploymentStatus.SUCCEEDED, head_sha=_LIVE_SHA),
            _SAVED_SHA,
            id="live_newer_work: the saved head differs from what went live",
        ),
        pytest.param(
            PublishState.TAKEN_OFFLINE,
            _app(status=AppStatus.APPROVED),
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
def test_each_of_the_remaining_ten_values_is_reachable(
    expected: PublishState,
    app: AppRegistry,
    deployment: Deployment | None,
    saved_head: str | None,
) -> None:
    """The eleven-case table, ten rows deep (see the module note on `NOTHING_BUILT`).
    Each row is a row combination that can actually occur, not a synthetic corner no
    real app reaches — the parametrize id says which product situation it is."""
    assert compute_publish_state(app, deployment, saved_head) is expected


# --- the drift bullet: named scenarios worth pinning on their own -----------------------


def test_a_live_app_with_four_saves_and_no_new_submission_reads_live_newer_work() -> None:
    """THE case that motivated reading the saved head at all. The submitted
    commit (`source_commit_sha`) has NOT moved since approval — a Save never touches it
    — so a check that only ever compared the submitted commit against the live head
    would see no difference and answer `live_drift_unknown`. The saved snapshot's head
    HAS moved (four Saves since), and that is the signal that must win."""
    app = _app(
        status=AppStatus.APPROVED,
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


def test_an_unattended_publish_reads_live_off_the_saved_head_never_the_pin() -> None:
    """An app published unattended has `approved_commit_sha` NULL — it
    never went through an administrator — and that column must play no part in
    deciding whether it reads as live. Deciding on the saved head against the
    deployment head, with the pin absent throughout, is the whole point."""
    app = _app(status=AppStatus.DRAFT, approved_commit_sha=None)
    deployment = _deployment(status=DeploymentStatus.SUCCEEDED, head_sha=_LIVE_SHA)

    assert compute_publish_state(app, deployment, _LIVE_SHA) is PublishState.LIVE_CURRENT


def test_a_routed_failure_code_resolves_above_the_failure_arm() -> None:
    """The `failure_code` bullet, exercised through the fallthrough arm rather than the
    `AppStatus.PENDING` shortcut (`app.status` here is `DRAFT`, so no status-level arm
    can answer and this pins the deployment-row check itself). Without this rule a
    citizen correctly routed to an administrator would read `did_not_start`."""
    app = _app(status=AppStatus.DRAFT)
    deployment = _deployment(status=DeploymentStatus.FAILED, failure_code=FAIL_ROUTED_FOR_REVIEW)

    assert compute_publish_state(app, deployment, None) is PublishState.IN_REVIEW


# --- an approved copy: its one button publishes it -------------------------------------


def test_an_approved_copy_never_attempted_reads_did_not_start_and_offers_itself() -> None:
    """Approval publishes; when that could not start, the owner's one button is Try again, and
    it posts the approved commit — whatever has been saved since."""
    app = _approved(_SUBMITTED_SHA)

    assert compute_publish_state(app, None, _SAVED_SHA) is PublishState.DID_NOT_START
    assert approved_retry_commit(app, None, approved_went_live=False) == _SUBMITTED_SHA


def test_a_row_older_than_the_approval_is_not_an_attempt_at_it() -> None:
    """A restart that was running when the administrator approved, the version live before
    the new one was sent, a routing that came before: none is an attempt at the approved copy."""
    app = _approved(_SUBMITTED_SHA)
    for older in (
        _deployment(status=DeploymentStatus.RUNNING, created_at=_BEFORE_APPROVAL),
        _deployment(head_sha=_LIVE_SHA, created_at=_BEFORE_APPROVAL),
        _deployment(
            status=DeploymentStatus.FAILED,
            failure_code=FAIL_ROUTED_FOR_REVIEW,
            created_at=_BEFORE_APPROVAL,
        ),
    ):
        assert compute_publish_state(app, older, _SAVED_SHA) is PublishState.DID_NOT_START
        assert approved_retry_commit(app, older, approved_went_live=False) == _SUBMITTED_SHA


def test_the_attempt_the_approval_starts_counts_as_since_approval() -> None:
    """`approve` stamps `approved_at` with its transaction's clock, the same instant its claim
    stamps on the row it starts — equal is since, not before."""
    app = _approved(_SUBMITTED_SHA)
    running = _deployment(status=DeploymentStatus.RUNNING, created_at=_APPROVED_AT)

    assert compute_publish_state(app, running, _SAVED_SHA) is PublishState.STARTING_UP
    assert approved_retry_commit(app, running, approved_went_live=False) is None


@pytest.mark.parametrize(
    ("expected", "retry", "deployment", "saved_head"),
    [
        pytest.param(
            PublishState.STARTING_UP,
            None,
            _deployment(status=DeploymentStatus.RUNNING),
            _SAVED_SHA,
            id="starting_up: the approved copy is being published right now",
        ),
        pytest.param(
            PublishState.DID_NOT_START,
            _SUBMITTED_SHA,
            _deployment(
                status=DeploymentStatus.FAILED,
                failure_code="provision_failed",
                head_sha=_SUBMITTED_SHA,
            ),
            _SAVED_SHA,
            id="did_not_start: the attempt at the approved copy failed; try it again",
        ),
        pytest.param(
            PublishState.DID_NOT_START,
            None,
            _deployment(
                status=DeploymentStatus.FAILED,
                failure_code="build_failed",
                head_sha=_SUBMITTED_SHA,
            ),
            _SAVED_SHA,
            id="did_not_start: the approved copy will not build; act on the saved version",
        ),
        pytest.param(
            PublishState.DID_NOT_START,
            _SUBMITTED_SHA,
            _deployment(status=DeploymentStatus.FAILED, failure_code="internal_error"),
            _SAVED_SHA,
            id="did_not_start: it failed before it could name a commit",
        ),
        pytest.param(
            PublishState.DID_NOT_START,
            None,
            _deployment(
                status=DeploymentStatus.FAILED,
                failure_code="provision_failed",
                head_sha=_SAVED_SHA,
            ),
            _SAVED_SHA,
            id="did_not_start: a later version failed; that one is retried through the gate",
        ),
        pytest.param(
            PublishState.LIVE_NEWER_WORK,
            None,
            _deployment(head_sha=_SUBMITTED_SHA),
            _SAVED_SHA,
            id="live_newer_work: the approved copy went live, and newer work is saved",
        ),
        pytest.param(
            PublishState.LIVE_CURRENT,
            None,
            _deployment(head_sha=_SAVED_SHA),
            _SAVED_SHA,
            id="live_current: a later version published unattended and is what is saved",
        ),
        pytest.param(
            PublishState.TAKEN_OFFLINE,
            _SUBMITTED_SHA,
            _deployment(head_sha=_SUBMITTED_SHA, unpublished_at=datetime(2026, 9, 21, tzinfo=UTC)),
            _SAVED_SHA,
            id="taken_offline: the approved version came down; publishing again restores it",
        ),
        pytest.param(
            PublishState.TAKEN_OFFLINE,
            None,
            _deployment(head_sha=_SAVED_SHA, unpublished_at=datetime(2026, 9, 21, tzinfo=UTC)),
            _SAVED_SHA,
            id="taken_offline: another version came down; it goes back through the gate",
        ),
        pytest.param(
            PublishState.LIVE_CURRENT,
            None,
            _deployment(
                status=DeploymentStatus.FAILED,
                failure_code="restart_failed",
                head_sha=_SAVED_SHA,
            ),
            _SAVED_SHA,
            id="live_current: a failed restart leaves the version it restarted serving",
        ),
    ],
)
def test_once_attempted_the_attempt_speaks_for_the_approved_copy(
    expected: PublishState,
    retry: str | None,
    deployment: Deployment,
    saved_head: str,
) -> None:
    app = _approved(_SUBMITTED_SHA)

    assert compute_publish_state(app, deployment, saved_head) is expected
    assert approved_retry_commit(app, deployment, approved_went_live=False) == retry


def test_approved_then_try_again_failed_then_succeeded_reads_live() -> None:
    app = _approved(_SUBMITTED_SHA)
    failed = _deployment(
        status=DeploymentStatus.FAILED, failure_code="provision_failed", head_sha=_SUBMITTED_SHA
    )
    assert compute_publish_state(app, failed, _SUBMITTED_SHA) is PublishState.DID_NOT_START
    assert approved_retry_commit(app, failed, approved_went_live=False) == _SUBMITTED_SHA

    succeeded = _deployment(
        head_sha=_SUBMITTED_SHA, created_at=_SINCE_APPROVAL + timedelta(minutes=5)
    )
    assert compute_publish_state(app, succeeded, _SUBMITTED_SHA) is PublishState.LIVE_CURRENT
    assert approved_retry_commit(app, succeeded, approved_went_live=False) is None


def test_a_nameless_failure_after_the_copy_went_live_goes_back_through_the_gate() -> None:
    """The approved copy went live; a later version was sent and failed before it could name its
    commit. Try again retries that later version through the gate — republishing the copy
    already serving would ignore what the owner just sent."""
    app = _approved(_SUBMITTED_SHA)
    failed = _deployment(status=DeploymentStatus.FAILED, failure_code="internal_error")

    assert compute_publish_state(app, failed, _SAVED_SHA) is PublishState.DID_NOT_START
    assert retry_needs_last_publish(app, failed)
    assert approved_retry_commit(app, failed, approved_went_live=True) is None


def test_a_nameless_failure_with_nothing_live_since_approval_retries_the_copy() -> None:
    app = _approved(_SUBMITTED_SHA)
    failed = _deployment(status=DeploymentStatus.FAILED, failure_code="interrupted")

    assert retry_needs_last_publish(app, failed)
    assert approved_retry_commit(app, failed, approved_went_live=False) == _SUBMITTED_SHA


@pytest.mark.parametrize("code", sorted(_NON_RETRYABLE_FAILURE_CODES))
def test_an_approved_copy_that_fails_in_itself_is_not_offered_again(code: str) -> None:
    """It would fail the same way on every press, and the owner could never send the fix: the
    one button acts on the saved version instead, named commit or not."""
    app = _approved(_SUBMITTED_SHA)
    for head in (_SUBMITTED_SHA, None):
        failed = _deployment(status=DeploymentStatus.FAILED, failure_code=code, head_sha=head)

        assert compute_publish_state(app, failed, _SAVED_SHA) is PublishState.DID_NOT_START
        assert not retry_needs_last_publish(app, failed)
        assert approved_retry_commit(app, failed, approved_went_live=False) is None


def test_every_failure_code_is_sorted_into_exactly_one_set() -> None:
    """Whether the one button retries the approved copy after a failure is decided per code, so
    a code the pipeline gains must be placed before it ships."""
    codes = {value for name, value in vars(deploy_service).items() if name.startswith("FAIL_")} | {
        INTERRUPTED
    }
    placed = [
        code
        for group in (
            _RETRYABLE_FAILURE_CODES,
            _NON_RETRYABLE_FAILURE_CODES,
            _RESTART_FAILURE_CODES,
            _ROUTED_FAILURE_CODES,
        )
        for code in group
    ]

    assert sorted(placed) == sorted(codes)


def test_only_a_nameless_failed_attempt_since_approval_asks_what_went_live() -> None:
    """Every other row answers for itself, which is what keeps the poll's ordinary path at its
    existing queries."""
    app = _approved(_SUBMITTED_SHA)
    nameless = _deployment(status=DeploymentStatus.FAILED, failure_code="internal_error")
    assert retry_needs_last_publish(app, nameless)

    for deployment in (
        None,
        _deployment(
            status=DeploymentStatus.FAILED, failure_code="build_failed", head_sha=_SAVED_SHA
        ),
        _deployment(
            status=DeploymentStatus.FAILED, failure_code="build_failed", head_sha=_SUBMITTED_SHA
        ),
        _deployment(status=DeploymentStatus.FAILED, failure_code="restart_failed"),
        _deployment(status=DeploymentStatus.RUNNING),
        _deployment(head_sha=_SUBMITTED_SHA),
        _deployment(
            status=DeploymentStatus.FAILED,
            failure_code="internal_error",
            created_at=_BEFORE_APPROVAL,
        ),
        _deployment(
            status=DeploymentStatus.FAILED,
            failure_code="internal_error",
            unpublished_at=datetime(2026, 9, 21, tzinfo=UTC),
        ),
    ):
        assert not retry_needs_last_publish(app, deployment), deployment
    assert not retry_needs_last_publish(_app(status=AppStatus.DRAFT), nameless)


def test_a_publish_counts_as_the_approved_copy_going_live_only_from_the_approval_on() -> None:
    app = _approved(_SUBMITTED_SHA)

    assert published_since_approval(app, _deployment(created_at=_SINCE_APPROVAL))
    assert published_since_approval(app, _deployment(created_at=_APPROVED_AT))
    assert not published_since_approval(app, _deployment(created_at=_BEFORE_APPROVAL))
    assert not published_since_approval(app, None)


def test_an_approval_with_no_stored_copy_is_a_draft_again() -> None:
    """Approved before copies were kept: there is nothing to republish, so the owner sends
    for review — never Try again."""
    app = _app(status=AppStatus.APPROVED, approved_commit_sha=_LIVE_SHA, approved_at=_APPROVED_AT)

    assert compute_publish_state(app, None, _SAVED_SHA) is PublishState.DRAFT
    assert approved_retry_commit(app, None, approved_went_live=False) is None


def test_no_state_but_an_approved_one_carries_a_retry_commit() -> None:
    for status in (AppStatus.DRAFT, AppStatus.PENDING, AppStatus.REJECTED, AppStatus.DISABLED):
        app = _app(
            status=status,
            approved_submission_id=uuid.uuid4(),
            approved_commit_sha=_SUBMITTED_SHA,
            approved_at=_APPROVED_AT,
        )
        assert approved_retry_commit(app, None, approved_went_live=False) is None, status


def test_a_non_routed_failure_code_reads_did_not_start() -> None:
    app = _app(status=AppStatus.DRAFT)
    deployment = _deployment(status=DeploymentStatus.FAILED, failure_code="revision_unhealthy")

    assert compute_publish_state(app, deployment, None) is PublishState.DID_NOT_START


def test_switched_off_and_taken_offline_are_told_apart() -> None:
    """A disabled app reads `switched_off` regardless of its deployment row; a
    live app an administrator merely unpublished reads `taken_offline`. Different
    remedies, both durable, and neither may stand in for the other."""
    disabled = _app(status=AppStatus.DISABLED)
    still_running = _deployment(status=DeploymentStatus.SUCCEEDED, head_sha=_LIVE_SHA)
    assert compute_publish_state(disabled, still_running, _LIVE_SHA) is PublishState.SWITCHED_OFF

    live_app = _app(status=AppStatus.APPROVED)
    unpublished = _deployment(
        status=DeploymentStatus.SUCCEEDED,
        head_sha=_LIVE_SHA,
        unpublished_at=datetime(2026, 8, 20, tzinfo=UTC),
    )
    assert compute_publish_state(live_app, unpublished, _LIVE_SHA) is PublishState.TAKEN_OFFLINE


# --- a failed RESTART is an attempt fact, not a production one -------------------------
#
# `deployments` is append-only and a restart claims a row of its own, so one that fails leaves
# a FAILED row newer than the attempt that published the container still serving. Read as a
# production fact it answers `did_not_start`, while `liveness.live_app_ids` — which reads the
# last attempt that actually published — goes on reporting the same app live. Two readers, one
# app, opposite answers, and the surface that believed the first withheld Take down as well as
# Restart from an owner whose app never went down.


@pytest.mark.parametrize("code", ["restart_failed", "restart_not_ready"])
def test_a_failed_restart_reports_the_live_state_it_left_standing(code: str) -> None:
    """★ The drift comparison still answers, because a restart copies the live `head_sha` onto
    its own row — so the reading is the full one, not a fallback to "couldn't check"."""
    app = _app()
    deployment = _deployment(status=DeploymentStatus.FAILED, failure_code=code, head_sha=_LIVE_SHA)
    assert compute_publish_state(app, deployment, _LIVE_SHA) is PublishState.LIVE_CURRENT
    assert compute_publish_state(app, deployment, _SAVED_SHA) is PublishState.LIVE_NEWER_WORK


def test_a_failed_publish_still_reports_that_it_did_not_start() -> None:
    """★ THE PAIRED NEGATIVE, and the reason the arm keys on the failure CODE. A build that
    never came up IS a production fact: nothing is serving, and reading it as live would put a
    "Live" pill over an application that has never run. Its row carries a `head_sha` too, so the
    code is the only thing telling the two apart."""
    app = _app()
    deployment = _deployment(
        status=DeploymentStatus.FAILED, failure_code="build_failed", head_sha=_LIVE_SHA
    )
    assert compute_publish_state(app, deployment, _LIVE_SHA) is PublishState.DID_NOT_START


@pytest.mark.parametrize("code", ["restart_failed", "restart_not_ready", "build_failed"])
def test_a_takedown_says_taken_offline_whatever_attempt_it_landed_on(code: str) -> None:
    """★ OFFLINE OUTRANKS THE ATTEMPT, AND IT HAS TO NAME ITSELF. `unpublish` stamps whichever row
    was newest when it ran, which after a failed restart is that failure.

    "not live" is not enough, and asserting only that is what hid the real answer: `DID_NOT_START`
    satisfies it, and it is a dead end — the production surface reads "Could not restart" about an
    application the owner has just removed themselves, and offers neither Publish nor Take down
    beside it. The only lever left was a full re-publish, or an administrator."""
    app = _app()
    deployment = _deployment(
        status=DeploymentStatus.FAILED,
        failure_code=code,
        head_sha=_LIVE_SHA,
        unpublished_at=datetime(2026, 9, 17, 1, 0, tzinfo=UTC),
    )
    assert compute_publish_state(app, deployment, _LIVE_SHA) is PublishState.TAKEN_OFFLINE


def test_a_failed_restart_with_no_head_claims_nothing_about_a_version() -> None:
    """A row with no `head_sha` cannot say which version is standing, and this arm's whole
    licence to report live is that the restart copied one across. Without it the honest answer
    is the generic failure."""
    app = _app()
    deployment = _deployment(
        status=DeploymentStatus.FAILED, failure_code="restart_failed", head_sha=None
    )
    assert compute_publish_state(app, deployment, _LIVE_SHA) is PublishState.DID_NOT_START


# --- the App Registry's status: the same reading, told apart where an administrator needs it ---

_TAKEN_DOWN_AT = datetime(2026, 9, 21, tzinfo=UTC)

_REGISTRY_CASES = [
    pytest.param(
        RegistryStatus.DISABLED,
        _app(status=AppStatus.DISABLED),
        _deployment(head_sha=_LIVE_SHA),
        id="disabled: switched off, whatever is still on its deployment row",
    ),
    pytest.param(
        RegistryStatus.WAITING_FOR_REVIEW,
        _app(status=AppStatus.PENDING),
        _deployment(head_sha=_LIVE_SHA),
        id="waiting for review: sent, with an older version still serving",
    ),
    pytest.param(
        RegistryStatus.REJECTED,
        _app(status=AppStatus.REJECTED),
        None,
        id="rejected",
    ),
    pytest.param(
        RegistryStatus.NOT_PUBLISHED,
        _approved(_SUBMITTED_SHA),
        None,
        id="not published: approved, and nothing has tried to publish the copy",
    ),
    pytest.param(
        RegistryStatus.NOT_PUBLISHED,
        _approved(_SUBMITTED_SHA),
        _deployment(head_sha=_LIVE_SHA, created_at=_BEFORE_APPROVAL),
        id="not published: the only row predates the approval",
    ),
    pytest.param(
        RegistryStatus.NOT_PUBLISHED,
        _approved(_SUBMITTED_SHA),
        _deployment(
            status=DeploymentStatus.FAILED,
            failure_code=FAIL_ROUTED_FOR_REVIEW,
            created_at=_BEFORE_APPROVAL,
        ),
        id="not published: an older routing row gives way to the approval",
    ),
    pytest.param(
        RegistryStatus.DRAFT,
        _app(status=AppStatus.DRAFT),
        None,
        id="draft: never published",
    ),
    pytest.param(
        RegistryStatus.DRAFT,
        _app(status=AppStatus.APPROVED, approved_commit_sha=_LIVE_SHA, approved_at=_APPROVED_AT),
        None,
        id="draft: approved before copies were kept, so there is nothing to publish",
    ),
    pytest.param(
        RegistryStatus.PUBLISHING,
        _approved(_SUBMITTED_SHA),
        _deployment(status=DeploymentStatus.RUNNING, created_at=_APPROVED_AT),
        id="publishing: the attempt the approval started",
    ),
    pytest.param(
        RegistryStatus.PUBLISHING,
        _app(status=AppStatus.DRAFT),
        _deployment(status=DeploymentStatus.RUNNING),
        id="publishing: an owner's publish in flight",
    ),
    pytest.param(
        RegistryStatus.TAKEN_OFFLINE,
        _app(status=AppStatus.DRAFT),
        _deployment(head_sha=_LIVE_SHA, unpublished_at=_TAKEN_DOWN_AT),
        id="taken offline",
    ),
    pytest.param(
        RegistryStatus.LIVE,
        _approved(_SUBMITTED_SHA),
        _deployment(head_sha=_SUBMITTED_SHA),
        id="live: the approved copy went live",
    ),
    pytest.param(
        RegistryStatus.LIVE,
        _app(status=AppStatus.DRAFT, source_commit_sha=_SUBMITTED_SHA),
        _deployment(head_sha=_LIVE_SHA),
        id="live: newer work sent since, which the list does not look for",
    ),
    pytest.param(
        RegistryStatus.LIVE,
        _app(status=AppStatus.DRAFT, source_commit_sha=None),
        _deployment(head_sha=_LIVE_SHA),
        id="live: whether newer work exists is unknown",
    ),
    pytest.param(
        RegistryStatus.LIVE,
        _app(status=AppStatus.DRAFT),
        _deployment(status=DeploymentStatus.FAILED, failure_code=FAIL_RESTART, head_sha=_LIVE_SHA),
        id="live: a failed restart leaves the version it restarted serving",
    ),
    pytest.param(
        RegistryStatus.LIVE,
        _app(status=AppStatus.DRAFT),
        _deployment(
            status=DeploymentStatus.FAILED,
            failure_code=FAIL_RESTART_NOT_READY,
            head_sha=_LIVE_SHA,
        ),
        id="live: a restart that did not come back ready leaves its version serving",
    ),
    pytest.param(
        RegistryStatus.PUBLISH_FAILED,
        _approved(_SUBMITTED_SHA),
        _deployment(
            status=DeploymentStatus.FAILED, failure_code="build_failed", head_sha=_SUBMITTED_SHA
        ),
        id="publish failed: the attempt at the approved copy failed",
    ),
    pytest.param(
        RegistryStatus.PUBLISH_FAILED,
        _app(status=AppStatus.DRAFT),
        _deployment(status=DeploymentStatus.FAILED, failure_code="revision_unhealthy"),
        id="publish failed: an owner's publish failed",
    ),
    pytest.param(
        RegistryStatus.PUBLISH_FAILED,
        _app(status=AppStatus.DRAFT),
        _deployment(status=DeploymentStatus.FAILED, failure_code=FAIL_RESTART, head_sha=None),
        id="publish failed: a failed restart that names no version claims none is serving",
    ),
    pytest.param(
        RegistryStatus.WAITING_FOR_REVIEW,
        _app(status=AppStatus.DRAFT),
        _deployment(status=DeploymentStatus.FAILED, failure_code=FAIL_ROUTED_FOR_REVIEW),
        id="waiting for review: an older row that routed to an administrator",
    ),
]


@pytest.mark.parametrize(("expected", "app", "deployment"), _REGISTRY_CASES)
def test_each_app_reads_one_registry_status(
    expected: RegistryStatus, app: AppRegistry, deployment: Deployment | None
) -> None:
    assert compute_registry_status(app, deployment) is expected


def test_every_registry_status_has_a_case() -> None:
    reached = {case.values[0] for case in _REGISTRY_CASES}

    assert reached == set(RegistryStatus)
