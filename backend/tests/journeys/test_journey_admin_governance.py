"""Journey: admin governance (the API + the SPA fields the admin console renders).

A super-admin (email allowlist: admin@bial.com) drives the whole review desk the way the
portal `AppRegistryPanel` / `AuditDrawer` / feedback panel do:

  * walk the lifecycle state machine — approve one app, reject another, disable+re-enable a
    third — and prove **every gated action wrote an audit row** (accountability, ADR-0005);
  * read the per-app audit trail back through the admin API, the way `AuditDrawer` does;
  * read the cross-user feedback stream, newest-first, each row carrying the author email.

Three concerns, one file (mirrors `test_journey_build_deploy_render.py`'s green+red split):

  * `test_admin_governance_walk_is_audited` — the GREEN spine. approve/reject/disable/enable
    all transition and each writes its audit row; RBAC gates a citizen (403) / anon (401).
    PASSES today.
  * `test_admin_feedback_is_newest_first_with_email` — GREEN. `GET /v1/admin/feedback` returns
    the stream newest-first with each item's author email. PASSES today.
  * `test_admin_apps_list_exposes_owner_username_for_spa` — RED. `AppRegistryPanel` renders the
    Owner cell from `app.ownerUsername`, but `AdminAppOut` projects only `ownerId` (a raw uuid),
    so the cell is always the `—` fallback. CAPTURES BUG (Journey 2.1).
  * `test_admin_audit_events_carry_spa_fields` — RED. `AuditDrawer` keys each row on `ev._id`,
    times it via `ev.at`, names the actor via `ev.username`, and reads `ev.count` top-level, but
    `AuditEventOut` emits `id`/`createdAt`/`actorId` and buries `count` under `detail`. Every row
    renders with an undefined key, a `—` time, and `anonymous`. CAPTURES BUG (Journey 3.1–3.4).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.db.models.app_registry import AppRegistry, AppStatus
from src.db.models.audit import AuditLog
from src.db.models.feedback import Feedback
from src.db.models.user import User
from src.services.auth.session_jwt import mint_session_jwt
from tests.factories import AppRegistryFactory, UserFactory

_TTL = settings.auth.access_ttl_seconds
_COMPILED = "var PreviewApp=()=>React.createElement('div',null,'gov');"


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
    """A pending app carrying a submitted client artifact — the approve gate's precondition."""
    return {
        "status": AppStatus.PENDING,
        "source_snapshot": {"compiled": _COMPILED, "src": "x", "entry": "PreviewApp"},
        **extra,
    }


async def _audited_action(db: AsyncSession, app_id: object, action: str) -> AuditLog:
    """The one audit row for (app, action) — `.scalar_one()` fails loudly if none was written."""
    return (
        await db.execute(
            sa.select(AuditLog).where(
                AuditLog.resource_id == str(app_id), AuditLog.action == action
            )
        )
    ).scalar_one()


# --- GREEN: the lifecycle walk, every gated action audited ----------------------------------


