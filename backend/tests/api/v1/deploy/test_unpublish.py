"""The admin unpublish kill-switch (#113): `POST /v1/admin/apps/{app_id}/unpublish`.

No real Azure anywhere — `PublishedAppRemover` is a `Protocol`
(`services/deploy/aca_publish.py`) exactly so a fake can stand in for it, the same
"no Azure, no network" philosophy `test_aca_publish.py` uses for anything below the ARM
SDK boundary. What's under test here is the route's own state machine (order of checks,
what gets written when, what doesn't), not whether Azure can delete a container app.

Every `Deployment` row is inserted directly rather than driven through a real deploy
pipeline — the pipeline itself is exercised in `test_deploy_routes.py`; this file only
needs rows already in a known terminal shape.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy import event as sa_event
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.db.models.app_registry import AppRegistry
from src.db.models.audit import AuditLog
from src.db.models.deployment import Deployment, DeploymentStatus
from src.services.auth.session_jwt import mint_session_jwt
from src.services.deploy import store
from src.services.deploy.aca_publish import AcaTransientError, PublishedAppRemover
from src.services.deploy.names import published_app_name
from tests.factories import AppRegistryFactory, UserFactory

_TTL = settings.auth.access_ttl_seconds
# `/v1/admin/apps/...`, not `/v1/apps/...`: this is a superadmin lever and it answers in the
# admin namespace with every other one, regardless of which module the code lives in.
_UNPUBLISH = "/v1/admin/apps/{app_id}/unpublish"


def _cookie(jwt: str) -> dict[str, str]:
    return {"Cookie": f"session={jwt}"}


async def _admin(db: AsyncSession) -> dict[str, str]:
    # The .env.test allowlist contains admin@bial.com -> super-admin.
    user = await UserFactory.create(db, email="admin@bial.com")
    return _cookie(mint_session_jwt(user.id, user.token_version, _TTL))


async def _citizen(db: AsyncSession) -> dict[str, str]:
    user = await UserFactory.create(db, email="nobody@rvaiglobal.com")
    return _cookie(mint_session_jwt(user.id, user.token_version, _TTL))


class _Unset:
    """Distinguishes "caller said nothing" from "caller said NULL" for the fixture below."""


_UNSET = _Unset()


async def _deployment(
    db: AsyncSession,
    *,
    app_id: uuid.UUID,
    user_id: uuid.UUID,
    status: DeploymentStatus = DeploymentStatus.SUCCEEDED,
    unpublished_at: datetime | None = None,
    # DEFAULT-SENTINEL, not `None`: a caller must be able to ask for a row whose
    # `container_app_name` really is NULL — the shape a deploy leaves behind when it dies
    # inside `create_or_update`, after the container exists but before `_advance` records
    # its name. Passing `None` explicitly is how a test reaches that case.
    container_app_name: str | None | _Unset = _UNSET,
) -> Deployment:
    row = Deployment(
        app_id=app_id,
        user_id=user_id,
        status=status,
        image_digest="sha256:" + "ab" * 32,
        container_app_name=(
            published_app_name(app_id)
            if isinstance(container_app_name, _Unset)
            else container_app_name
        ),
        url=f"https://{published_app_name(app_id)}.example/",
        unpublished_at=unpublished_at,
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return row


class FakeRemover:
    """Records every `delete_app` call; can be told to raise a bounded number of times,
    matching a real transient ARM failure that a retry then clears.

    Raises `AcaTransientError`, not a bare `RuntimeError`: `sweep_published_apps` catches
    `Exception` so the route cannot tell the difference, but a double that models a failure
    the real client can never produce is a double that can drift silently. The real
    `delete_app` raises `AcaError`/`AcaTransientError` from `_call`'s funnel, or returns
    None on a 404 (`absent_is_none=True`) — see `AbsentRemover` for that second shape."""

    def __init__(self, *, fail_times: int = 0) -> None:
        self.calls: list[uuid.UUID] = []
        self._fail_times = fail_times

    async def delete_app(self, *, app_id: uuid.UUID) -> None:
        self.calls.append(app_id)
        if self._fail_times > 0:
            self._fail_times -= 1
            raise AcaTransientError("simulated ACA delete failure")


class AbsentRemover(FakeRemover):
    """The already-gone container. The real `delete_app` passes `absent_is_none=True`, so a
    404 from ARM returns None rather than raising — which `sweep_published_apps` counts as
    swept. Behaviourally identical to a successful delete here, and that is the point being
    pinned: a non-zero sweep count means "no error", NOT "something was deleted"."""


class RacingRemover(FakeRemover):
    """Stamps `unpublished_at` from underneath, during the ARM call.

    This is the only honest way to reach the route's lost-race branch: the route reads
    `row.unpublished_at` BEFORE the sweep and calls `store.unpublish` AFTER it, so a
    concurrent unpublish that lands in that window is exactly a write issued from inside
    `delete_app`. A raw UPDATE, not the ORM object, because the route's in-memory `row` must
    stay stale — that staleness is the race."""

    def __init__(self, db: AsyncSession, deployment_id: uuid.UUID, *, at: datetime) -> None:
        super().__init__()
        self._db = db
        self._deployment_id = deployment_id
        self._at = at

    async def delete_app(self, *, app_id: uuid.UUID) -> None:
        await super().delete_app(app_id=app_id)
        await self._db.execute(
            sa.update(Deployment)
            .where(Deployment.id == self._deployment_id)
            .values(unpublished_at=self._at)
        )


class DeletingRemover(FakeRemover):
    """Deletes the whole app from underneath, during the ARM call.

    The realistic trigger for the vanished-row case: an admin unpublishes, and while the
    minutes-long delete runs someone lands `DELETE /v1/admin/apps/{id}`, whose CASCADE takes
    the `deployments` rows with it. Like `RacingRemover`, the write has to be issued from
    inside `delete_app` to land in the route's own window."""

    def __init__(self, db: AsyncSession, app_id: uuid.UUID) -> None:
        super().__init__()
        self._db = db
        self._app_id = app_id

    async def delete_app(self, *, app_id: uuid.UUID) -> None:
        await super().delete_app(app_id=app_id)
        await self._db.execute(sa.delete(AppRegistry).where(AppRegistry.id == self._app_id))


