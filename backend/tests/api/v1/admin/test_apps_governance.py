"""Admin app-registry governance: super-admin-only +
audited, the exact state machine, the reviewed-submission-id approve guard,
the artifact-exists pin check and the audited bundle download. That approving publishes is
`test_approve_publishes.py`'s."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import storage_dependency, storage_or_none_dependency
from src.config import settings
from src.db.models.app_registry import AppRegistry, AppStatus
from src.db.models.audit import AuditLog
from src.db.models.deleted_project import MIN_DELETE_REMARK_WORDS
from src.db.models.deployment import Deployment
from src.db.models.project import Project
from src.db.models.project_database import ProjectDatabase
from src.main import create_app
from src.services.appserving.governance import nuke_app
from src.services.auth.csrf import issue_csrf_token
from src.services.auth.session_jwt import mint_session_jwt
from src.services.storage import AppContainerStore, StorageError, snapshot_key, submission_key
from tests.factories import AppRegistryFactory, ProjectFactory, UserFactory
from tests.fakes import FakeStorage

_TTL = settings.auth.access_ttl_seconds
_SHA = "1f" * 20  # 40 lowercase hex chars — the shape the bundle parser guarantees
# A rejection note that clears the floor (20 chars, trimmed). The floor itself and
# every way of failing it are pinned in `test_queue_counts.py`; here the note is just a
# valid input, so the state-machine tests stay about the state machine.
_NOTE = "This one needs a named data owner before it goes live."


class _RecordingContainerStore(AppContainerStore):
    """A per-app container store double that records the containers it was asked to delete.
    Deliberately skips `AppContainerStore.__init__` (no Azure config) — the sweep only calls
    `delete_container`, so the un-set `_config` is never touched."""

    def __init__(self) -> None:  # noqa: D107 — no super().__init__ on purpose (see class docstring)
        self.deleted: list[uuid.UUID] = []

    async def delete_container(self, app_id: uuid.UUID) -> None:
        self.deleted.append(app_id)


class _ExplodingHeadStorage(FakeStorage):
    """A store whose `head()` fails TRANSIENTLY (not not-found) — approve's
    verify-before-pin seam must map a storage ERROR to 503 (ambiguity denies), never a
    409 (absent). Mirrors `_ExplodingGetStorage`/`_ExplodingPutStorage` in test_lifecycle."""

    async def head(self, key):
        raise StorageError("head blip", provider="fake", key=key)


class _ExplodingSignedUrlStorage(FakeStorage):
    """A store whose signed-URL mint explodes — bundle-url's fail-closed twin: a storage
    ERROR is 503, and no bearer URL (or audit row) is produced."""

    async def _signed_read_url_impl(self, key, *, expires_in):
        raise StorageError("sign blip", provider="fake", key=key)


def _cookie(jwt: str) -> dict[str, str]:
    return {"Cookie": f"session={jwt}"}


async def _admin(db: AsyncSession) -> dict[str, str]:
    # The .env.test allowlist contains admin@bial.com → super-admin.
    user = await UserFactory.create(db, email="admin@bial.com")
    return _cookie(mint_session_jwt(user.id, user.token_version, _TTL))


async def _citizen(db: AsyncSession) -> dict[str, str]:
    user = await UserFactory.create(db, email="nobody@rvaiglobal.com")
    return _cookie(mint_session_jwt(user.id, user.token_version, _TTL))


async def _app(db: AsyncSession, **overrides) -> AppRegistry:
    owner = await UserFactory.create(db)
    return await AppRegistryFactory.create(db, user_id=owner.id, **overrides)


def _pending(**extra):
    """A pending app carrying a submitted (typed) submission ref — the approve
    gate's precondition. The BLOB is seeded separately via `_stage_bundle`."""
    return {
        "status": AppStatus.PENDING,
        "source_submission_id": uuid.uuid4(),
        "source_commit_sha": _SHA,
        "submitted_at": datetime.now(UTC),
        **extra,
    }


def _approved(**extra):
    """An approved app whose pin matches its source submission (the post-approve
    steady state)."""
    sid = uuid.uuid4()
    return {
        "status": AppStatus.APPROVED,
        "source_submission_id": sid,
        "source_commit_sha": _SHA,
        "submitted_at": datetime.now(UTC),
        "approved_submission_id": sid,
        "approved_commit_sha": _SHA,
        **extra,
    }


def _stage_bundle(store: FakeStorage, row: AppRegistry) -> None:
    """Seed the submission blob approve's head-check verifies."""
    assert row.source_submission_id is not None
    store.objects[submission_key(row.id, row.source_submission_id)] = b"# v2 git bundle\nfake"


def _wire_storage(app) -> FakeStorage:
    """One in-memory store behind BOTH storage seams this router uses. `approve` / `bundle-url`
    take the None-tolerant `storage_or_none_dependency` (they document a 503); `hard_delete`
    keeps the raising `storage_dependency`. Overriding both to the same instance keeps every
    test in this file blind to which seam its route happens to sit on."""
    store = FakeStorage()
    app.dependency_overrides[storage_dependency] = lambda: store
    app.dependency_overrides[storage_or_none_dependency] = lambda: store
    return store


def _approve_body(row: AppRegistry) -> dict[str, str]:
    return {"submissionId": str(row.source_submission_id)}


# --- super-admin gating ---------------------------------------------------


async def test_citizen_is_forbidden(client, db_session) -> None:
    app = await _app(db_session, **_pending())
    headers = await _citizen(db_session)
    assert (await client.get("/v1/admin/apps", headers=headers)).status_code == 403
    assert (
        await client.post(f"/v1/admin/apps/{app.id}/approve", headers=headers)
    ).status_code == 403
    assert (
        await client.get(f"/v1/admin/apps/{app.id}/bundle-url", headers=headers)
    ).status_code == 403
    # ★ THE THREE DESTRUCTIVE LEVERS, which this list was missing. The delete's body contract
    # changed on this branch and the kill switch widened to draft and rejected apps, so both are
    # exactly the moment to pin who may reach them. The delete is sent WITHOUT a reason on
    # purpose: the gate has to outrank the body, or a citizen learns from a 422 that they were
    # one valid sentence away from destroying somebody else's work.
    assert (
        await client.request("DELETE", f"/v1/admin/apps/{app.id}", headers=headers)
    ).status_code == 403
    assert (
        await client.post(f"/v1/admin/apps/{app.id}/disable", headers=headers)
    ).status_code == 403
    assert (
        await client.post(f"/v1/admin/apps/{app.id}/enable", headers=headers)
    ).status_code == 403
    # LIVENESS: the app is untouched by all six refusals.
    assert await db_session.get(AppRegistry, app.id) is not None


