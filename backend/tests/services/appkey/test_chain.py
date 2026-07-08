"""The X-App-Key auth chain (U4, R23, AE3) — fail-closed on every mismatch.

Exercised through a standalone app mounting a chain-protected records-like route, so
the exact status codes (401/403/404/429) and the 403-before-404 ordering are pinned
independent of the records endpoints (U5)."""

from __future__ import annotations

import httpx
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.errors import register_exception_handlers
from src.db.models.app_registry import AppStatus
from src.db.session import get_db
from src.services.appkey.chain import InjectedUser, RequireAppKey, make_per_app_limiter
from src.services.auth.session_jwt import mint_session_jwt
from src.services.ratelimit import install_rate_limiting
from tests.factories import AppRegistryFactory, UserFactory

_TTL = 900


def _build_app(db_session: AsyncSession, *, limit: int = 1000) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    install_rate_limiting(app)
    limiter = make_per_app_limiter(
        limit=limit, window_seconds=60, message="Too many requests for this app. Please slow down."
    )

    @app.get("/apps/{app_id}/records", dependencies=[Depends(limiter)])
    async def _records(ctx: RequireAppKey, user: InjectedUser) -> dict[str, str | None]:
        return {"appId": str(ctx.app_id), "userId": str(user.id) if user else None}

    async def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _get(app: FastAPI, path: str, **headers: str) -> httpx.Response:
    async with _client(app) as c:
        return await c.get(path, headers=headers)


async def _make_app(db: AsyncSession, **overrides: object):
    user = await UserFactory.create(db)
    return await AppRegistryFactory.create(db, user_id=user.id, **overrides)


# --- fail-closed status matrix (AE3) -------------------------------------------


async def test_missing_key_is_401(db_session) -> None:
    app_row = await _make_app(db_session, status=AppStatus.APPROVED)
    resp = await _get(_build_app(db_session), f"/apps/{app_row.id}/records")
    assert resp.status_code == 401
    assert resp.json()["error"]["message"] == "Missing or invalid app key."


async def test_unknown_key_is_401(db_session) -> None:
    app_row = await _make_app(db_session, status=AppStatus.APPROVED)
    resp = await _get(
        _build_app(db_session), f"/apps/{app_row.id}/records", **{"X-App-Key": "bial_nope"}
    )
    assert resp.status_code == 401


async def test_disabled_app_is_403(db_session) -> None:
    app_row = await _make_app(db_session, status=AppStatus.DISABLED)
    resp = await _get(
        _build_app(db_session), f"/apps/{app_row.id}/records", **{"X-App-Key": app_row.app_key}
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["message"] == "This app is not available."


async def test_rejected_app_is_403(db_session) -> None:
    app_row = await _make_app(db_session, status=AppStatus.REJECTED)
    resp = await _get(
        _build_app(db_session), f"/apps/{app_row.id}/records", **{"X-App-Key": app_row.app_key}
    )
    assert resp.status_code == 403


async def test_key_vs_url_mismatch_is_404(db_session) -> None:
    app_row = await _make_app(db_session, status=AppStatus.APPROVED)
    other = await _make_app(db_session, status=AppStatus.APPROVED)
    # A valid key for `app_row` used on `other`'s URL → 404 (no cross-app enumeration).
    resp = await _get(
        _build_app(db_session), f"/apps/{other.id}/records", **{"X-App-Key": app_row.app_key}
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["message"] == "App not found."


async def test_disabled_before_mismatch_403_wins(db_session) -> None:
    # The 403 status check runs BEFORE the 404 URL cross-check: a disabled app's key
    # on a mismatched URL is 403, not 404 (load-bearing ordering).
    disabled = await _make_app(db_session, status=AppStatus.DISABLED)
    other = await _make_app(db_session, status=AppStatus.APPROVED)
    resp = await _get(
        _build_app(db_session), f"/apps/{other.id}/records", **{"X-App-Key": disabled.app_key}
    )
    assert resp.status_code == 403


# --- loginRequired live gate ---------------------------------------------------


async def test_login_not_required_allows_anonymous(db_session) -> None:
    app_row = await _make_app(db_session, status=AppStatus.APPROVED, login_required=False)
    resp = await _get(
        _build_app(db_session), f"/apps/{app_row.id}/records", **{"X-App-Key": app_row.app_key}
    )
    assert resp.status_code == 200
    assert resp.json()["userId"] is None


async def test_login_required_without_bearer_is_401(db_session) -> None:
    app_row = await _make_app(db_session, status=AppStatus.APPROVED, login_required=True)
    resp = await _get(
        _build_app(db_session), f"/apps/{app_row.id}/records", **{"X-App-Key": app_row.app_key}
    )
    assert resp.status_code == 401


async def test_login_required_with_valid_bearer_allows(db_session) -> None:
    user = await UserFactory.create(db_session, email="in-app@rvaiglobal.com")
    app_row = await AppRegistryFactory.create(
        db_session, user_id=user.id, status=AppStatus.APPROVED, login_required=True
    )
    bearer = mint_session_jwt(user.id, user.token_version, _TTL)
    resp = await _get(
        _build_app(db_session),
        f"/apps/{app_row.id}/records",
        **{"X-App-Key": app_row.app_key, "Authorization": f"Bearer {bearer}"},
    )
    assert resp.status_code == 200
    assert resp.json()["userId"] == str(user.id)


async def test_login_required_stale_token_version_is_401(db_session) -> None:
    user = await UserFactory.create(db_session, token_version=2)
    app_row = await AppRegistryFactory.create(
        db_session, user_id=user.id, status=AppStatus.APPROVED, login_required=True
    )
    stale = mint_session_jwt(user.id, 0, _TTL)  # minted against an older version
    resp = await _get(
        _build_app(db_session),
        f"/apps/{app_row.id}/records",
        **{"X-App-Key": app_row.app_key, "Authorization": f"Bearer {stale}"},
    )
    assert resp.status_code == 401


# --- per-app limiter (keyed on appId) ------------------------------------------


async def test_limiter_keys_on_app_and_429s_over_ceiling(db_session) -> None:
    app_row = await _make_app(db_session, status=AppStatus.APPROVED)
    app = _build_app(db_session, limit=2)
    headers = {"X-App-Key": app_row.app_key}
    async with _client(app) as c:
        assert (await c.get(f"/apps/{app_row.id}/records", headers=headers)).status_code == 200
        assert (await c.get(f"/apps/{app_row.id}/records", headers=headers)).status_code == 200
        third = await c.get(f"/apps/{app_row.id}/records", headers=headers)
    assert third.status_code == 429
    assert third.json()["error"]["message"] == "Too many requests for this app. Please slow down."


@pytest.mark.parametrize("bad", ["Bearer", "Basic xyz", "garbage"])
async def test_login_required_malformed_authorization_is_401(db_session, bad: str) -> None:
    app_row = await _make_app(db_session, status=AppStatus.APPROVED, login_required=True)
    resp = await _get(
        _build_app(db_session),
        f"/apps/{app_row.id}/records",
        **{"X-App-Key": app_row.app_key, "Authorization": bad},
    )
    assert resp.status_code == 401
