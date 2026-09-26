"""Journey: admin governance — the API a super-admin (email allowlist: admin@bial.com) drives
through the review desk, matching what the portal's `AppRegistryPanel` and feedback panel render.

Walks the lifecycle state machine (approve/reject/disable/enable), proving every gated action
writes an audit row and that a citizen/anon caller is refused; reads the cross-user feedback
stream back through the admin API, checking each carries the fields the SPA renders —
`ownerUsername` on an apps-list row."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import storage_dependency, storage_or_none_dependency
from src.config import settings
from src.db.models.app_registry import AppRegistry, AppStatus
from src.db.models.audit import AuditLog
from src.db.models.feedback import Feedback
from src.db.models.user import User
from src.services.auth.session_jwt import mint_session_jwt
from src.services.storage import submission_key
from tests.factories import AppRegistryFactory, UserFactory
from tests.fakes import FakeStorage

_TTL = settings.auth.access_ttl_seconds
_SHA = "9d" * 20  # 40 lowercase hex chars — what the bundle parser guarantees


def _cookie(jwt: str) -> dict[str, str]:
    return {"Cookie": f"session={jwt}"}


async def _admin(db: AsyncSession) -> tuple[User, dict[str, str]]:
    """The .env.test allowlist has admin@bial.com → super-admin (an email, not a role column)."""
    user = await UserFactory.create(db, email="admin@bial.com")
    return user, _cookie(mint_session_jwt(user.id, user.token_version, _TTL))


async def _citizen(db: AsyncSession) -> dict[str, str]:
    user = await UserFactory.create(db, email="nobody@rvaiglobal.com")
    return _cookie(mint_session_jwt(user.id, user.token_version, _TTL))


async def _owned_app(db: AsyncSession, owner: User, **overrides: object) -> AppRegistry:
    return await AppRegistryFactory.create(db, user_id=owner.id, **overrides)


def _pending_seed(**extra: object) -> dict[str, object]:
    """A pending app carrying a submitted (typed) submission ref — the approve gate's
    precondition. The submission BLOB is staged separately via `_stage_bundle`."""
    return {
        "status": AppStatus.PENDING,
        "source_submission_id": uuid.uuid4(),
        "source_commit_sha": _SHA,
        "submitted_at": datetime.now(UTC),
        **extra,
    }


def _stage_bundle(store: FakeStorage, row: AppRegistry) -> None:
    """Seed the immutable submission blob approve's head-check verifies."""
    assert row.source_submission_id is not None
    store.objects[submission_key(row.id, row.source_submission_id)] = b"# v2 git bundle\ngov"


async def _audited_action(db: AsyncSession, app_id: object, action: str) -> AuditLog:
    """The one audit row for (app, action) — `.scalar_one()` fails loudly if none was written."""
    return (
        await db.execute(
            sa.select(AuditLog).where(
                AuditLog.resource_id == str(app_id), AuditLog.action == action
            )
        )
    ).scalar_one()


# --- the lifecycle walk, every gated action audited ------------------------------------------


async def test_admin_governance_walk_is_audited(client, app, db_session) -> None:
    """approve -> reject -> disable -> enable; each transition writes its audit row.

    The accountability contract: a permission-gated action MUST leave a durable audit row
    naming the actor."""
    store = FakeStorage()
    app.dependency_overrides[storage_dependency] = lambda: store
    # Both storage seams to ONE store: routes that document a 503 take the None-tolerant
    # `storage_or_none_dependency`, `hard_delete` keeps the raising one. Binding both keeps
    # this journey blind to which seam each route it walks happens to sit on.
    app.dependency_overrides[storage_or_none_dependency] = lambda: store
    admin, admin_headers = await _admin(db_session)
    owner = await UserFactory.create(db_session, email="app-owner@rvaiglobal.com")

    # RBAC gate first: the review desk is super-admin only.
    citizen_headers = await _citizen(db_session)
    assert (await client.get("/v1/admin/apps", headers=citizen_headers)).status_code == 403
    assert (await client.get("/v1/admin/apps")).status_code == 401

    to_approve = await _owned_app(db_session, owner, **_pending_seed())
    _stage_bundle(store, to_approve)
    to_reject = await _owned_app(db_session, owner, **_pending_seed())
    approved_sid = uuid.uuid4()
    to_toggle = await _owned_app(
        db_session,
        owner,
        status=AppStatus.APPROVED,
        approved_submission_id=approved_sid,
        approved_commit_sha=_SHA,
    )

    # approve pins EXACTLY the reviewed submission.
    approved = await client.post(
        f"/v1/admin/apps/{to_approve.id}/approve",
        json={"submissionId": str(to_approve.source_submission_id)},
        headers=admin_headers,
    )
    assert approved.status_code == 200
    assert approved.json() == {"appId": str(to_approve.id), "status": "approved"}
    assert (await _audited_action(db_session, to_approve.id, "approve")).actor_id == admin.id

    # the note must clear a 20-character floor — a rejection is the only thing that travels back.
    rejected = await client.post(
        f"/v1/admin/apps/{to_reject.id}/reject",
        json={"note": "Not yet — this needs a named data owner first."},
        headers=admin_headers,
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    assert (await _audited_action(db_session, to_reject.id, "reject")).actor_id == admin.id

    disabled = await client.post(f"/v1/admin/apps/{to_toggle.id}/disable", headers=admin_headers)
    assert disabled.status_code == 200
    assert disabled.json()["status"] == "disabled"
    assert (await _audited_action(db_session, to_toggle.id, "disable")).actor_id == admin.id

    enabled = await client.post(f"/v1/admin/apps/{to_toggle.id}/enable", headers=admin_headers)
    assert enabled.status_code == 200
    assert enabled.json()["status"] == "approved"
    assert (await _audited_action(db_session, to_toggle.id, "enable")).actor_id == admin.id


# --- feedback stream, newest-first, with author email -----------------------------------------


async def test_admin_feedback_is_newest_first_with_email(client, db_session) -> None:
    _, admin_headers = await _admin(db_session)
    author = await UserFactory.create(db_session, email="feedbacker@rvaiglobal.com")
    now = datetime.now(UTC)
    # Two rows with explicit, distinct timestamps so the ordering is deterministic.
    db_session.add(
        Feedback(
            user_id=author.id,
            message="older note",
            page="/chat",
            created_at=now - timedelta(hours=1),
        )
    )
    db_session.add(
        Feedback(user_id=author.id, message="newer note", page="/admin", created_at=now)
    )
    await db_session.flush()

    resp = await client.get("/v1/admin/feedback", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    messages = [f["message"] for f in body["feedback"]]
    assert messages.index("newer note") < messages.index("older note")
    # every row carries the resolved author email, not just an id, since the SPA renders it
    assert all(f["email"] == "feedbacker@rvaiglobal.com" for f in body["feedback"])

    assert (
        await client.get("/v1/admin/feedback", headers=await _citizen(db_session))
    ).status_code == 403


# --- admin apps list exposes an owner username the SPA can render -----------------------------


async def test_admin_apps_list_exposes_owner_username_for_spa(client, db_session) -> None:
    """`AppRegistryPanel` renders the Owner cell from `app.ownerUsername` (`:296`, `:54`)."""
    _, admin_headers = await _admin(db_session)
    owner = await UserFactory.create(db_session, email="owner-cell@rvaiglobal.com")
    app = await _owned_app(db_session, owner, **_pending_seed())

    listed = await client.get("/v1/admin/apps", headers=admin_headers)
    assert listed.status_code == 200
    row = next(a for a in listed.json()["apps"] if a["appId"] == str(app.id))

    assert row.get("ownerUsername") == owner.email