class AuditSpyRemover(FakeRemover):
    """Reads the audit table from inside the ARM call, to prove the accountability row was
    COMMITTED before Azure was ever touched — the whole point of the audit-first ordering,
    and the only thing that survives a gateway 504 mid-delete."""

    def __init__(self, db: AsyncSession) -> None:
        super().__init__()
        self._db = db
        self.actions_visible_during_delete: list[str] = []

    async def delete_app(self, *, app_id: uuid.UUID) -> None:
        await super().delete_app(app_id=app_id)
        rows = (await self._db.execute(sa.select(AuditLog.action))).scalars().all()
        self.actions_visible_during_delete = list(rows)


class CommitCountingRemover(FakeRemover):
    """Counts COMMITS, not rows — the distinction `AuditSpyRemover` structurally cannot make.

    `conftest.py` hands the route the test's own `db_session`, so an uncommitted audit row is
    visible to a reader on that same session either way. Reading the table therefore proves
    the append happened, never that it was made durable — and durability is the entire point
    of the audit-first ordering, since what has to survive is the request dying at the gateway
    mid-ARM-delete. Wrapping `commit` is the only vantage point that can tell the two apart."""

    def __init__(self, db: AsyncSession) -> None:
        super().__init__()
        self.commits_before_delete = -1
        self._count = 0
        # SQLAlchemy's own `after_commit` hook rather than wrapping `db.commit`: the event
        # fires for the real commit on the underlying sync session, so it cannot be fooled by
        # a caller that reaches past the wrapper, and it needs no assignment to a bound method.
        sa_event.listen(db.sync_session, "after_commit", self._on_commit)

    def _on_commit(self, _session: object) -> None:
        self._count += 1

    async def delete_app(self, *, app_id: uuid.UUID) -> None:
        await super().delete_app(app_id=app_id)
        self.commits_before_delete = self._count


def _wire(app, remover: PublishedAppRemover) -> None:
    from src.api.v1.deploy.deps import published_app_remover_or_none

    app.dependency_overrides[published_app_remover_or_none] = lambda: remover


async def _owned_app(db: AsyncSession):
    owner = await UserFactory.create(db, email="builder@rvaiglobal.com")
    app_row = await AppRegistryFactory.create(db, user_id=owner.id)
    return owner, app_row


