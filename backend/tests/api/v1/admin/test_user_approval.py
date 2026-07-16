"""Pending approval: the approve endpoint and the THREE enforcement seams —
`current_user`, `POST /auth/refresh`, and the login callback (which, UNLIKE
suspension, does NOT block the redirect — a pending user's session establishes
normally; only `current_user` fail-closes every OTHER endpoint).

Mirrors `test_user_suspension.py`'s structure — see that file for the sibling
marker (`suspended_at`). Approval is independent of suspension: a never-approved
user is "pending", a suspended user is "disabled" regardless of approval state.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.db.models.audit import AuditLog
from src.db.models.user import User
from src.main import create_app
from src.services.auth.csrf import issue_csrf_token
from src.services.auth.oidc import get_oauth
from src.services.auth.session_jwt import mint_session_jwt
from tests.factories import UserFactory

_TTL = settings.auth.access_ttl_seconds


def _cookie(user: User) -> dict[str, str]:
    return {"Cookie": f"session={mint_session_jwt(user.id, user.token_version, _TTL)}"}


async def _admin(db: AsyncSession) -> dict[str, str]:
    return _cookie(await UserFactory.create(db, email="admin@bial.com"))


async def _approve(client, headers, user_id) -> Any:
    return await client.post(f"/v1/admin/users/{user_id}/approve", headers=headers)


# --- current_user seam -----------------------------------------------------------


async def test_pending_user_can_still_reach_auth_me(client, db_session) -> None:
    # /v1/auth/me is exempt — the SPA must be able to learn "pending" at all.
    pending = await UserFactory.create(db_session, email="pending@rvaiglobal.com", approved_at=None)
    resp = await client.get("/v1/auth/me", headers=_cookie(pending))
    assert resp.status_code == 200
    assert resp.json()["status"] == "pending"


async def test_pending_user_is_rejected_everywhere_else(client, db_session) -> None:
    pending = await UserFactory.create(db_session, email="gated@rvaiglobal.com", approved_at=None)
    resp = await client.get("/v1/projects", headers=_cookie(pending))
    assert resp.status_code == 403
    assert resp.json() == {"detail": "Pending approval"}


async def test_approved_user_is_not_gated(client, db_session) -> None:
    approved = await UserFactory.create(db_session, email="approved@rvaiglobal.com")
    resp = await client.get("/v1/projects", headers=_cookie(approved))
    assert resp.status_code == 200


# --- refresh seam: deliberately does NOT gate pending, unlike suspension -----------


async def test_refresh_seam_keeps_a_pending_user_signed_in(client, db_session) -> None:
    # A pending user's session must stay alive indefinitely while they wait —
    # current_user (not refresh) is the seam that fail-closes actual endpoints.
    # A refresh-time pending check was tried and reverted: see auth/router.py's
    # refresh() comment for why it silently logged pending users out.
    from src.services.auth.refresh import issue_new_family

    pending = await UserFactory.create(db_session, email="sneak2@rvaiglobal.com", approved_at=None)
    raw = await issue_new_family(db_session, pending.id)

    csrf = issue_csrf_token(pending.id, pending.token_version)
    resp = await client.post(
        "/v1/auth/refresh",
        headers={"Cookie": f"refresh={raw}; csrf={csrf}", "X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200
    assert "set-cookie" in {k.lower() for k in resp.headers}


# --- login-callback seam: does NOT block, unlike suspension -----------------------


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


async def test_first_time_signin_establishes_a_session_while_pending(app, client, db_session) -> None:
    _fake_signin(app, oid="brand-new-oid", email="new-hire@rvaiglobal.com")
    resp = await client.get("/v1/auth/callback")
    assert resp.status_code == 302
    assert resp.headers["location"] == settings.FRONTEND_URL  # a real session, not a bounce
    assert resp.headers.get_list("set-cookie") != []  # cookies WERE set

    user = await db_session.scalar(select(User).where(User.email == "new-hire@rvaiglobal.com"))
    assert user is not None
    assert user.approved_at is None
    assert user.status() == "pending"


async def test_superadmin_first_signin_is_auto_approved(app, client, db_session) -> None:
    # An allowlisted email must never be locked out for want of an approver.
    superadmin_email = next(iter(settings.superadmin_emails))
    _fake_signin(app, oid="fresh-superadmin-oid", email=superadmin_email)
    resp = await client.get("/v1/auth/callback")
    assert resp.status_code == 302
    assert resp.headers["location"] == settings.FRONTEND_URL

    user = await db_session.scalar(select(User).where(User.email == superadmin_email))
    assert user is not None
    assert user.approved_at is not None
    assert user.status() == "approved"


async def test_superadmin_promoted_after_first_signin_is_approved_on_next_signin(
    app, client, db_session
) -> None:
    # The lockout this guards against: a user signs in once (pending), THEN gets
    # added to SUPERADMIN_EMAILS — their next sign-in takes the conflict/update
    # path, which must also auto-approve, not just the fresh-insert path.
    superadmin_email = next(iter(settings.superadmin_emails))
    pending_row = await UserFactory.create(
        db_session, azure_oid="promoted-oid", email=superadmin_email, approved_at=None
    )
    assert pending_row.approved_at is None  # sanity: really pending before sign-in

    _fake_signin(app, oid="promoted-oid", email=superadmin_email)
    resp = await client.get("/v1/auth/callback")
    assert resp.status_code == 302
    assert resp.headers["location"] == settings.FRONTEND_URL

    await db_session.refresh(pending_row)
    assert pending_row.approved_at is not None
    assert pending_row.status() == "approved"


async def test_returning_users_approval_state_is_never_reset_on_signin(
    app, client, db_session
) -> None:
    citizen = await UserFactory.create(
        db_session, azure_oid="returning-oid", email="returning@rvaiglobal.com", approved_at=None
    )
    _fake_signin(app, oid="returning-oid", email="returning@rvaiglobal.com")
    await client.get("/v1/auth/callback")

    await db_session.refresh(citizen)
    assert citizen.approved_at is None  # still pending — the upsert never touches it


# --- endpoint contract -------------------------------------------------------------


async def test_approve_endpoint_contract(client, db_session) -> None:
    from uuid import uuid4

    pending = await UserFactory.create(db_session, email="approveme@rvaiglobal.com", approved_at=None)
    admin_headers = await _admin(db_session)

    assert (await _approve(client, admin_headers, uuid4())).status_code == 404

    resp = await _approve(client, admin_headers, pending.id)
    assert resp.status_code == 200
    body = resp.json()
    assert body["userId"] == str(pending.id)
    assert body["approvedAt"] is not None

    # Already approved -> 409
    assert (await _approve(client, admin_headers, pending.id)).status_code == 409


async def test_approve_is_audited(client, db_session) -> None:
    pending = await UserFactory.create(db_session, email="ledger2@rvaiglobal.com", approved_at=None)
    admin_headers = await _admin(db_session)
    await _approve(client, admin_headers, pending.id)

    actions = (
        (
            await db_session.execute(
                select(AuditLog.action)
                .where(AuditLog.resource_type == "user", AuditLog.resource_id == str(pending.id))
                .order_by(AuditLog.created_at)
            )
        )
        .scalars()
        .all()
    )
    assert actions == ["user:approve"]


async def test_approve_does_not_touch_sessions(client, db_session) -> None:
    # Approving isn't a revocation — token_version must be untouched.
    pending = await UserFactory.create(db_session, email="notouch@rvaiglobal.com", approved_at=None)
    original_version = pending.token_version
    admin_headers = await _admin(db_session)
    await _approve(client, admin_headers, pending.id)

    await db_session.refresh(pending)
    assert pending.token_version == original_version


async def test_citizen_is_forbidden_from_approving(client, db_session) -> None:
    plain = await UserFactory.create(db_session, email="plain2@rvaiglobal.com")
    other = await UserFactory.create(db_session, email="other2@rvaiglobal.com", approved_at=None)
    assert (await _approve(client, _cookie(plain), other.id)).status_code == 403


def test_approve_route_documents_error_codes_in_openapi() -> None:
    paths = create_app().openapi()["paths"]
    codes = set(paths["/v1/admin/users/{user_id}/approve"]["post"]["responses"])
    assert {"401", "403", "404", "409", "500"} <= codes