async def test_unauthenticated_is_401(client, db_session) -> None:
    assert (await client.get("/v1/admin/apps")).status_code == 401


async def test_gate_denials_are_detail_shaped_not_envelope(client, db_session) -> None:
    # The gate raises bare HTTPException -> `{"detail"}`, NOT the AppApiError envelope.
    citizen = await _citizen(db_session)
    forbidden = await client.get("/v1/admin/apps", headers=citizen)
    assert forbidden.status_code == 403
    assert forbidden.json() == {"detail": "Super-admin privileges required."}
    unauth = await client.get("/v1/admin/apps")
    assert unauth.status_code == 401
    assert set(unauth.json()) == {"detail"}


def test_admin_routes_document_error_codes_in_openapi() -> None:
    # Each governance route lists its explicit 4xx/5xx plus the inherited
    # dependency 401/403 (DetailBody) and the v1-router 500 default.
    paths = create_app().openapi()["paths"]
    approve = set(paths["/v1/admin/apps/{app_id}/approve"]["post"]["responses"])
    assert {"404", "409", "503", "401", "403", "500"} <= approve
    assert {"401", "403", "500"} <= set(paths["/v1/admin/apps"]["get"]["responses"])
    bundle = set(paths["/v1/admin/apps/{app_id}/bundle-url"]["get"]["responses"])
    assert {"404", "409", "503", "401", "403", "500"} <= bundle


async def test_there_is_no_way_to_mark_an_app_deployed_by_hand(client, db_session) -> None:
    app = await _app(db_session, **_approved())
    headers = await _admin(db_session)

    resp = await client.post(f"/v1/admin/apps/{app.id}/mark-deployed", headers=headers)

    assert resp.status_code == 404
    assert (await client.get("/v1/admin/apps", headers=headers)).status_code == 200
    assert not any(path.endswith("/mark-deployed") for path in create_app().openapi()["paths"])


# --- approve pins exactly the reviewed submission ---------------------


async def test_approve_pins_the_reviewed_submission(client, app, db_session) -> None:
    store = _wire_storage(app)
    row = await _app(db_session, **_pending())
    _stage_bundle(store, row)
    headers = await _admin(db_session)

    resp = await client.post(
        f"/v1/admin/apps/{row.id}/approve", json=_approve_body(row), headers=headers
    )
    assert resp.status_code == 200
    assert resp.json() == {"appId": str(row.id), "status": "approved"}

    fresh = await db_session.get(AppRegistry, row.id)
    await db_session.refresh(fresh)
    assert fresh.status is AppStatus.APPROVED
    # The pin IS the reviewed submission, SHA carried over from submit.
    assert fresh.approved_submission_id == fresh.source_submission_id
    assert fresh.approved_commit_sha == _SHA
    assert fresh.approved_by is not None
    assert fresh.approved_at is not None


async def test_approve_race_resubmitted_since_review_is_409(client, app, db_session) -> None:
    # THE race: admin reviews submission A; the owner re-submits (B) before the
    # admin clicks approve-with-A. A status-only guard cannot see this
    # (PENDING→PENDING is legal); the reviewed-id predicate updates zero rows.
    store = _wire_storage(app)
    reviewed_sid = uuid.uuid4()
    row = await _app(db_session, **_pending())
    # Both blobs exist (submissions are retained), so ONLY the guard can refuse.
    store.objects[submission_key(row.id, reviewed_sid)] = b"# v2 git bundle\nA"
    _stage_bundle(store, row)
    headers = await _admin(db_session)

    resp = await client.post(
        f"/v1/admin/apps/{row.id}/approve",
        json={"submissionId": str(reviewed_sid)},
        headers=headers,
    )
    assert resp.status_code == 409
    assert "re-submitted" in resp.json()["error"]["message"]

    fresh = await db_session.get(AppRegistry, row.id)
    await db_session.refresh(fresh)
    assert fresh.status is AppStatus.PENDING  # nothing promoted
    assert fresh.approved_submission_id is None  # nothing pinned


async def test_approve_missing_artifact_is_409_and_no_pin(client, app, db_session) -> None:
    # The reviewed submission's blob is gone (or never existed) → refuse, so an
    # app can never reach APPROVED with an artifact that 404s at publish time.
    _wire_storage(app)  # empty store — no blob staged
    row = await _app(db_session, **_pending())
    headers = await _admin(db_session)
    resp = await client.post(
        f"/v1/admin/apps/{row.id}/approve", json=_approve_body(row), headers=headers
    )
    assert resp.status_code == 409
    assert "missing" in resp.json()["error"]["message"]
    fresh = await db_session.get(AppRegistry, row.id)
    await db_session.refresh(fresh)
    assert fresh.status is AppStatus.PENDING
    assert fresh.approved_submission_id is None


async def test_approve_storage_head_error_is_503_no_pin_no_audit(client, app, db_session) -> None:
    # Fail-closed: a storage ERROR on the verify-before-pin head-check is ambiguity,
    # NOT absence — 503 (not 409), nothing pinned, and no approve audit row written.
    store = _ExplodingHeadStorage()
    app.dependency_overrides[storage_or_none_dependency] = lambda: store
    row = await _app(db_session, **_pending())
    headers = await _admin(db_session)
    resp = await client.post(
        f"/v1/admin/apps/{row.id}/approve", json=_approve_body(row), headers=headers
    )
    assert resp.status_code == 503
    fresh = await db_session.get(AppRegistry, row.id)
    await db_session.refresh(fresh)
    assert fresh.status is AppStatus.PENDING
    assert fresh.approved_submission_id is None  # nothing pinned
    rows = (
        (
            await db_session.execute(
                sa.select(AuditLog).where(
                    AuditLog.resource_id == str(row.id), AuditLog.action == "approve"
                )
            )
        )
        .scalars()
        .all()
    )
    assert rows == []  # fail-closed before any side effect