async def test_happy_path_unpublishes_and_audits(app, client, db_session) -> None:
    admin_headers = await _admin(db_session)
    owner, app_row = await _owned_app(db_session)
    deployment = await _deployment(db_session, app_id=app_row.id, user_id=owner.id)
    remover = FakeRemover()
    _wire(app, remover)

    resp = await client.post(_UNPUBLISH.format(app_id=app_row.id), headers=admin_headers)

    assert resp.status_code == 200
    body = resp.json()
    assert body["appId"] == str(app_row.id)
    assert body["deploymentId"] == str(deployment.id)
    assert body["unpublishedAt"]
    # Pins router.py: `swept = await sweep_published_apps([app_id], client=remover)` — revert
    # that call (e.g. skip straight to `store.unpublish`) and this goes to 0.
    assert remover.calls == [app_row.id]

    row = await db_session.get(Deployment, deployment.id)
    assert row is not None
    assert row.unpublished_at is not None

    # ONE row, not two. The pre-ARM `unpublish` row already carries the whole ADR-0005
    # payload; "and it worked" is durable in `unpublished_at` and the log line, so a second
    # success row would only double the volume of the most-read resource type.
    # Pins router.py's single `append_audit` on the success path.
    audit = (
        await db_session.execute(sa.select(AuditLog).where(AuditLog.action == "unpublish"))
    ).scalar_one()
    assert audit.resource_type == "app"
    assert audit.resource_id == str(app_row.id)
    assert audit.detail["deploymentId"] == str(deployment.id)
    assert audit.detail["projectId"] == str(app_row.project_id)
    assert audit.detail["containerAppName"] == deployment.container_app_name
    assert audit.detail["deploymentStatus"] == DeploymentStatus.SUCCEEDED.value
    # No `unpublish:unconfirmed` sibling — that pair is what distinguishes a clean run from a
    # teardown this request never observed complete.
    assert (
        await db_session.execute(
            sa.select(AuditLog).where(AuditLog.action == "unpublish:unconfirmed")
        )
    ).scalar_one_or_none() is None


