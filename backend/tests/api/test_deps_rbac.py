"""RBAC gate (requires_superadmin) exercised through a protected test route.

Overrides `current_user` (no live session cookie needed) and `superadmin_allowlist`
(no global Settings mutation), then asserts the gate returns 200 for an allowlisted
user and a plain 403 otherwise — the plan's U1 verification.
"""

from __future__ import annotations

import httpx
from fastapi import FastAPI

from src.api.deps import current_user
from src.api.deps_rbac import CurrentSuperadmin, superadmin_allowlist
from src.db.models.user import User
from tests.factories import UserFactory


def _build_app(user: User, allowlist: frozenset[str]) -> FastAPI:
    app = FastAPI()

    @app.get("/admin-only")
    async def _admin_only(admin: CurrentSuperadmin) -> dict[str, str]:
        return {"email": admin.email}

    # Short-circuit the cookie/DB auth chain and the Settings read.
    app.dependency_overrides[current_user] = lambda: user
    app.dependency_overrides[superadmin_allowlist] = lambda: allowlist
    return app


async def _get_admin_only(app: FastAPI) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get("/admin-only")


async def test_superadmin_passes() -> None:
    user = UserFactory.build(email="admin@bial.com")
    resp = await _get_admin_only(_build_app(user, frozenset({"admin@bial.com"})))
    assert resp.status_code == 200
    assert resp.json() == {"email": "admin@bial.com"}


async def test_citizen_is_forbidden() -> None:
    user = UserFactory.build(email="citizen@bial.com")
    resp = await _get_admin_only(_build_app(user, frozenset({"admin@bial.com"})))
    assert resp.status_code == 403
    # Plain, non-leaking message — no hint about who IS an admin.
    assert resp.json() == {"detail": "Super-admin privileges required."}


async def test_gate_is_case_insensitive() -> None:
    # A mixed-case IdP email still passes against a lowercased allowlist.
    user = UserFactory.build(email="Admin@BIAL.com")
    resp = await _get_admin_only(_build_app(user, frozenset({"admin@bial.com"})))
    assert resp.status_code == 200