async def test_approve_foreign_submission_id_is_409(client, app, db_session) -> None:
    # A submission id belonging to ANOTHER app: the key derivation scopes the blob
    # under THIS app's prefix, so the head misses and approve refuses — one app's
    # review can never pin another app's artifact.
    store = _wire_storage(app)
    other = await _app(db_session, **_pending())
    _stage_bundle(store, other)
    row = await _app(db_session, **_pending())
    _stage_bundle(store, row)
    headers = await _admin(db_session)
    resp = await client.post(
        f"/v1/admin/apps/{row.id}/approve", json=_approve_body(other), headers=headers
    )
    assert resp.status_code == 409


async def test_approve_requires_pending(client, app, db_session) -> None:
    _wire_storage(app)
    row = await _app(db_session, status=AppStatus.DRAFT)
    headers = await _admin(db_session)
    resp = await client.post(
        f"/v1/admin/apps/{row.id}/approve",
        json={"submissionId": str(uuid.uuid4())},
        headers=headers,
    )
    assert resp.status_code == 409


async def test_approve_disabled_directly_is_409(client, app, db_session) -> None:
    # The transition to APPROVED legally permits DISABLED as a source (that is enable's path),
    # and a kill-switched app's source_submission_id is frozen — so WITHOUT the explicit
    # PENDING-only pre-check, approve-with-the-frozen-id would re-stamp the pin and
    # promote a kill-switched app. Approve reaches APPROVED only from PENDING.
    store = _wire_storage(app)
    row = await _app(db_session, **_approved(status=AppStatus.DISABLED))
    _stage_bundle(store, row)
    headers = await _admin(db_session)
    resp = await client.post(
        f"/v1/admin/apps/{row.id}/approve", json=_approve_body(row), headers=headers
    )
    assert resp.status_code == 409
    fresh = await db_session.get(AppRegistry, row.id)
    await db_session.refresh(fresh)
    assert fresh.status is AppStatus.DISABLED  # still kill-switched


# --- state machine -------------------------------------------------------------


async def test_reject_transition(client, db_session) -> None:
    app = await _app(db_session, **_pending())
    headers = await _admin(db_session)
    resp = await client.post(
        f"/v1/admin/apps/{app.id}/reject", json={"note": _NOTE}, headers=headers
    )
    assert resp.json()["status"] == "rejected"
    fresh = await db_session.get(AppRegistry, app.id)
    await db_session.refresh(fresh)
    assert fresh.rejection_note == _NOTE


async def test_reject_leaves_the_approved_pin_intact(client, db_session) -> None:
    # Status governs liveness; the pin governs WHICH artifact. A previously-approved
    # app re-submitted and then rejected keeps its approved pin — the documented
    # "a pending re-submit keeps serving the prior approved artifact" invariant.
    pinned_sid = uuid.uuid4()
    app = await _app(
        db_session,
        **_pending(approved_submission_id=pinned_sid, approved_commit_sha=_SHA),
    )
    headers = await _admin(db_session)
    resp = await client.post(
        f"/v1/admin/apps/{app.id}/reject", json={"note": _NOTE}, headers=headers
    )
    assert resp.json()["status"] == "rejected"
    fresh = await db_session.get(AppRegistry, app.id)
    await db_session.refresh(fresh)
    assert fresh.approved_submission_id == pinned_sid  # pin survives rejection


async def test_disable_then_enable_preserves_the_pin(client, db_session) -> None:
    app = await _app(db_session, **_approved())
    pinned = app.approved_submission_id
    headers = await _admin(db_session)
    dis = await client.post(f"/v1/admin/apps/{app.id}/disable", headers=headers)
    assert dis.json()["status"] == "disabled"
    en = await client.post(f"/v1/admin/apps/{app.id}/enable", headers=headers)
    assert en.json()["status"] == "approved"
    fresh = await db_session.get(AppRegistry, app.id)
    await db_session.refresh(fresh)
    assert fresh.approved_submission_id == pinned  # the pin survives the round trip


