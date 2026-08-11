"""The scheduler's first passenger (U6, ADR-0011 / ADR-0029).

Three properties are worth a test rather than a comment, and each of them has a specific way of
failing silently:

* **The gate comes before the imports.** A disabled task that still drags in the ORM and the ARM
  SDK works perfectly and quietly taxes every deployment that has not enabled it — nothing ever
  raises. Asserted in a fresh interpreter, because the suite has already imported half the world
  by the time an in-process check could run.
* **A deferred row is not a reconciled one.** ARM answering "I cannot say" and ARM answering "it
  is gone" are one keystroke apart in the reporting, and collapsing them eventually marks a live
  app failed — the single most expensive mistake this reconciler can make.
* **The cron actually fires.** Taskiq validates a cron nowhere at decoration time, so a schedule
  that never matches a minute looks exactly like a healthy one from the outside.

The reconciler's own four-answer logic is pinned service-side in
`tests/services/deploy/test_reconcile.py`; this file pins the WORKER wrapping around it — the
gate, the schedule, the log line, and the boot one-shot it must not have replaced.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from structlog.testing import capture_logs
from taskiq.cli.scheduler.run import is_cron_task_now

import src.worker_main as worker_main
from src.config import settings
from src.db.models.deployment import Deployment, DeploymentStatus
from src.services.deploy import reconcile as deploy_reconcile_service
from src.services.deploy import store
from src.services.deploy.config import DeployConfig
from src.services.sandbox.aca import AcaTransientError
from src.workers.deploy_reconcile import (
    DEPLOY_RECONCILE_CRON,
    DEPLOY_RECONCILE_DISABLED_EVENT,
    DEPLOY_RECONCILE_DONE_EVENT,
    DEPLOY_RECONCILE_SCHEDULE_ID,
    DEPLOY_RECONCILE_TASK_NAME,
    _honoured_by_the_clock,
    reconcile_stalled_deploys,
)
from tests.factories import AppRegistryFactory, UserFactory

_BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
_DIGEST = "sha256:" + "ef" * 32

_DEPLOY_BLOCK: dict[str, Any] = {
    "acr_server": "bialgenaicr.azurecr.io",
    "acr_name": "bialgenaicr",
    "acr_resource_group": "BIAL-GENAI-AIML-RG",
    "acr_subscription_id": "00000000-0000-0000-0000-000000000000",
    "acr_username": "bialgenaicr",
    "acr_password": "shh",
    "subscription_id": "00000000-0000-0000-0000-000000000000",
    "resource_group": "BIAL-GENAI-DEV-RG",
    "region": "centralindia",
    "managed_environment_name": "bial-citizen-dev-aca-env",
}


def _deploy_config(**overrides: Any) -> DeployConfig:
    values: dict[str, Any] = {**_DEPLOY_BLOCK, **overrides}
    return DeployConfig(**values)


class _Arm:
    """Answers as ARM does: `None` is a CONFIRMED absence, a blip RAISES. The `blip` flag is
    flipped mid-test to play "the throttle cleared before the next tick"."""

    def __init__(self, *, fqdn: str | None, image: str | None = None, blip: bool = False) -> None:
        self.fqdn = fqdn
        self.image = image
        self.blip = blip

    async def get_app_fqdn(self, *, app_id: uuid.UUID) -> str | None:
        if self.blip:
            raise AcaTransientError("ARM is throttling")
        return self.fqdn

    async def get_app_image(self, *, app_id: uuid.UUID) -> str | None:
        return self.image


@pytest.fixture
def on_duty(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deployment that publishes apps, with the scheduled pass switched ON.

    Every behavioural test has to opt in explicitly: the suite runs with `DEPLOY__*` unset and the
    flag off, so without this the task would take the disabled branch and every assertion below
    would pass vacuously (`.claude/rules/testing.md`).
    """
    monkeypatch.setattr(settings, "deploy", _deploy_config(reconcile_enabled=True))


def _wire(monkeypatch: pytest.MonkeyPatch, db_session: Any, arm: _Arm) -> None:
    """Point the task at the test's transaction and at a fake ARM.

    Both patches land on the MODULE the task imports from, not on a name bound into it: the task
    body does its imports inside the function, so every call re-reads these attributes. A patch
    applied to the worker module itself would miss them entirely.
    """

    @contextlib.asynccontextmanager
    async def _session() -> AsyncIterator[Any]:
        # Yields the test's session WITHOUT closing it — `async with AsyncSession(...)` closes on
        # exit, and the reconciler opens one per row, so a naive factory would close the fixture's
        # session out from under the second row.
        yield db_session

    monkeypatch.setattr("src.db.base.async_session_factory", lambda: _session())
    monkeypatch.setattr("src.services.deploy.aca_publish.get_published_apps", lambda: arm)