async def test_idempotent_repeat_does_not_call_azure_again(app, client, db_session) -> None:
    admin_headers = await _admin(db_session)
    owner, app_row = await _owned_app(db_session)
    await _deployment(db_session, app_id=app_row.id, user_id=owner.id)
    remover = FakeRemover()
    _wire(app, remover)

    first = await client.post(_UNPUBLISH.format(app_id=app_row.id), headers=admin_headers)
    second = await client.post(_UNPUBLISH.format(app_id=app_row.id), headers=admin_headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["unpublishedAt"] == second.json()["unpublishedAt"]
    # Pins router.py: `if row.unpublished_at is not None: return ... ` (the skip branch) —
    # remove that early return and this becomes 2.
    assert len(remover.calls) == 1


async def test_an_unobserved_teardown_leaves_unpublished_at_unset_and_retry_succeeds(
    app, client, db_session
) -> None:
    """A sweep that comes back empty is recorded as UNCONFIRMED, never as failed.

    `sweep_published_apps` collapses every exception into a count, so a zero covers both a
    terminal `AcaError` (ARM refused; the container really is still up) and an
    `AcaTransientError` raised by `await_lro` on ceiling expiry — whose docstring says the
    outcome is genuinely unknown because "the operation may still land". The second case is
    the LIKELY one here: the ceiling is 300s and the edge gateway gives up at 20. Asserting
    "could not be removed" would be the mirror image of the unobserved-success row this
    route's whole audit discipline exists to avoid.

    Mutation receipt: rename the action back to `unpublish:failed` (or restore the "could not
    be removed. Please try again." copy) and this goes red."""
    admin_headers = await _admin(db_session)
    owner, app_row = await _owned_app(db_session)
    deployment = await _deployment(db_session, app_id=app_row.id, user_id=owner.id)
    remover = FakeRemover(fail_times=1)
    _wire(app, remover)

    failed = await client.post(_UNPUBLISH.format(app_id=app_row.id), headers=admin_headers)

    assert failed.status_code == 503
    body = failed.json()["error"]
    # Machine-readable, so a client can tell "retry is right" from "retry can never help"
    # without string-matching prose.
    assert body["code"] == "teardown_unconfirmed"
    assert "could not be confirmed" in body["message"]

    row = await db_session.get(Deployment, deployment.id)
    assert row is not None
    # Pins router.py: `if ... == 0: raise AppApiError(503, ...)` running BEFORE
    # `store.unpublish`. Leaving the stamp unwritten is the conservative choice — stamping on
    # an unknown outcome could mark an app down that is still serving.
    assert row.unpublished_at is None

    # The ATTEMPT is still on record, because it was committed before Azure was called. This
    # is the accountability contract (ADR-0005): an admin who pressed the button and got a 503
    # must not leave an empty audit log behind — that was the gap where a repeated failing
    # episode was invisible.
    # Pins router.py: move the `append_audit(action="unpublish")` + `db.commit()` back below
    # the sweep and this goes red.
    attempted = (
        await db_session.execute(sa.select(AuditLog).where(AuditLog.action == "unpublish"))
    ).scalar_one()
    assert attempted.resource_id == str(app_row.id)
    # …and the outcome is recorded too, so "attempted" is not mistaken for "succeeded".
    unconfirmed = (
        await db_session.execute(
            sa.select(AuditLog).where(AuditLog.action == "unpublish:unconfirmed")
        )
    ).scalar_one()
    assert unconfirmed.detail["reason"] == "teardown_unconfirmed"

    # Retry: the fake no longer raises (fail_times exhausted), same as a transient ARM
    # error clearing. Proves the design's retry-safety claim, not just the 503 itself.
    retried = await client.post(_UNPUBLISH.format(app_id=app_row.id), headers=admin_headers)
    assert retried.status_code == 200
    assert len(remover.calls) == 2


async def test_blocked_while_a_deploy_is_in_flight(app, client, db_session) -> None:
    admin_headers = await _admin(db_session)
    owner, app_row = await _owned_app(db_session)
    succeeded = await _deployment(db_session, app_id=app_row.id, user_id=owner.id)
    await _deployment(
        db_session, app_id=app_row.id, user_id=owner.id, status=DeploymentStatus.RUNNING
    )
    remover = FakeRemover()
    _wire(app, remover)

    resp = await client.post(_UNPUBLISH.format(app_id=app_row.id), headers=admin_headers)

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "deploy_in_flight"
    # Pins router.py: `if await store.in_flight(db, app_id=app_id) is not None: raise ...`
    # running BEFORE the teardown call — remove that check and `remover.calls` becomes
    # non-empty, and the line below fails.
    assert remover.calls == []
    row = await db_session.get(Deployment, succeeded.id)
    assert row is not None
    assert row.unpublished_at is None


async def test_never_deployed_is_a_409(app, client, db_session) -> None:
    """NO deployment row at all — the one state in which no container can provably exist,
    since `pub-<app_id>` is only ever created by a pipeline that owns a row, and rows leave
    only by CASCADE with the app itself (which is a 404 here). So this stays a refusal
    rather than a blind sweep, and `UnpublishResponse` keeps its required fields."""
    admin_headers = await _admin(db_session)
    _owner, app_row = await _owned_app(db_session)
    remover = FakeRemover()
    _wire(app, remover)

    resp = await client.post(_UNPUBLISH.format(app_id=app_row.id), headers=admin_headers)

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "never_deployed"
    assert remover.calls == []


async def test_non_admin_is_forbidden(app, client, db_session) -> None:
    citizen_headers = await _citizen(db_session)
    owner, app_row = await _owned_app(db_session)
    deployment = await _deployment(db_session, app_id=app_row.id, user_id=owner.id)
    remover = FakeRemover()
    _wire(app, remover)

    resp = await client.post(_UNPUBLISH.format(app_id=app_row.id), headers=citizen_headers)

    assert resp.status_code == 403
    assert remover.calls == []
    row = await db_session.get(Deployment, deployment.id)
    assert row is not None
    assert row.unpublished_at is None


async def test_republish_restores_the_app_at_the_same_url(db_session: AsyncSession) -> None:
    """Structural proof, at the store layer: `unpublished_at` lives on the ROW, so a later
    successful deploy — a NEW row — is what the route's resolution returns, unpublished or
    not. Pins `store.latest_for_app`'s `id DESC` ordering: if it ever returned the OLDEST
    row instead, this would return the unpublished one and republish would appear broken."""
    from src.services.deploy import store

    owner = await UserFactory.create(db_session, email="repub@rvaiglobal.com")
    app_row = await AppRegistryFactory.create(db_session, user_id=owner.id)

    first = await _deployment(db_session, app_id=app_row.id, user_id=owner.id)
    await store.unpublish(db_session, first.id, at=datetime.now(UTC))

    second = await _deployment(db_session, app_id=app_row.id, user_id=owner.id)

    current = await store.latest_for_app(db_session, app_id=app_row.id)
    assert current is not None
    assert current.id == second.id
    assert current.unpublished_at is None
    # Same URL: the container name is a pure function of the immutable app id, unaffected
    # by which deployment row is "current".
    assert current.container_app_name == first.container_app_name


async def test_a_failed_deploy_can_still_own_a_live_container_and_is_torn_down(
    app, client, db_session
) -> None:
    """The orphan case, and the one the lever is most obviously needed for. The pipeline
    calls `create_or_update` at step 5 and only THEN awaits the revision, so an attempt that
    settles FAILED at step 6 leaves `pub-<app_id>` running, externally addressable, holding
    the app's database URL and Blob SAS, and billing.

    Resolving through `store.last_successful` — as this route originally did — answered "this
    app has never been published, there is nothing to unpublish" while exactly that container
    served traffic. Resolving through `store.latest_for_app` tears it down.

    Mutation receipt: swap `latest_for_app` back to `last_successful` in router.py and this
    goes red with a 409 `never_deployed`."""
    admin_headers = await _admin(db_session)
    owner, app_row = await _owned_app(db_session)
    failed = await _deployment(
        db_session, app_id=app_row.id, user_id=owner.id, status=DeploymentStatus.FAILED
    )
    remover = FakeRemover()
    _wire(app, remover)

    resp = await client.post(_UNPUBLISH.format(app_id=app_row.id), headers=admin_headers)

    assert resp.status_code == 200
    assert remover.calls == [app_row.id]
    row = await db_session.get(Deployment, failed.id)
    assert row is not None
    # The stamp lands on the FAILED row, and means what it says: THIS is the attempt whose
    # container was torn down.
    assert row.unpublished_at is not None

    audit = (
        await db_session.execute(sa.select(AuditLog).where(AuditLog.action == "unpublish"))
    ).scalar_one()
    # Recorded so an operator sees the interesting case without joining back to `deployments`.
    assert audit.detail["deploymentStatus"] == DeploymentStatus.FAILED.value


async def test_a_redeploy_that_failed_after_an_unpublish_is_still_torn_down(
    app, client, db_session
) -> None:
    """The subtler half of the same bug. History: succeeded (then unpublished) -> redeploy
    that FAILED at the readiness check, re-creating the container.

    `last_successful` returns the OLD succeeded row, which is already stamped, so the route
    took its already-down early return and answered 200 while the re-created container was
    live. Reading the newest row instead makes "is this app live" a question about the latest
    attempt, which is the only reading that can be right."""
    admin_headers = await _admin(db_session)
    owner, app_row = await _owned_app(db_session)
    old = await _deployment(
        db_session,
        app_id=app_row.id,
        user_id=owner.id,
        unpublished_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    newest = await _deployment(
        db_session, app_id=app_row.id, user_id=owner.id, status=DeploymentStatus.FAILED
    )
    remover = FakeRemover()
    _wire(app, remover)

    resp = await client.post(_UNPUBLISH.format(app_id=app_row.id), headers=admin_headers)

    assert resp.status_code == 200
    assert resp.json()["deploymentId"] == str(newest.id)
    # Azure WAS called — the early return would have skipped it entirely.
    assert remover.calls == [app_row.id]
    fresh = await db_session.get(Deployment, newest.id)
    assert fresh is not None
    assert fresh.unpublished_at is not None
    # The old row keeps the timestamp it already had; it is not maintained afterwards.
    stale = await db_session.get(Deployment, old.id)
    assert stale is not None
    assert stale.unpublished_at == datetime(2026, 8, 1, tzinfo=UTC)


async def test_app_not_found_is_404(app, client, db_session) -> None:
    admin_headers = await _admin(db_session)
    remover = FakeRemover()
    _wire(app, remover)

    resp = await client.post(_UNPUBLISH.format(app_id=uuid.uuid4()), headers=admin_headers)

    assert resp.status_code == 404


async def test_unpublish_store_write_does_not_commit_on_its_own(db_session) -> None:
    """Review finding on #120: `store.unpublish` used to `db.commit()` on its own, which
    took the transaction boundary away from the route that owns it.

    Note what this does and does not claim NOW. The original fix was described as making the
    stamp share one trailing commit with `append_audit`; the later audit-first change moved
    the accountability row to a commit BEFORE the Azure call, so the two writes are
    deliberately in different transactions and the route sequences three commits in total.
    What survives — and what this test pins — is narrower and still load-bearing: the store
    function must not commit, because the route branches on its return value and decides
    where the next boundary falls. A commit in here would fire in the middle of that.

    Testing this end to end through the HTTP layer doesn't work in this suite: `db_session`
    (conftest.py) binds the whole test to ONE already-open connection-level transaction, so
    a mid-test `session.rollback()` unwinds back to the test's own start — including fixture
    setup — not just the request's writes, making a commit/then-fail/then-rollback dance
    indistinguishable from a plain reset. Spying on `db.commit` directly is what actually
    isolates the claim: `store.unpublish` itself must never call it, full stop — the router
    (already covered by `test_happy_path_unpublishes_and_audits`, which needs BOTH the row
    and the audit entry to appear) is the only place a commit is allowed to happen.

    Mutation receipt: restoring the deleted `await db.commit()` in `store.unpublish`
    (services/deploy/store.py) turns this red — `commits` stops being empty."""
    from src.services.deploy import store

    owner = await UserFactory.create(db_session, email="spy-owner@rvaiglobal.com")
    app_row = await AppRegistryFactory.create(db_session, user_id=owner.id)
    deployment = await _deployment(db_session, app_id=app_row.id, user_id=owner.id)

    commits = 0
    real_commit = db_session.commit

    async def spy_commit() -> None:
        nonlocal commits
        commits += 1
        await real_commit()

    db_session.commit = spy_commit
    try:
        settled = await store.unpublish(db_session, deployment.id, at=datetime.now(UTC))
    finally:
        db_session.commit = real_commit

    assert settled is True
    assert commits == 0


async def test_store_unpublish_stamps_exactly_once(db_session) -> None:
    """The idempotency predicate itself, at the store layer.

    The router-level repeat test cannot reach this: it short-circuits at the route's
    `if row.unpublished_at is not None` early return, so `store.unpublish` is never called a
    second time and the `WHERE unpublished_at IS NULL` clause is never exercised. That clause
    is the whole concurrency story — it is what makes the return value, rather than a
    (necessarily stale) prior read, the authority on who won.

    Mutation receipt: drop `Deployment.unpublished_at.is_(None)` from the `.where()` in
    services/deploy/store.py so the UPDATE matches on id alone, and this goes red — the
    second call starts returning True and overwrites the first timestamp."""
    from src.services.deploy import store

    owner = await UserFactory.create(db_session, email="twice@rvaiglobal.com")
    app_row = await AppRegistryFactory.create(db_session, user_id=owner.id)
    deployment = await _deployment(db_session, app_id=app_row.id, user_id=owner.id)

    first_at = datetime(2026, 8, 12, 9, 0, tzinfo=UTC)
    second_at = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)

    assert await store.unpublish(db_session, deployment.id, at=first_at) is True
    assert await store.unpublish(db_session, deployment.id, at=second_at) is False

    await db_session.refresh(deployment)
    # The FIRST timestamp survives: a later call must not silently rewrite when the takedown
    # actually happened.
    assert deployment.unpublished_at == first_at


async def test_losing_the_race_still_records_this_admins_action(app, client, db_session) -> None:
    """Two admins hit the lever for the same incident. The loser's guarded UPDATE touches
    zero rows, so it reports the WINNER's timestamp rather than its own — but its own attempt
    is still on record, because the audit row was committed before Azure was called.

    That is the gap the ordering closes: previously this branch returned 200 having really
    called `delete_app`, and wrote nothing at all, so the second admin's action was invisible
    everywhere.

    Mutation receipt: delete the whole `if not await store.unpublish(...)` branch in
    router.py and this goes red — the response reports `now` instead of the winner's stamp."""
    admin_headers = await _admin(db_session)
    owner, app_row = await _owned_app(db_session)
    deployment = await _deployment(db_session, app_id=app_row.id, user_id=owner.id)
    winner_at = datetime(2026, 8, 12, 8, 30, tzinfo=UTC)
    remover = RacingRemover(db_session, deployment.id, at=winner_at)
    _wire(app, remover)

    resp = await client.post(_UNPUBLISH.format(app_id=app_row.id), headers=admin_headers)

    assert resp.status_code == 200
    # The winner's timestamp, not this call's own unwritten one.
    assert resp.json()["unpublishedAt"].startswith("2026-08-12T08:30")
    # It really did call Azure — which is exactly why the attempt must be audited.
    assert remover.calls == [app_row.id]
    audit = (
        await db_session.execute(sa.select(AuditLog).where(AuditLog.action == "unpublish"))
    ).scalar_one()
    assert audit.resource_id == str(app_row.id)


async def test_an_app_deleted_mid_teardown_is_a_404_not_a_500(app, client, db_session) -> None:
    """A zero-row stamp has two causes, and only one of them is the race.

    If a concurrent `DELETE /v1/admin/apps/{id}` cascades the deployment row away while the
    ARM delete runs, `store.unpublish` also touches zero rows — and `db.refresh(row)` would
    then raise `ObjectDeletedError`, escaping as an undocumented 500 on a request whose
    teardown actually SUCCEEDED. Re-reading with `db.get(..., populate_existing=True)` turns
    that into the 404 this route already documents, which by then is simply true.

    Mutation receipt: swap the re-read back to `await db.refresh(row)` and this goes red with
    a 500."""
    admin_headers = await _admin(db_session)
    owner, app_row = await _owned_app(db_session)
    await _deployment(db_session, app_id=app_row.id, user_id=owner.id)
    remover = DeletingRemover(db_session, app_row.id)
    _wire(app, remover)

    resp = await client.post(_UNPUBLISH.format(app_id=app_row.id), headers=admin_headers)

    assert resp.status_code == 404
    # The container really was swept — the admin's intent held, and the app's own delete
    # sweeps the same container anyway. Only the row to report is gone.
    assert remover.calls == [app_row.id]


async def test_the_attempt_is_audited_before_azure_is_touched(app, client, db_session) -> None:
    """The 504 guarantee, pinned. An ARM delete is bounded at `provision_timeout_s` (300s)
    behind an edge gateway that gives up at twenty, so the request can simply never return —
    and a request that never returns cannot audit anything on its way out. The accountability
    row therefore has to be durable BEFORE the sweep, not after it.

    Mutation receipt: move `append_audit` + `db.commit()` below the sweep in router.py and
    `actions_visible_during_delete` becomes empty."""
    admin_headers = await _admin(db_session)
    owner, app_row = await _owned_app(db_session)
    await _deployment(db_session, app_id=app_row.id, user_id=owner.id)
    remover = AuditSpyRemover(db_session)
    _wire(app, remover)

    resp = await client.post(_UNPUBLISH.format(app_id=app_row.id), headers=admin_headers)

    assert resp.status_code == 200
    assert "unpublish" in remover.actions_visible_during_delete


async def test_an_already_absent_container_still_settles_the_row(app, client, db_session) -> None:
    """`delete_app` passes `absent_is_none=True`, so ARM's 404 returns None instead of
    raising and `sweep_published_apps` counts it as swept. This is what makes retry converge
    after a partial failure — and it is also why a non-zero count means "no error", not
    "something was deleted". The route treats the two identically on purpose: its job is to
    guarantee absence, not to prove it personally caused it."""
    admin_headers = await _admin(db_session)
    owner, app_row = await _owned_app(db_session)
    deployment = await _deployment(db_session, app_id=app_row.id, user_id=owner.id)
    remover = AbsentRemover()
    _wire(app, remover)

    resp = await client.post(_UNPUBLISH.format(app_id=app_row.id), headers=admin_headers)

    assert resp.status_code == 200
    row = await db_session.get(Deployment, deployment.id)
    assert row is not None
    assert row.unpublished_at is not None


async def test_publishing_unconfigured_is_a_503_that_does_not_say_try_again(
    app, client, db_session
) -> None:
    """`DEPLOY__*` unset: the provider yields None rather than raising (a raising one would
    resolve BEFORE the route body and escape its error handling as a 500 with the wrong
    envelope), and the body has to interpret that None itself.

    Without the guard, None flows into `sweep_published_apps`, which re-resolves the
    singleton, catches `DeployNotConfiguredError` and returns 0 — landing in the
    unconfirmed-teardown branch and telling the admin to retry on an environment where
    retrying can never work.

    Both 503s therefore carry a `code`: this one is terminal, the other is worth retrying, and
    they are otherwise indistinguishable to a client that will not parse prose.

    Mutation receipt: remove `if remover is None:` from router.py and the code flips to
    `teardown_unconfirmed`."""
    admin_headers = await _admin(db_session)
    owner, app_row = await _owned_app(db_session)
    await _deployment(db_session, app_id=app_row.id, user_id=owner.id)
    from src.api.v1.deploy.deps import published_app_remover_or_none

    app.dependency_overrides[published_app_remover_or_none] = lambda: None

    resp = await client.post(_UNPUBLISH.format(app_id=app_row.id), headers=admin_headers)

    assert resp.status_code == 503
    body = resp.json()["error"]
    assert body["code"] == "publishing_unavailable"
    message = body["message"]
    assert "not switched on" in message
    assert "try again" not in message.lower()
    # Nothing was attempted, so nothing is audited — this is a configuration refusal, not an
    # admin action that failed.
    assert (
        await db_session.execute(sa.select(AuditLog).where(AuditLog.action.like("unpublish%")))
    ).scalars().all() == []


async def test_the_citizen_read_surface_reports_the_takedown(app, client, db_session) -> None:
    """#2 of the re-review: `unpublished_at` was write-only on the wire. The POST response
    carried it, but `GET /v1/projects/{id}/deployment` — the one surface the portal actually
    polls — did not, so a killed app kept rendering as live with a clickable dead URL.

    Mutation receipt: `schemas.py` `unpublished_at=row.unpublished_at` -> `unpublished_at=None`
    and this goes red while every other test stays green."""
    admin_headers = await _admin(db_session)
    owner, app_row = await _owned_app(db_session)
    await _deployment(db_session, app_id=app_row.id, user_id=owner.id)
    _wire(app, FakeRemover())
    # The citizen GET is gated on `DEPLOY__*` being configured (it 503s otherwise), which the
    # test env deliberately leaves unset. Overriding the dependency is how `test_deploy_routes`
    # reaches this route too — the object itself is never called on this path, only its
    # presence is checked.
    from src.api.v1.deploy.deps import deploy_service_or_none

    app.dependency_overrides[deploy_service_or_none] = lambda: object()
    owner_headers = _cookie(mint_session_jwt(owner.id, owner.token_version, _TTL))

    assert (
        await client.post(_UNPUBLISH.format(app_id=app_row.id), headers=admin_headers)
    ).status_code == 200

    resp = await client.get(f"/v1/projects/{app_row.project_id}/deployment", headers=owner_headers)

    assert resp.status_code == 200
    body = resp.json()
    # The takedown is a SECOND AXIS, so both halves have to be observable at once: the row is
    # still a successful deploy, and it is also down. A client that can only see `status`
    # cannot tell this apart from a live app, which is the bug.
    assert body["status"] == DeploymentStatus.SUCCEEDED.value
    assert body["unpublishedAt"]


async def test_the_audit_row_is_committed_before_azure_is_touched(app, client, db_session) -> None:
    """The durability half of the 504 guarantee. Its sibling above pins the ORDER of
    `append_audit` against the sweep; this pins the COMMIT, which is the part that actually
    survives the request dying mid-ARM-delete.

    Mutation receipt: delete ONLY `await db.commit()` in router.py (leave `append_audit`
    exactly where it is) and this goes red — the ordering test alone stays green, which is
    why it needed a sibling."""
    admin_headers = await _admin(db_session)
    owner, app_row = await _owned_app(db_session)
    await _deployment(db_session, app_id=app_row.id, user_id=owner.id)
    remover = CommitCountingRemover(db_session)
    _wire(app, remover)

    resp = await client.post(_UNPUBLISH.format(app_id=app_row.id), headers=admin_headers)

    assert resp.status_code == 200
    # EXACTLY one, not "at least one". Nothing in this file's fixtures commits — they insert
    # and flush inside the test's own transaction — so the only commit that can land before
    # the sweep is the route's durability boundary. Asserting the exact count means a fixture
    # that starts committing later fails this loudly instead of quietly satisfying it, which
    # is the failure mode that made the ORIGINAL version of this test pass while testing
    # nothing.
    assert remover.commits_before_delete == 1


async def test_the_audit_names_the_container_even_when_the_row_never_recorded_one(
    app, client, db_session
) -> None:
    """The case the nine-line comment in router.py exists for, and the only one that can tell
    the derivation apart from a column read: a deploy that died inside `create_or_update`
    leaves `container_app_name` NULL over a container that really exists. That is precisely
    the incident where an operator needs to know which container was targeted.

    Mutation receipt: `published_app_name(app_id)` -> `row.container_app_name` in router.py
    and this goes red (None); every other test in this file still passes, because they all
    seed the column with the derived value."""
    admin_headers = await _admin(db_session)
    owner, app_row = await _owned_app(db_session)
    await _deployment(db_session, app_id=app_row.id, user_id=owner.id, container_app_name=None)
    _wire(app, FakeRemover())

    resp = await client.post(_UNPUBLISH.format(app_id=app_row.id), headers=admin_headers)

    assert resp.status_code == 200
    audit = (
        await db_session.execute(sa.select(AuditLog).where(AuditLog.action == "unpublish"))
    ).scalar_one()
    assert audit.detail["containerAppName"] == published_app_name(app_row.id)


async def test_settling_a_running_row_clears_a_takedown_stamp_it_raced_into(
    db_session: AsyncSession,
) -> None:
    """#3 of the re-review: the kill-switch jamming on a live app.

    `unpublish` resolves through `latest_for_app`, which has no status predicate, so a
    takedown landing in the window after `in_flight` returned None can stamp the NEW running
    row — the one whose pipeline is at that moment publishing the container. If the stamp
    survives the settle, the portal reports "Taken down" over a genuinely live app AND every
    later unpublish takes the idempotent early return, never calling Azure again.

    Mutation receipt: drop `unpublished_at=None` from `_finish`'s `.values(...)` and this
    goes red."""
    owner, app_row = await _owned_app(db_session)
    running = await _deployment(
        db_session,
        app_id=app_row.id,
        user_id=owner.id,
        status=DeploymentStatus.RUNNING,
        unpublished_at=datetime.now(UTC),
    )

    assert await store.succeed(db_session, running.id, url="https://x.example/") is True

    await db_session.refresh(running)
    assert running.status is DeploymentStatus.SUCCEEDED
    assert running.unpublished_at is None


async def test_settling_never_erases_a_takedown_on_an_already_finished_row(
    db_session: AsyncSession,
) -> None:
    """The other side of the guard above: clearing must not be able to un-kill a real
    takedown. `_finish` is guarded on `RUNNING`, so a settled row is untouchable — a late
    pipeline write cannot resurrect an app an admin took down."""
    owner, app_row = await _owned_app(db_session)
    settled = await _deployment(
        db_session,
        app_id=app_row.id,
        user_id=owner.id,
        status=DeploymentStatus.SUCCEEDED,
        unpublished_at=datetime.now(UTC),
    )

    assert await store.succeed(db_session, settled.id, url="https://x.example/") is False

    await db_session.refresh(settled)
    assert settled.unpublished_at is not None