@pytest.mark.parametrize("source", [AppStatus.DRAFT, AppStatus.REJECTED])
async def test_disable_switches_off_a_draft_or_rejected_app(
    client, db_session, source: AppStatus
) -> None:
    """The kill switch reaches the two categories most likely to need it.

    DRAFT is the ORDINARY member of the marketplace catalog — one-click deploy never writes
    a status — and REJECTED apps keep serving whatever they last deployed. Before the
    `STATUS_TRANSITIONS[DISABLED]` widening, the only lever that touched either was
    `nuke_app`, which destroys the owner's work; that is the harm this transition removes.
    """
    app = await _app(db_session, status=source)
    headers = await _admin(db_session)
    resp = await client.post(f"/v1/admin/apps/{app.id}/disable", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "disabled"
    fresh = await db_session.get(AppRegistry, app.id)
    await db_session.refresh(fresh)
    assert fresh.status is AppStatus.DISABLED
    # A gated action that moves the state machine writes its audit row, always — the
    # widened source set must not slip a transition past the trail.
    assert "disable" in await _audited_actions(db_session, app.id)


async def test_disable_refuses_a_pending_app_and_names_the_right_lever(client, db_session) -> None:
    """PENDING is the one status deliberately LEFT OUT of the widening.

    An app sitting in the review queue is REJECTED, not switched off: disabling it would let
    an administrator dispose of a submission with the ops lever instead of deciding it, and
    the citizen would never get the rejection note the review flow owes them. The copy names
    the lever they actually wanted rather than refusing bare.
    """
    app = await _app(db_session, **_pending())
    headers = await _admin(db_session)
    resp = await client.post(f"/v1/admin/apps/{app.id}/disable", headers=headers)
    assert resp.status_code == 409
    assert "rejected instead" in resp.json()["error"]["message"]
    fresh = await db_session.get(AppRegistry, app.id)
    await db_session.refresh(fresh)
    assert fresh.status is AppStatus.PENDING  # still in the queue
    # A refused transition writes nothing: the early-return 409 leaves the trail (and the
    # database) exactly as it found them.
    assert "disable" not in await _audited_actions(db_session, app.id)


async def test_an_administrators_kill_switch_survives_the_owners_withdraw(
    client, db_session
) -> None:
    """THE BYPASS THE TRANSITION TABLE WARNS ABOUT, pinned so the obvious repair cannot
    ship silently.

    `withdraw` is citizen-facing and reads `STATUS_TRANSITIONS[DRAFT]` with nothing but an
    ownership predicate in front of it. Add DISABLED to that row — the tempting way to
    un-stick a switched-off draft — and this test goes red: the owner of an app an
    administrator killed walks it straight back to draft from their own workspace.
    """
    owner = await UserFactory.create(db_session, email="owner@rvaiglobal.com")
    app = await AppRegistryFactory.create(db_session, user_id=owner.id, status=AppStatus.DRAFT)
    admin_headers = await _admin(db_session)
    killed = await client.post(f"/v1/admin/apps/{app.id}/disable", headers=admin_headers)
    assert killed.status_code == 200

    # The owner's own signed double-submit headers — the citizen route's real gate, so the
    # 409 below is the state machine refusing and not CSRF refusing for it.
    csrf = issue_csrf_token(owner.id, owner.token_version)
    session = mint_session_jwt(owner.id, owner.token_version, _TTL)
    owner_headers = {"Cookie": f"session={session}; csrf={csrf}", "X-CSRF-Token": csrf}
    resp = await client.post(f"/v1/apps/{app.id}/withdraw", headers=owner_headers)
    assert resp.status_code == 409
    fresh = await db_session.get(AppRegistry, app.id)
    await db_session.refresh(fresh)
    assert fresh.status is AppStatus.DISABLED  # containment held


async def test_enable_guard_rejects_non_disabled(client, db_session) -> None:
    # A pending app must not be promotable to approved via enable (approve-gate bypass).
    app = await _app(db_session, **_pending())
    headers = await _admin(db_session)
    resp = await client.post(f"/v1/admin/apps/{app.id}/enable", headers=headers)
    assert resp.status_code == 409


# --- what the app WAS, remembered across the kill switch ---------------------------------


@pytest.mark.parametrize("source", [AppStatus.DRAFT, AppStatus.REJECTED])
async def test_switching_off_and_back_on_returns_the_app_to_what_it_was(
    client, db_session, source: AppStatus
) -> None:
    """A rejected app comes back REJECTED and a draft comes back DRAFT.

    Enable used to resolve to the literal APPROVED. On an app that was never approved that
    invents an approval nobody gave — and once the artifact-pin guard refuses a row with no
    pin, it instead strands the app in DISABLED with no lever left. `previous_status` is the
    memory that makes the return trip honest.
    """
    app = await _app(db_session, status=source)
    headers = await _admin(db_session)

    off = await client.post(f"/v1/admin/apps/{app.id}/disable", headers=headers)
    assert off.json()["status"] == "disabled"
    killed = await db_session.get(AppRegistry, app.id)
    await db_session.refresh(killed)
    assert killed.previous_status is source  # written from the row, inside the guarded UPDATE

    on = await client.post(f"/v1/admin/apps/{app.id}/enable", headers=headers)
    assert on.status_code == 200
    assert on.json()["status"] == source.value  # the response says where it actually landed
    fresh = await db_session.get(AppRegistry, app.id)
    await db_session.refresh(fresh)
    assert fresh.status is source
    assert fresh.approved_submission_id is None  # no approval was invented on the way back
    # The memory describes a switched-off app; a stale one on a live row is a fact waiting
    # to be misread.
    assert fresh.previous_status is None
    # Both gated actions leave their trail.
    actions = await _audited_actions(db_session, app.id)
    assert "disable" in actions and "enable" in actions


async def test_an_approved_app_still_checks_its_approved_submission_on_the_way_back(
    client, db_session
) -> None:
    """The artifact-pin guard rides on the APPROVED arm only, and it still bites there.

    An approved-status row with no `approved_submission_id` is the approved-with-no-artifact
    state the schema otherwise prevents. Re-enabling one would resurrect it, so the guard
    refuses — and because it is scoped to the approved arm, the draft and rejected restores
    above (which have no pin and are not supposed to) sail past it.
    """
    app = await _app(db_session, status=AppStatus.APPROVED, approved_submission_id=None)
    headers = await _admin(db_session)
    assert (
        await client.post(f"/v1/admin/apps/{app.id}/disable", headers=headers)
    ).status_code == 200

    resp = await client.post(f"/v1/admin/apps/{app.id}/enable", headers=headers)
    assert resp.status_code == 409
    fresh = await db_session.get(AppRegistry, app.id)
    await db_session.refresh(fresh)
    assert fresh.status is AppStatus.DISABLED  # refused, and still contained
    assert fresh.previous_status is AppStatus.APPROVED  # the memory survives a refused enable


async def test_an_app_disabled_before_the_column_existed_re_enables_to_approved(
    client, db_session
) -> None:
    """A NULL `previous_status` is a PRE-COLUMN ROW, not an error.

    Migration 0038 backfilled every already-disabled row to `approved` — the status the code
    it replaced resolved them to — and `enable` reads a NULL the same way as the backstop, for
    a row inserted by hand during an incident or one the backfill could not reach. Simulated
    by nulling the column on a DISABLED row, which is exactly the shape 0038 found.
    """
    sid = uuid.uuid4()
    app = await _app(
        db_session,
        status=AppStatus.DISABLED,
        approved_submission_id=sid,
        approved_commit_sha=_SHA,
        previous_status=None,
    )
    headers = await _admin(db_session)

    resp = await client.post(f"/v1/admin/apps/{app.id}/enable", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"
    fresh = await db_session.get(AppRegistry, app.id)
    await db_session.refresh(fresh)
    assert fresh.status is AppStatus.APPROVED


# --- the queue projection -----------------------------------------------


async def test_the_list_projects_no_key_and_no_signed_url(client, db_session) -> None:
    await _app(db_session, **_pending())
    approved = await _app(db_session, **_approved())
    headers = await _admin(db_session)
    listed = await client.get("/v1/admin/apps", headers=headers)
    ids = [a["appId"] for a in listed.json()["apps"]]
    assert str(approved.id) in ids
    # The projection never leaks the app key or mints a signed URL.
    row = next(a for a in listed.json()["apps"] if a["appId"] == str(approved.id))
    assert "appKey" not in row
    assert "url" not in row and "bundleUrl" not in row
    assert row["hasApprovedSnapshot"] is True


async def test_list_sources_the_display_name_from_the_owning_project(client, db_session) -> None:
    # app_registry has no name column; the admin registry shows the OWNING PROJECT's
    # name (never the old "(untitled)"), for both a pending and a non-pending app.
    owner = await UserFactory.create(db_session)
    pending_project = await ProjectFactory.create(db_session, owner.id, name="Acme Expenses")
    pending = await AppRegistryFactory.create(
        db_session, user_id=owner.id, project_id=pending_project.id, **_pending()
    )
    approved_project = await ProjectFactory.create(db_session, owner.id, name="Gate Roster")
    approved = await AppRegistryFactory.create(
        db_session, user_id=owner.id, project_id=approved_project.id, **_approved()
    )
    headers = await _admin(db_session)

    by_id = {
        a["appId"]: a for a in (await client.get("/v1/admin/apps", headers=headers)).json()["apps"]
    }
    assert by_id[str(pending.id)]["name"] == "Acme Expenses"
    assert by_id[str(approved.id)]["name"] == "Gate Roster"


async def test_pending_row_carries_the_review_payload(client, db_session) -> None:
    app = await _app(db_session, **_pending())
    headers = await _admin(db_session)
    listed = await client.get("/v1/admin/apps", headers=headers)
    row = next(a for a in listed.json()["apps"] if a["appId"] == str(app.id))
    assert row["submissionId"] == str(app.source_submission_id)
    assert row["commitSha"] == _SHA
    assert row["submittedAt"] is not None
    assert row["hasApprovedSnapshot"] is False


async def test_the_admin_row_carries_no_route_or_hand_recorded_deployment(
    client, db_session
) -> None:
    app = await _app(db_session, **_approved())
    headers = await _admin(db_session)

    listed = await client.get("/v1/admin/apps", headers=headers)

    row = next(a for a in listed.json()["apps"] if a["appId"] == str(app.id))
    assert row["approvedSubmissionId"] == str(app.approved_submission_id)
    assert not {"approvalRoute", "deployedAt", "deployedUrl", "redeployNeeded"} & set(row)


# --- the audited bundle download ---------------------------------------------


async def test_bundle_url_mints_and_audits(client, app, db_session) -> None:
    store = _wire_storage(app)
    row = await _app(db_session, **_pending())
    _stage_bundle(store, row)
    headers = await _admin(db_session)

    resp = await client.get(f"/v1/admin/apps/{row.id}/bundle-url", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["submissionId"] == str(row.source_submission_id)
    assert body["commitSha"] == _SHA
    assert body["url"].startswith("https://")
    # Minutes-scale TTL — far under the ABC's 7-day ceiling.
    assert 0 < body["expiresInSeconds"] <= 3600

    audit = (
        await db_session.execute(
            sa.select(AuditLog).where(
                AuditLog.resource_id == str(row.id), AuditLog.action == "bundle:download"
            )
        )
    ).scalar_one()
    # The detail identifies the artifact — and NEVER carries the bearer URL.
    assert audit.detail == {"submissionId": str(row.source_submission_id), "commitSha": _SHA}
    assert body["url"] not in str(audit.detail)


async def test_bundle_url_storage_error_is_503_and_unaudited(client, app, db_session) -> None:
    # Fail-closed twin of the 409 case: a storage ERROR while minting the signed URL is
    # 503 (ambiguity denies), and no bearer credential or audit row is produced.
    store = _ExplodingSignedUrlStorage()
    app.dependency_overrides[storage_or_none_dependency] = lambda: store
    row = await _app(db_session, **_pending())  # has a submission to sign
    headers = await _admin(db_session)
    resp = await client.get(f"/v1/admin/apps/{row.id}/bundle-url", headers=headers)
    assert resp.status_code == 503
    rows = (
        (
            await db_session.execute(
                sa.select(AuditLog).where(
                    AuditLog.resource_id == str(row.id), AuditLog.action == "bundle:download"
                )
            )
        )
        .scalars()
        .all()
    )
    assert rows == []  # no audit row for a pull that never produced a URL


async def test_bundle_url_without_submission_is_409_and_unaudited(client, app, db_session) -> None:
    _wire_storage(app)
    row = await _app(db_session)  # draft, never submitted
    headers = await _admin(db_session)
    resp = await client.get(f"/v1/admin/apps/{row.id}/bundle-url", headers=headers)
    assert resp.status_code == 409
    # No audit row for a non-event.
    rows = (
        (
            await db_session.execute(
                sa.select(AuditLog).where(
                    AuditLog.resource_id == str(row.id), AuditLog.action == "bundle:download"
                )
            )
        )
        .scalars()
        .all()
    )
    assert rows == []


# --- approval --------------------------------------------------------

# The shape the submit service attaches: both answer sets, the differences, and the
# redacted explanation. The projection must carry it VERBATIM — the review screen leads
# with the disagreement, and a lossy pass-through here would blank it.
_DECLARATION = {
    "citizen": {"personal_information": "no"},
    "review": {"personal_information": "yes"},
    "differences": ["personal_information"],
    "explanation": "It only stores visitor gate numbers.",
}


async def test_an_approved_app_projects_its_declaration_verbatim(client, app, db_session) -> None:
    store = _wire_storage(app)
    row = await _app(db_session, **_pending(declaration=_DECLARATION))
    _stage_bundle(store, row)
    headers = await _admin(db_session)

    resp = await client.post(
        f"/v1/admin/apps/{row.id}/approve", json=_approve_body(row), headers=headers
    )
    assert resp.status_code == 200

    listed = await client.get("/v1/admin/apps", headers=headers)
    projected = next(a for a in listed.json()["apps"] if a["appId"] == str(row.id))
    assert projected["declaration"] == _DECLARATION


async def test_a_superadmin_approving_their_own_app_is_recorded_distinguishably(
    client, app, db_session
) -> None:
    """Recorded, not forbidden. RBAC has two computed roles and no concept of a
    second approver, and the platform already books the missing separation of duties as an
    accepted risk; forbidding it would leave a superadmin unable to publish their own
    work at all. So the answer is a trail an actor-keyed query can read: the action word
    itself differs (`approve:self`), which makes "list every self-approval" one
    predicate rather than a join against app ownership."""
    store = _wire_storage(app)
    admin_user = await UserFactory.create(db_session, email="superadmin@bial.com")
    row = await AppRegistryFactory.create(db_session, user_id=admin_user.id, **_pending())
    _stage_bundle(store, row)
    headers = _cookie(mint_session_jwt(admin_user.id, admin_user.token_version, _TTL))

    resp = await client.post(
        f"/v1/admin/apps/{row.id}/approve", json=_approve_body(row), headers=headers
    )

    assert resp.status_code == 200  # allowed — the risk is accepted, not litigated here
    assert await _audited_actions(db_session, row.id) == ["approve:self"]


async def test_approving_someone_elses_app_stays_the_plain_action(client, app, db_session) -> None:
    """The other half of that distinction: the ordinary case must NOT drift into the self bucket,
    or the distinction it exists to make is worthless."""
    store = _wire_storage(app)
    row = await _app(db_session, **_pending())  # owned by a citizen, not the admin
    _stage_bundle(store, row)
    headers = await _admin(db_session)

    resp = await client.post(
        f"/v1/admin/apps/{row.id}/approve", json=_approve_body(row), headers=headers
    )

    assert resp.status_code == 200
    assert await _audited_actions(db_session, row.id) == ["approve"]


# --- audit -------------------------------------------------------------


async def test_governance_actions_are_audited_with_artifact_detail(
    client, app, db_session
) -> None:
    store = _wire_storage(app)
    row = await _app(db_session, **_pending())
    _stage_bundle(store, row)
    headers = await _admin(db_session)
    await client.post(f"/v1/admin/apps/{row.id}/approve", json=_approve_body(row), headers=headers)
    detail = (
        await db_session.execute(
            sa.select(AuditLog.detail).where(
                AuditLog.resource_id == str(row.id), AuditLog.action == "approve"
            )
        )
    ).scalar_one()
    assert detail["submissionId"] == str(row.source_submission_id)
    assert detail["commitSha"] == _SHA


async def _audited_actions(db_session, app_id) -> list[str]:
    rows = await db_session.execute(
        sa.select(AuditLog.action).where(AuditLog.resource_id == str(app_id))
    )
    return list(rows.scalars().all())


async def test_patch_login_required_is_audited(client, db_session) -> None:
    # Audit every gated action. The login-required gate is the only admin-patchable
    # field now that the app display name is sourced from the owning project.
    app = await _app(db_session, **_pending())
    headers = await _admin(db_session)
    await client.patch(f"/v1/admin/apps/{app.id}", json={"loginRequired": True}, headers=headers)
    assert "config:loginRequired" in await _audited_actions(db_session, app.id)


async def test_patch_ignores_a_name_key_and_does_not_audit_it(client, db_session) -> None:
    # The app name is project-sourced; `PatchAppRequest` no longer carries `name`, and
    # `CamelModel` ignores unknown keys — so a stray `{"name": ...}` is silently dropped (200,
    # not 422) and writes no `config:name` audit row. The response name stays the project's.
    app = await _app(db_session, **_pending())
    headers = await _admin(db_session)
    resp = await client.patch(
        f"/v1/admin/apps/{app.id}", json={"name": "Ignored"}, headers=headers
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "Test Project"  # project-sourced, not the ignored key
    assert "config:name" not in await _audited_actions(db_session, app.id)


async def test_reject_over_long_note_is_422_not_silently_truncated(client, db_session) -> None:
    app = await _app(db_session, **_pending())
    headers = await _admin(db_session)
    resp = await client.post(
        f"/v1/admin/apps/{app.id}/reject", json={"note": "x" * 1001}, headers=headers
    )
    assert resp.status_code == 422  # was a silent chop to 1000 chars the admin never saw
    fresh = await db_session.get(AppRegistry, app.id)
    await db_session.refresh(fresh)
    assert fresh.status is AppStatus.PENDING  # not rejected
    assert fresh.rejection_note is None


# --- hard-delete -----------------------------------------------------------------


async def test_hard_delete_purges_everything(client, db_session, app) -> None:
    store = _wire_storage(app)
    row = await _app(db_session, **_pending())
    # The snapshot bundle is the app's object-store artifact nuke_app must sweep (the per-app
    # file model and the shared data plane were both retired, so nothing else is left to sweep).
    store.objects[snapshot_key(row.id)] = b"bundle-bytes"
    await db_session.flush()
    headers = await _admin(db_session)

    resp = await client.request(
        "DELETE",
        f"/v1/admin/apps/{row.id}",
        headers=headers,
        json={
            "reason": "Duplicate app created in error during onboarding, owner asked for removal"
        },
    )
    assert resp.json() == {"ok": True}
    assert await db_session.get(AppRegistry, row.id) is None
    assert store.objects == {}
    # ★ THE ROW, AND WHAT IT SAYS. Asserting only that an `app:delete` row exists left the
    # `detail=` kwarg deletable with the suite still green — and that kwarg IS the
    # administrator's justification, on the one row that outlives what it destroyed. Read by
    # APP ID after the app row is gone, which is the property `audit_logs` is chosen for (no
    # foreign key, `resource_id` a plain string).
    audited = (
        await db_session.execute(
            sa.select(AuditLog).where(
                AuditLog.resource_id == str(row.id), AuditLog.action == "app:delete"
            )
        )
    ).scalar_one()
    assert audited.detail == {
        "reason": "Duplicate app created in error during onboarding, owner asked for removal",
        "projectId": str(row.project_id),
    }
    # THE PROJECT SURVIVES AN APP HARD-DELETE, by design — which is what makes `projectId` the
    # only handle left connecting this row to something still readable.
    assert await db_session.get(Project, row.project_id) is not None


async def test_hard_delete_without_a_reason_is_refused(client, db_session, app) -> None:
    """★ THE SERVER IS WHAT ENFORCES THE RULE. The dialog that collects the reason is a
    courtesy to the person typing it; a client that forgets the body — which is exactly what
    every client did until this branch — must not be able to destroy somebody's app anyway."""
    _wire_storage(app)
    row = await _app(db_session, **_pending())
    await db_session.flush()
    headers = await _admin(db_session)

    bodiless = await client.request("DELETE", f"/v1/admin/apps/{row.id}", headers=headers)
    too_short = await client.request(
        "DELETE", f"/v1/admin/apps/{row.id}", headers=headers, json={"reason": "because"}
    )

    assert bodiless.status_code == 422
    assert too_short.status_code == 422
    # LIVENESS: nothing was destroyed by either refusal.
    assert await db_session.get(AppRegistry, row.id) is not None


async def test_hard_delete_shares_the_project_deletes_word_bound(client, db_session, app) -> None:
    """The harsher act — destroying somebody else's app — takes the same reason bound as the
    citizen's own project delete (`MIN_DELETE_REMARK_WORDS`), not a looser one."""
    _wire_storage(app)
    row = await _app(db_session, **_pending())
    await db_session.flush()
    headers = await _admin(db_session)

    reason = " ".join(f"w{i}" for i in range(MIN_DELETE_REMARK_WORDS))
    resp = await client.request(
        "DELETE", f"/v1/admin/apps/{row.id}", headers=headers, json={"reason": reason}
    )

    assert resp.status_code == 200, resp.text
    assert await db_session.get(AppRegistry, row.id) is None


async def test_hard_delete_sweeps_every_retained_submission(client, db_session, app) -> None:
    # Submissions are retained forever — until the app is hard-deleted, at which
    # point the whole submissions/{app_id}/ prefix goes with it. Another app's
    # submissions are untouched (the prefix is app-scoped).
    store = _wire_storage(app)
    row = await _app(db_session, **_pending())
    bystander = await _app(db_session, **_pending())
    _stage_bundle(store, bystander)
    assert bystander.source_submission_id is not None
    bystander_key = submission_key(bystander.id, bystander.source_submission_id)
    store.objects[snapshot_key(row.id)] = b"snapshot"
    for sid in (uuid.uuid4(), uuid.uuid4(), uuid.uuid4()):
        store.objects[submission_key(row.id, sid)] = b"# v2 git bundle\nretained"
    await db_session.flush()
    headers = await _admin(db_session)

    resp = await client.request(
        "DELETE",
        f"/v1/admin/apps/{row.id}",
        headers=headers,
        json={
            "reason": "Duplicate app created in error during onboarding, owner asked for removal"
        },
    )
    assert resp.json() == {"ok": True}
    # Snapshot + all three submissions swept; the bystander's submission survives.
    assert set(store.objects) == {bystander_key}


async def test_nuke_app_sweeps_the_per_app_container(db_session) -> None:
    # nuke_app now receives the container store by injection (not a global singleton), so it
    # sweeps the app's per-app Blob container alongside the snapshot bundle. Drives the service
    # directly with a recording store to prove the container sweep fires.
    row = await _app(db_session, **_pending())
    await db_session.flush()
    store = FakeStorage()
    store.objects[snapshot_key(row.id)] = b"bundle-bytes"
    containers = _RecordingContainerStore()

    await nuke_app(db_session, store, row.id, containers)
    await db_session.flush()

    assert containers.deleted == [row.id]  # the per-app container was swept
    assert store.objects == {}  # the snapshot blob was swept
    assert await db_session.get(AppRegistry, row.id) is None  # registry row dropped


async def test_nuke_app_sweeps_the_container_registry_repository(db_session, monkeypatch) -> None:
    """★ THE IMAGE GOES WITH THE APP, and until now nothing said so.

    `sweep_app_repositories` was wired into `nuke_app` and never asserted anywhere: delete the
    call and every suite stayed green while the admin lever — the one whose dialog says
    "destroyed permanently" — left the app's compiled tree sitting in the container registry,
    which is exactly what the citizen's own softer delete removes."""
    row = await _app(db_session, **_pending())
    db_session.add(Deployment(app_id=row.id, user_id=row.user_id))
    await db_session.flush()
    swept: list[list[uuid.UUID]] = []

    async def _recording(app_ids, *, config, transport=None) -> list[str]:
        swept.append(list(app_ids))
        return []

    monkeypatch.setattr("src.services.appserving.governance.sweep_app_repositories", _recording)

    await nuke_app(db_session, FakeStorage(), row.id, None)

    assert swept == [[row.id]]
    assert await db_session.get(AppRegistry, row.id) is None  # and the row still went


async def test_nuke_app_does_not_ask_the_registry_about_an_app_never_built(
    db_session, monkeypatch
) -> None:
    """An image reaches the registry only through a deploy — `names.image_tag` composes the push
    tag from the DEPLOYMENT id — so an app with no deployment row has no repository to delete.

    Asking anyway is not free: a registry that refuses the delete credential answers 401/403 for
    whatever it is handed, and every id in the sweep comes back a survivor. That would put a
    repository that never existed into the teardown record of every delete and send an operator
    after it, which the sibling sweeps' own docstrings call worse than naming nothing."""
    row = await _app(db_session, **_pending())  # no Deployment row
    await db_session.flush()
    swept: list[list[uuid.UUID]] = []

    async def _recording(app_ids, *, config, transport=None) -> list[str]:
        swept.append(list(app_ids))
        return []

    monkeypatch.setattr("src.services.appserving.governance.sweep_app_repositories", _recording)

    await nuke_app(db_session, FakeStorage(), row.id, None)

    # The sweep is still CALLED (one code path, no branch to drift) — with nothing in it.
    assert swept == [[]]
    assert await db_session.get(AppRegistry, row.id) is None


async def test_nuke_app_names_every_artefact_that_outlived_it(db_session, monkeypatch) -> None:
    """★ SURVIVORS ARE THE RETURN VALUE, not a log line the caller cannot read.

    All four sweeps already answer with what they could not destroy and `nuke_app` used to
    throw all four answers away, which made the admin hard-delete the one destructive lever on
    the platform that kept no record of a leak. Each is driven to its failing answer here so
    the tagging is proved per artefact rather than in aggregate."""
    row = await _app(db_session, **_pending())
    await db_session.flush()

    async def _blobs(storage, keys) -> list[str]:
        return ["snapshots/left-behind"]

    async def _containers(store, app_ids) -> list[uuid.UUID]:
        return list(app_ids)

    async def _published(app_ids, *, client=None) -> list[uuid.UUID]:
        return list(app_ids)

    async def _repos(app_ids, *, config, transport=None) -> list[str]:
        return ["app-still-in-the-registry"]

    for name, double in (
        ("sweep_blobs", _blobs),
        ("sweep_app_containers", _containers),
        ("sweep_published_apps", _published),
        ("sweep_app_repositories", _repos),
    ):
        monkeypatch.setattr(f"src.services.appserving.governance.{name}", double)

    survivors = await nuke_app(db_session, FakeStorage(), row.id, None)

    assert survivors == [
        ("blob", "snapshots/left-behind"),
        ("app_container", str(row.id)),
        ("published_app", str(row.id)),
        ("registry_repository", "app-still-in-the-registry"),
    ]


async def test_hard_delete_records_a_database_that_outlived_it(
    client, db_session, app, monkeypatch
) -> None:
    """★ A SURVIVING DATABASE IS ON THE RECORD, on the harsher lever too.

    `salt_the_earth` answers whether the earth is actually salted, and this route discarded
    that answer — so an administrator could destroy somebody else's app, the drop could fail,
    and a copy of the citizen's data would stay on the cluster with nothing written down.
    Nothing automatic collects it either: `appdb/reconcile.py` is operator-invoked and, by its
    own docstring, report-only. The route still answers `{"ok": true}` — the delete DID happen,
    and the citizen has no notification path to be told otherwise."""
    _wire_storage(app)
    owner = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, owner.id)
    row = await AppRegistryFactory.create(
        db_session, user_id=owner.id, project_id=project.id, **_pending()
    )
    db_session.add(
        ProjectDatabase(
            project_id=project.id,
            db_name="bialdb_stubborn",
            role_name="bialrole_stubborn",
            password_encrypted="not-a-real-token",
        )
    )
    await db_session.flush()
    headers = await _admin(db_session)

    async def _refuses_to_salt(*, db_name: str, role_name: str) -> bool:
        return False

    monkeypatch.setattr("src.api.v1.admin.router.salt_the_earth", _refuses_to_salt)

    resp = await client.request(
        "DELETE",
        f"/v1/admin/apps/{row.id}",
        headers=headers,
        json={"reason": "Owner left the organisation and asked for the app to be destroyed"},
    )

    assert resp.json() == {"ok": True}  # the delete happened; the leak is not the citizen's news
    recorded = (
        await db_session.execute(
            sa.select(AuditLog.detail).where(
                AuditLog.action == "project:teardown-incomplete",
                AuditLog.resource_id == str(project.id),
            )
        )
    ).scalar_one()
    assert recorded == {
        "count": 1,
        "survived": [{"artefact": "app_database", "id": "bialdb_stubborn"}],
        # The row's `resource_id` is the PROJECT, so the app id rides in the detail: the only
        # handle from this record back to the app it was about.
        "appId": str(row.id),
    }


async def test_hard_delete_writes_no_teardown_row_when_nothing_survived(
    client, db_session, app
) -> None:
    """The row exists to be read, so a clean delete must not file one. Paired with the test
    above so `toBeNull`-shaped absence is never the only thing asserted: that one proves the
    row appears, this one proves it is not filed unconditionally."""
    _wire_storage(app)
    row = await _app(db_session, **_pending())
    await db_session.flush()
    headers = await _admin(db_session)

    resp = await client.request(
        "DELETE",
        f"/v1/admin/apps/{row.id}",
        headers=headers,
        json={"reason": "Duplicate app created in error during onboarding, owner asked for it"},
    )

    assert resp.json() == {"ok": True}
    filed = (
        await db_session.execute(
            sa.select(sa.func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == "project:teardown-incomplete")
        )
    ).scalar_one()
    assert filed == 0


# --- The storage-off contract (FIX 9) ------------------------------------------


@pytest.mark.parametrize("route", ["approve", "bundle-url"])
async def test_storage_route_is_503_not_500_when_storage_is_unconfigured(
    client, db_session, route: str
) -> None:
    """Fixture-free store-off baseline for the two governance routes that ADVERTISE a 503: with
    no store wired at all, `storage_or_none_dependency` resolves `get_storage()` ->
    StorageUnconfiguredError -> None, and each body maps None onto the documented 503. Binding
    no fixture is the load-bearing half — see `fake_storage` in `tests/conftest.py`."""
    from src.services.storage import accessor as _storage_accessor

    _storage_accessor._backend_singleton = None  # store off: no backend configured in .env.test
    row = await _app(db_session, **_pending())
    headers = await _admin(db_session)

    if route == "approve":
        resp = await client.post(
            f"/v1/admin/apps/{row.id}/approve", json=_approve_body(row), headers=headers
        )
    else:
        resp = await client.get(f"/v1/admin/apps/{row.id}/bundle-url", headers=headers)

    assert resp.status_code == 503
    body = resp.json()
    assert body["error"]["message"] == "Storage is temporarily unavailable. Please try again."
    assert "detail" not in body  # pin the ENVELOPE, not just the status
    # Fail-closed twin of the exploding-store cases: nothing pinned, nothing audited.
    fresh = await db_session.get(AppRegistry, row.id)
    await db_session.refresh(fresh)
    assert fresh.status is AppStatus.PENDING
    assert fresh.approved_submission_id is None
    audited = (
        (await db_session.execute(sa.select(AuditLog).where(AuditLog.resource_id == str(row.id))))
        .scalars()
        .all()
    )
    assert audited == []