async def _abandoned(db: Any, *, digest: str | None = _DIGEST) -> uuid.UUID:
    """A deployment row whose pipeline stopped beating."""
    user = await UserFactory.create(db)
    app = await AppRegistryFactory.create(db, user_id=user.id)
    deployment_id = await store.claim(db, app_id=app.id, user_id=user.id)
    assert deployment_id is not None
    await db.execute(
        sa.update(Deployment)
        .where(Deployment.id == deployment_id)
        .values(
            image_digest=digest,
            heartbeat_at=datetime.now(UTC)
            - timedelta(seconds=deploy_reconcile_service.STALE_AFTER_S + 60),
        )
    )
    return deployment_id


async def _row(db: Any, deployment_id: uuid.UUID) -> Deployment:
    row = await db.get(Deployment, deployment_id)
    await db.refresh(row)
    return row


def _events(logs: Sequence[Mapping[str, Any]], name: str) -> list[Mapping[str, Any]]:
    # `Mapping`, not `dict`: `capture_logs()` yields `MutableMapping` entries, and all four type
    # gates reject the narrower annotation.
    return [entry for entry in logs if entry["event"] == name]


# --- the happy path ---------------------------------------------------------------


async def test_the_pass_settles_a_stalled_deploy_and_reports_the_count(
    monkeypatch: pytest.MonkeyPatch, db_session: Any, on_duty: None
) -> None:
    """The whole point of moving this onto a clock: a deploy the control plane died in the middle
    of gets settled without anyone pressing anything."""
    deployment_id = await _abandoned(db_session)
    _wire(monkeypatch, db_session, _Arm(fqdn="pub-x.example.io", image=f"reg/app@{_DIGEST}"))

    with capture_logs() as logs:
        await reconcile_stalled_deploys()

    done = _events(logs, DEPLOY_RECONCILE_DONE_EVENT)
    assert len(done) == 1
    assert done[0]["resolved"] == 1
    assert (await _row(db_session, deployment_id)).status is DeploymentStatus.SUCCEEDED


async def test_the_log_line_carries_counts_and_nothing_identifying(
    monkeypatch: pytest.MonkeyPatch, db_session: Any, on_duty: None
) -> None:
    """Counts only (`.claude/rules/security.md`). A deployment id or an app name in the worker's
    log stream is a durable record of who deployed what, in a process that has no reason to know
    either."""
    deployment_id = await _abandoned(db_session)
    _wire(monkeypatch, db_session, _Arm(fqdn="pub-x.example.io", image=f"reg/app@{_DIGEST}"))

    with capture_logs() as logs:
        await reconcile_stalled_deploys()

    done = _events(logs, DEPLOY_RECONCILE_DONE_EVENT)[0]
    assert set(done) == {"event", "log_level", "resolved"}
    assert str(deployment_id) not in str(done)


async def test_a_pass_that_resolves_nothing_still_logs(
    monkeypatch: pytest.MonkeyPatch, db_session: Any, on_duty: None
) -> None:
    """Silence is how an out-of-process worker dies unnoticed. A completed pass is the liveness
    signal (U11 reads its staleness), so "nothing to do" must be distinguishable from "nothing
    ran"."""
    _wire(monkeypatch, db_session, _Arm(fqdn=None))

    with capture_logs() as logs:
        await reconcile_stalled_deploys()

    assert _events(logs, DEPLOY_RECONCILE_DONE_EVENT)[0]["resolved"] == 0


# --- the flag gate ----------------------------------------------------------------


async def test_the_flag_off_returns_before_it_reconciles_anything(
    monkeypatch: pytest.MonkeyPatch, db_session: Any
) -> None:
    """`DEPLOY__RECONCILE_ENABLED=false` is the state every environment ships in, and it has to
    mean the pass does not run — not that it runs and reports zero."""
    monkeypatch.setattr(settings, "deploy", _deploy_config(reconcile_enabled=False))
    await _abandoned(db_session)

    def _must_not_be_called() -> Any:  # pragma: no cover - the assertion is that it isn't
        raise AssertionError("a disabled pass must not resolve a publish client")

    monkeypatch.setattr("src.services.deploy.aca_publish.get_published_apps", _must_not_be_called)

    with capture_logs() as logs:
        await reconcile_stalled_deploys()

    disabled = _events(logs, DEPLOY_RECONCILE_DISABLED_EVENT)
    assert [entry["reason"] for entry in disabled] == ["flag_off"]
    assert not _events(logs, DEPLOY_RECONCILE_DONE_EVENT)