async def test_admin_governance_walk_is_audited(client, db_session) -> None:
    """approve -> reject -> disable -> enable; each transition writes its audit row.

    This is the accountability contract (ADR-0005): a permission-gated action MUST leave a
    durable audit row naming the actor. The admin audit API also reads those rows back (the
    data the `AuditDrawer` renders — even though it renders them under the wrong keys, see the
    RED test below).
    """
    admin, admin_headers = await _admin(db_session)
    owner = await UserFactory.create(db_session, email="app-owner@rvaiglobal.com")

    # RBAC gate first: the review desk is super-admin only.
    citizen_headers = await _citizen(db_session)
    assert (await client.get("/v1/admin/apps", headers=citizen_headers)).status_code == 403
    assert (await client.get("/v1/admin/apps")).status_code == 401

    # Seed the three apps the walk needs, each in the right starting state.
    to_approve = await _owned_app(db_session, owner, **_pending_seed())
    to_reject = await _owned_app(db_session, owner, **_pending_seed())
    to_toggle = await _owned_app(
        db_session,
        owner,
        status=AppStatus.APPROVED,
        approved_snapshot={"compiled": _COMPILED, "src": "x", "entry": "PreviewApp"},
    )

    # 1. approve (pending -> approved) — copies the client artifact, no server compile.
    approved = await client.post(f"/v1/admin/apps/{to_approve.id}/approve", headers=admin_headers)
    assert approved.status_code == 200
    assert approved.json() == {"appId": str(to_approve.id), "status": "approved"}
    assert (await _audited_action(db_session, to_approve.id, "approve")).actor_id == admin.id

    # 2. reject (pending -> rejected) — stores the note.
    rejected = await client.post(
        f"/v1/admin/apps/{to_reject.id}/reject", json={"note": "not yet"}, headers=admin_headers
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    assert (await _audited_action(db_session, to_reject.id, "reject")).actor_id == admin.id

    # 3. disable (approved -> disabled).
    disabled = await client.post(f"/v1/admin/apps/{to_toggle.id}/disable", headers=admin_headers)
    assert disabled.status_code == 200
    assert disabled.json()["status"] == "disabled"
    assert (await _audited_action(db_session, to_toggle.id, "disable")).actor_id == admin.id

    # 4. enable (disabled -> approved).
    enabled = await client.post(f"/v1/admin/apps/{to_toggle.id}/enable", headers=admin_headers)
    assert enabled.status_code == 200
    assert enabled.json()["status"] == "approved"
    assert (await _audited_action(db_session, to_toggle.id, "enable")).actor_id == admin.id

    # The admin audit API surfaces the approve row (the read path the AuditDrawer drives).
    events = await client.get(f"/v1/admin/apps/{to_approve.id}/audit", headers=admin_headers)
    assert events.status_code == 200
    assert "approve" in [e["action"] for e in events.json()["events"]]


# --- GREEN: feedback stream, newest-first, with author email --------------------------------


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
    # Newest-first (order_by created_at desc).
    assert messages.index("newer note") < messages.index("older note")
    # Every row carries the resolved author email the SPA renders.
    assert all(f["email"] == "feedbacker@rvaiglobal.com" for f in body["feedback"])

    # RBAC gate: a citizen cannot read the cross-user feedback stream.
    assert (
        await client.get("/v1/admin/feedback", headers=await _citizen(db_session))
    ).status_code == 403


# --- RED: admin apps list must expose an owner username the SPA can render -------------------


async def test_admin_apps_list_exposes_owner_username_for_spa(client, db_session) -> None:
    """`AppRegistryPanel` renders the Owner cell from `app.ownerUsername` (`:296`, `:54`)."""
    _, admin_headers = await _admin(db_session)
    owner = await UserFactory.create(db_session, email="owner-cell@rvaiglobal.com")
    app = await _owned_app(db_session, owner, **_pending_seed())

    listed = await client.get("/v1/admin/apps?status=pending", headers=admin_headers)
    assert listed.status_code == 200
    row = next(a for a in listed.json()["apps"] if a["appId"] == str(app.id))

    # CAPTURES BUG: admin apps expose no owner username — `AdminAppOut` projects `ownerId`
    # (a raw uuid) only, no `ownerUsername`/`ownerEmail`, so the Owner cell is always `—`.
    # This assertion is RED today and turns GREEN once the projection resolves the owner's
    # email/display name into a human `ownerUsername`.
    assert row.get("ownerUsername") == owner.email


# --- RED: audit events must carry the keys the AuditDrawer reads ----------------------------


async def test_admin_audit_events_carry_spa_fields(client, db_session) -> None:
    """`AuditDrawer` reads `ev._id` (key), `ev.at` (time), `ev.username` (actor), `ev.count`."""
    admin, admin_headers = await _admin(db_session)
    owner = await UserFactory.create(db_session, email="audit-owner@rvaiglobal.com")
    app = await _owned_app(db_session, owner, login_required=False, **_pending_seed())

    # Two audited actions so the drawer has rows to render, including a count-bearing one.
    await client.post(f"/v1/admin/apps/{app.id}/approve", headers=admin_headers)
    flip = await client.patch(
        f"/v1/admin/apps/{app.id}", json={"loginRequired": True}, headers=admin_headers
    )
    assert flip.status_code == 200

    resp = await client.get(f"/v1/admin/apps/{app.id}/audit", headers=admin_headers)
    assert resp.status_code == 200
    events = resp.json()["events"]
    # GREEN precondition — the read path works and the count-bearing event is present.
    assert len(events) >= 2
    cfg = next(e for e in events if e["action"] == "config:loginRequired")

    # FIXED: AuditEventOut now resolves the actor's `username` (join on actor_id) and surfaces
    # `count` top-level, alongside the modern `id`/`createdAt`/`actorId`/`resourceId`. The SPA
    # AuditDrawer was modernized in the same change to read those names (id key, createdAt time,
    # resourceId) rather than the stale Mongo `_id`/`at`/`recordId`, so every row now renders a
    # real key, timestamp, and actor. (Resolution mirrors the owner/username admin fixes: add the
    # missing human field to the API, and point the SPA at the API's canonical camelCase names.)
    missing = [key for key in ("id", "createdAt", "username", "count") if key not in cfg]
    assert not missing, f"audit event missing SPA-required keys: {missing}"
