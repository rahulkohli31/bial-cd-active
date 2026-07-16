"""Local suspension (U10, R10–R14, KD-6): deactivate/reactivate endpoints and the
THREE enforcement seams — login callback, `current_user`, and `POST /auth/refresh`.

Deactivate = `suspended_at` + a token_version bump + refresh-family revocation, so a
live session dies instantly (the `current_user` seam 403s the old JWT — suspension is
checked BEFORE the token_version bump so the SPA surfaces it instead of silently
refreshing, KD-2 — while the refresh seam still 401s it), a captured refresh cookie
cannot re-mint, an outstanding runner token dies at the data-plane check, and a fresh
Entra sign-in bounces to the login banner. Self/peer super-admins are protected (AE6);
both actions are audited.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.db.models.app_registry import AppStatus
from src.db.models.audit import AuditLog
from src.db.models.refresh_token import RefreshToken
from src.db.models.user import User
from src.main import create_app
from src.services.auth.csrf import issue_csrf_token
from src.services.auth.oidc import get_oauth
from src.services.auth.refresh import issue_new_family
from src.services.auth.session_jwt import mint_session_jwt
from tests.factories import AppRegistryFactory, UserFactory

_TTL = settings.auth.access_ttl_seconds


def _cookie(user: User) -> dict[str, str]:
    return {"Cookie": f"session={mint_session_jwt(user.id, user.token_version, _TTL)}"}


async def _admin(db: AsyncSession) -> dict[str, str]:
    return _cookie(await UserFactory.create(db, email="admin@bial.com"))


async def _deactivate(client, headers, user_id) -> Any:
    return await client.post(f"/v1/admin/users/{user_id}/deactivate", headers=headers)


async def _reactivate(client, headers, user_id) -> Any:
    return await client.post(f"/v1/admin/users/{user_id}/reactivate", headers=headers)


# --- the AE5 core: a live session dies immediately ------------------------------


async def test_deactivate_kills_live_session_immediately(client, db_session) -> None:
    citizen = await UserFactory.create(db_session, email="live@rvaiglobal.com")
    live_cookie = _cookie(citizen)
    assert (await client.get("/v1/auth/me", headers=live_cookie)).status_code == 200

    admin_headers = await _admin(db_session)
    resp = await _deactivate(client, admin_headers, citizen.id)
    assert resp.status_code == 200
    body = resp.json()
    assert body["userId"] == str(citizen.id)
    assert body["suspendedAt"] is not None

    # The pre-suspension JWT is refused instantly. The token_version bump AND the
    # suspended_at flag would each kill it; current_user checks suspension FIRST (KD-2), so
    # the status is the 403 the SPA surfaces — not a 401 it would silently refresh past.
    resp = await client.get("/v1/auth/me", headers=live_cookie)
    assert resp.status_code == 403
    assert resp.json() == {"detail": "Account suspended"}


async def test_current_user_seam_403s_even_a_valid_jwt(client, db_session) -> None:
    # Defense-in-depth: were a validly-versioned JWT ever to exist for a suspended
    # user, the `current_user` seam itself refuses with a 403 (not just the bump).
    citizen = await UserFactory.create(db_session, email="seam@rvaiglobal.com")
    admin_headers = await _admin(db_session)
    assert (await _deactivate(client, admin_headers, citizen.id)).status_code == 200

    await db_session.refresh(citizen)  # pick up the bumped token_version
    fresh_cookie = _cookie(citizen)  # signed with the CURRENT version — still refused
    resp = await client.get("/v1/auth/me", headers=fresh_cookie)
    assert resp.status_code == 403
    assert resp.json() == {"detail": "Account suspended"}


async def test_current_user_401s_a_stale_token_not_suspended(client, db_session) -> None:
    # The other side of KD-2: reordering suspended_at ahead of token_version must NOT weaken
    # revocation for the ordinary case. A token whose version is stale (logout, a reactivated
    # user's old session — token_version bumped, suspended_at null) still 401s; it must never
    # fall through the suspension check to a 200.
    citizen = await UserFactory.create(db_session, email="stale@rvaiglobal.com")
    stale_cookie = _cookie(citizen)  # signed with the CURRENT version...
    citizen.token_version += 1  # ...which a logout/revocation then bumps past
    await db_session.flush()

    resp = await client.get("/v1/auth/me", headers=stale_cookie)
    assert resp.status_code == 401
    assert resp.json() != {"detail": "Account suspended"}  # a 401, not the suspension 403


# --- refresh seam (KD-6) ---------------------------------------------------------


async def test_deactivate_revokes_refresh_families(client, db_session) -> None:
    citizen = await UserFactory.create(db_session, email="fam@rvaiglobal.com")
    raw = await issue_new_family(db_session, citizen.id)
    admin_headers = await _admin(db_session)
    assert (await _deactivate(client, admin_headers, citizen.id)).status_code == 200

    active = await db_session.scalar(
        select(func.count())
        .select_from(RefreshToken)
        .where(RefreshToken.user_id == citizen.id, RefreshToken.revoked.is_(False))
    )
    assert active == 0  # every family revoked, as logout does

    # Even with a fresh CSRF pair (post-bump version), the captured refresh cookie
    # cannot re-mint a session.
    await db_session.refresh(citizen)
    csrf = issue_csrf_token(citizen.id, citizen.token_version)
    resp = await client.post(
        "/v1/auth/refresh",
        headers={"Cookie": f"refresh={raw}; csrf={csrf}", "X-CSRF-Token": csrf},
    )
    assert resp.status_code == 401
    assert "set-cookie" not in {k.lower() for k in resp.headers}


async def test_refresh_seam_rejects_suspended_user_with_live_family(client, db_session) -> None:
    # Seam-specific: suspend WITHOUT revocation (direct column write) so the family
    # is still live — the explicit `/auth/refresh` check must still refuse (R11).
    citizen = await UserFactory.create(db_session, email="sneak@rvaiglobal.com")
    raw = await issue_new_family(db_session, citizen.id)
    citizen.suspended_at = datetime.now(UTC)
    await db_session.flush()

    csrf = issue_csrf_token(citizen.id, citizen.token_version)
    resp = await client.post(
        "/v1/auth/refresh",
        headers={"Cookie": f"refresh={raw}; csrf={csrf}", "X-CSRF-Token": csrf},
    )
    assert resp.status_code == 401
    assert "set-cookie" not in {k.lower() for k in resp.headers}


# --- login-callback seam ---------------------------------------------------------


class _FakeEntra:
    def __init__(self, token: dict[str, Any]) -> None:
        self._token = token

    async def authorize_access_token(self, request: Any) -> dict[str, Any]:
        return self._token


class _FakeOAuth:
    def __init__(self, entra: _FakeEntra) -> None:
        self.entra = entra


def _fake_signin(app: Any, *, oid: str, email: str) -> None:
    token = {
        "userinfo": {
            "oid": oid,
            "sub": f"sub-{oid}",
            "tid": settings.auth.tenant_id,
            "email": email,
            "preferred_username": email,
        },
        "access_token": "x",
        "id_token": "y",
    }
    app.dependency_overrides[get_oauth] = lambda: _FakeOAuth(_FakeEntra(token))


async def test_login_callback_blocks_suspended_user(app, client, db_session) -> None:
    citizen = await UserFactory.create(
        db_session, azure_oid="suspended-oid", email="blocked@rvaiglobal.com"
    )
    citizen.suspended_at = datetime.now(UTC)
    await db_session.flush()
    _fake_signin(app, oid="suspended-oid", email="blocked@rvaiglobal.com")

    resp = await client.get("/v1/auth/callback")
    assert resp.status_code == 302
    assert resp.headers["location"] == f"{settings.FRONTEND_URL}/login?authError=account_suspended"
    assert resp.headers.get_list("set-cookie") == []  # no session minted
    # No refresh family was issued for the refused sign-in.
    families = await db_session.scalar(
        select(func.count()).select_from(RefreshToken).where(RefreshToken.user_id == citizen.id)
    )
    assert families == 0


async def test_reactivated_user_can_sign_in_again(app, client, db_session) -> None:
    citizen = await UserFactory.create(
        db_session, azure_oid="comeback-oid", email="comeback@rvaiglobal.com"
    )
    admin_headers = await _admin(db_session)
    assert (await _deactivate(client, admin_headers, citizen.id)).status_code == 200
    resp = await _reactivate(client, admin_headers, citizen.id)
    assert resp.status_code == 200
    assert resp.json()["suspendedAt"] is None

    _fake_signin(app, oid="comeback-oid", email="comeback@rvaiglobal.com")
    signin = await client.get("/v1/auth/callback")
    assert signin.status_code == 302
    assert signin.headers["location"] == settings.FRONTEND_URL  # a real sign-in again


async def test_reactivate_does_not_resurrect_old_sessions(client, db_session) -> None:
    citizen = await UserFactory.create(db_session, email="lazarus@rvaiglobal.com")
    old_cookie = _cookie(citizen)  # minted pre-suspension
    admin_headers = await _admin(db_session)
    assert (await _deactivate(client, admin_headers, citizen.id)).status_code == 200
    assert (await _reactivate(client, admin_headers, citizen.id)).status_code == 200

    # The pre-suspension JWT stays dead (the bump is permanent); a fresh session works.
    assert (await client.get("/v1/auth/me", headers=old_cookie)).status_code == 401
    await db_session.refresh(citizen)
    assert (await client.get("/v1/auth/me", headers=_cookie(citizen))).status_code == 200


# --- runner tokens (learning: sandboxed-app-auth-session-injection) ---------------


async def test_outstanding_runner_token_dies_on_deactivate(client, db_session) -> None:
    # Regression only (no code change): the runner token carries token_version, so
    # the deactivate bump kills it at `require_login_if_required`.
    citizen = await UserFactory.create(db_session, email="frame@rvaiglobal.com")
    app_row = await AppRegistryFactory.create(
        db_session, user_id=citizen.id, status=AppStatus.APPROVED, login_required=True
    )
    # Mint the outstanding runner token inline (the dedicated mint_runner_token wrapper was
    # retired); it is the same signed session JWT the runner frame would have carried.
    outstanding = mint_session_jwt(citizen.id, citizen.token_version, _TTL)
    data_headers = {"X-App-Key": app_row.app_key, "Authorization": f"Bearer {outstanding}"}
    assert (
        await client.get(f"/v1/apps/{app_row.id}/records", headers=data_headers)
    ).status_code == 200

    admin_headers = await _admin(db_session)
    assert (await _deactivate(client, admin_headers, citizen.id)).status_code == 200
    resp = await client.get(f"/v1/apps/{app_row.id}/records", headers=data_headers)
    assert resp.status_code == 401  # dead on the token_version bump


# --- AE6: self / peer-admin protection --------------------------------------------


async def test_admin_cannot_suspend_self(client, db_session) -> None:
    admin = await UserFactory.create(db_session, email="admin@bial.com")
    resp = await _deactivate(client, _cookie(admin), admin.id)
    assert resp.status_code == 403
    await db_session.refresh(admin)
    assert admin.suspended_at is None  # nothing applied


async def test_admin_cannot_suspend_peer_admin(client, db_session) -> None:
    peer = await UserFactory.create(db_session, email="superadmin@bial.com")  # allowlisted
    admin_headers = await _admin(db_session)
    resp = await _deactivate(client, admin_headers, peer.id)
    assert resp.status_code == 403
    await db_session.refresh(peer)
    assert peer.suspended_at is None
    # Nothing audited as applied for the refused action.
    audited = await db_session.scalar(
        select(func.count()).select_from(AuditLog).where(AuditLog.action == "user:deactivate")
    )
    assert audited == 0


# --- endpoint contract -------------------------------------------------------------


async def test_unknown_user_404_and_state_conflicts_409(client, db_session) -> None:
    from uuid import uuid4

    citizen = await UserFactory.create(db_session, email="state@rvaiglobal.com")
    admin_headers = await _admin(db_session)

    assert (await _deactivate(client, admin_headers, uuid4())).status_code == 404
    assert (await _reactivate(client, admin_headers, uuid4())).status_code == 404
    # Reactivating an active user conflicts; double-deactivate conflicts.
    assert (await _reactivate(client, admin_headers, citizen.id)).status_code == 409
    assert (await _deactivate(client, admin_headers, citizen.id)).status_code == 200
    assert (await _deactivate(client, admin_headers, citizen.id)).status_code == 409


async def test_both_actions_are_audited(client, db_session) -> None:
    citizen = await UserFactory.create(db_session, email="ledger@rvaiglobal.com")
    admin_headers = await _admin(db_session)
    await _deactivate(client, admin_headers, citizen.id)
    await _reactivate(client, admin_headers, citizen.id)

    actions = (
        (
            await db_session.execute(
                select(AuditLog.action)
                .where(AuditLog.resource_type == "user", AuditLog.resource_id == str(citizen.id))
                .order_by(AuditLog.created_at)
            )
        )
        .scalars()
        .all()
    )
    assert actions == ["user:deactivate", "user:reactivate"]


async def test_citizen_is_forbidden(client, db_session) -> None:
    plain = await UserFactory.create(db_session, email="plain@rvaiglobal.com")
    other = await UserFactory.create(db_session, email="other@rvaiglobal.com")
    assert (await _deactivate(client, _cookie(plain), other.id)).status_code == 403
    assert (await _reactivate(client, _cookie(plain), other.id)).status_code == 403


def test_suspension_routes_document_error_codes_in_openapi() -> None:
    paths = create_app().openapi()["paths"]
    for action in ("deactivate", "reactivate"):
        codes = set(paths[f"/v1/admin/users/{{user_id}}/{action}"]["post"]["responses"])
        assert {"401", "403", "404", "409", "500"} <= codes