async def test_an_unconfigured_deployment_is_reported_separately_from_a_switched_off_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two different facts for whoever reads the log: "this deployment does not publish at all"
    versus "it does, and an operator silenced the timer". One line covering both would send an
    operator looking for a flag that is not the problem."""
    monkeypatch.setattr(settings, "deploy", None)

    with capture_logs() as logs:
        await reconcile_stalled_deploys()

    assert [entry["reason"] for entry in _events(logs, DEPLOY_RECONCILE_DISABLED_EVENT)] == [
        "unconfigured"
    ]


def test_a_disabled_pass_imports_nothing_heavy() -> None:
    """THE ordering contract, in a fresh interpreter.

    A disabled task must cost structlog, the broker and the settings profile and nothing else, so
    that parking a passenger on this scheduler never taxes a deployment that has not turned it on.
    Nothing raises when this regresses — the task simply works while quietly loading the ORM
    engine and the publish client's Azure credential chain into a process that will not use them.

    Scoped by measurement, not aspiration: `sqlalchemy` and `azure.mgmt.appcontainers` are ALREADY
    in the chassis's import closure (`src.config` reads every capability's config model), so
    naming them here would assert a falsehood about a cost this unit did not add. What is asserted
    is exactly this module's own lazy set — `src.db.base` in particular BUILDS the async engine at
    import — plus `src.main`, which a careless import would add silently.
    """
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-B",
            "-c",
            "import asyncio, sys;"
            " from src.workers.deploy_reconcile import reconcile_stalled_deploys;"
            " asyncio.run(reconcile_stalled_deploys());"
            " heavy = [m for m in ('src.db.base', 'src.services.deploy.reconcile',"
            " 'src.services.deploy.aca_publish', 'src.main') if m in sys.modules];"
            " print('HEAVY:' + ','.join(heavy))",
        ],
        cwd=_BACKEND_ROOT,
        env={"PATH": os.environ["PATH"], "ENV_FILE": ".env.test"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "HEAVY:" in result.stdout, result.stdout
    pulled_in = result.stdout.split("HEAVY:")[1].strip()
    assert pulled_in == "", (
        f"a disabled deploy-reconcile pass imported {pulled_in} — move those imports inside the "
        f"task body, below the flag gate"
    )


# --- what it must leave alone -----------------------------------------------------


async def test_a_deploy_that_is_still_beating_is_left_alone(
    monkeypatch: pytest.MonkeyPatch, db_session: Any, on_duty: None
) -> None:
    """Staleness is measured from `heartbeat_at`, never from `created_at`. A row inside the window
    belongs to a running pipeline, and settling it would put two writers on one deploy."""
    user = await UserFactory.create(db_session)
    app = await AppRegistryFactory.create(db_session, user_id=user.id)
    live = await store.claim(db_session, app_id=app.id, user_id=user.id)
    assert live is not None
    _wire(monkeypatch, db_session, _Arm(fqdn="pub-x.example.io", image=f"reg/app@{_DIGEST}"))

    with capture_logs() as logs:
        await reconcile_stalled_deploys()

    assert _events(logs, DEPLOY_RECONCILE_DONE_EVENT)[0]["resolved"] == 0
    assert (await _row(db_session, live)).status is DeploymentStatus.RUNNING


async def test_an_arm_blip_is_retried_on_the_next_tick_not_counted_as_reconciled(
    monkeypatch: pytest.MonkeyPatch, db_session: Any, on_duty: None
) -> None:
    """THE most expensive answer to get wrong, and the reason this pass belongs on a repeating
    clock at all.

    "ARM could not say" must leave the row exactly as it was — not settled, and not tallied as
    resolved — so the very next tick asks again. Collapsing it into "confirmed absent" would mark
    a live app failed, and reporting it as resolved would mean nobody ever looks again.
    """
    deployment_id = await _abandoned(db_session)
    arm = _Arm(fqdn="pub-x.example.io", image=f"reg/app@{_DIGEST}", blip=True)
    _wire(monkeypatch, db_session, arm)

    with capture_logs() as throttled:
        await reconcile_stalled_deploys()

    assert _events(throttled, DEPLOY_RECONCILE_DONE_EVENT)[0]["resolved"] == 0
    row = await _row(db_session, deployment_id)
    assert row.status is DeploymentStatus.RUNNING
    assert row.failure_code is None

    # The throttle clears. The next tick finds the same row still in the work list and settles it.
    arm.blip = False
    with capture_logs() as recovered:
        await reconcile_stalled_deploys()

    assert _events(recovered, DEPLOY_RECONCILE_DONE_EVENT)[0]["resolved"] == 1
    assert (await _row(db_session, deployment_id)).status is DeploymentStatus.SUCCEEDED


# --- the schedule -----------------------------------------------------------------


def test_the_module_is_registered_with_the_worker() -> None:
    """Taskiq imports only the broker module. An unregistered task module is a queue whose
    messages are enqueued and never consumed — which looks exactly like a dead worker."""
    assert "src.workers.deploy_reconcile" in worker_main._TASK_MODULES


def test_the_schedule_label_is_a_list_of_dicts_with_a_pinned_id() -> None:
    """Two library behaviours in one assertion. A bare dict raises inside
    `LabelScheduleSource.startup()`, and a schedule entry with no `schedule_id` gets a fresh
    `uuid4().hex` per process start — useless as a correlation key and useless for dedupe."""
    schedule = reconcile_stalled_deploys.labels["schedule"]

    assert isinstance(schedule, list)
    assert schedule == [
        {"cron": DEPLOY_RECONCILE_CRON, "schedule_id": DEPLOY_RECONCILE_SCHEDULE_ID}
    ]
    assert "interval" not in schedule[0], (
        "an interval task fires on EVERY process start (last_run is None) and skip_first_run "
        "does not suppress it — at revision-roll frequency that is a pass per deploy"
    )


async def test_the_scheduler_surfaces_the_task_under_its_pinned_id() -> None:
    """End to end through the real schedule source: what the clock will actually read."""
    from taskiq.schedule_sources import LabelScheduleSource

    from src.broker import broker

    source = LabelScheduleSource(broker)
    await source.startup()

    surfaced = source.schedules[DEPLOY_RECONCILE_SCHEDULE_ID]
    assert surfaced.task_name == DEPLOY_RECONCILE_TASK_NAME
    assert surfaced.cron == DEPLOY_RECONCILE_CRON


def test_the_cron_fires_on_the_five_and_not_the_minute_after() -> None:
    at_five = datetime(2026, 8, 11, 12, 5, tzinfo=UTC)

    assert is_cron_task_now(DEPLOY_RECONCILE_CRON, at_five)
    assert not is_cron_task_now(DEPLOY_RECONCILE_CRON, at_five + timedelta(minutes=1))
    # A tick already taken this minute is not taken twice.
    assert not is_cron_task_now(DEPLOY_RECONCILE_CRON, at_five, last_run=at_five)


def test_a_cron_that_would_never_fire_refuses_to_import() -> None:
    """Taskiq validates a cron NOWHERE at decoration time. A malformed one logs a warning every
    second forever and never fires, and — the sneakier half — an evaluator that accepts
    `99 * * * *` gives a schedule that parses and matches no minute ever. Both look identical to a
    healthy schedule from outside the process."""
    with pytest.raises(ValueError, match="does not parse"):
        _honoured_by_the_clock("nonsense")

    with pytest.raises(ValueError, match="matches no minute"):
        _honoured_by_the_clock("99 * * * *")

    # The real one survives its own gate.
    assert _honoured_by_the_clock(DEPLOY_RECONCILE_CRON) == DEPLOY_RECONCILE_CRON


# --- the boot one-shot this unit must NOT have replaced ---------------------------


async def test_the_boot_one_shot_still_runs_before_the_first_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_reconcile_interrupted_deploys` is a BOOT-PATH ONE-SHOT, not a loop, and the scheduled
    pass does not replace it. It settles a deploy that straddled the restart BEFORE the first
    request is served — which no cron can do, because a five-minute tick would leave the citizen
    looking at a Deploy button that 409s for up to `store.DEPLOY_STALE_AFTER_S` (thirty minutes).

    The two are easy to confuse and easy to delete symmetrically. This pins the ordering: the
    reconcile happens, and it happens before anything is served.
    """
    from src.main import create_app, lifespan

    monkeypatch.setattr(settings, "deploy", _deploy_config(reconcile_enabled=True))
    order: list[str] = []

    async def _spy(session_factory: Any, published_apps: Any) -> int:
        order.append("reconciled")
        return 0

    monkeypatch.setattr(
        "src.services.deploy.reconcile.reconcile_stalled_deployments",
        _spy,
    )
    monkeypatch.setattr(
        "src.services.deploy.aca_publish.get_published_apps", lambda: _Arm(fqdn=None)
    )

    app = create_app()
    async with lifespan(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as request:
            served = await request.get("/openapi.json")
            order.append("served")

    assert served.status_code == 200
    assert order == ["reconciled", "served"], (
        "the boot one-shot must settle a straddling deploy before the first request — a cron "
        "cannot, and deleting it symmetrically with the periodic loop is the easy mistake here"
    )
