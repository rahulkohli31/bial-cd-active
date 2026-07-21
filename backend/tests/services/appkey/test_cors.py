"""The app's one CORS layer (U4, R23) — credentialed CORS for FRONTEND_URL only,
`Origin: null` NEVER reflected on any path. The null-reflecting, credential-free
data-route branch died with the shared data plane (U6); the former data path is
kept here as a regression guard that it stays dead."""

from __future__ import annotations

import uuid

from src.config import settings

_FRONTEND = settings.FRONTEND_URL
_RETIRED_DATA_PATH = f"/v1/apps/{uuid.uuid4()}/records"
_AUTH_PATH = "/v1/auth/me"
_PREFLIGHT = {"Access-Control-Request-Method": "POST"}


# --- the retired data-route branch stays retired -------------------------------


async def test_retired_data_route_never_reflects_null_origin(client) -> None:
    resp = await client.request(
        "OPTIONS", _RETIRED_DATA_PATH, headers={"Origin": "null", **_PREFLIGHT}
    )
    assert "access-control-allow-origin" not in resp.headers


async def test_retired_data_route_actual_request_is_not_reflected(client) -> None:
    resp = await client.get(_RETIRED_DATA_PATH, headers={"Origin": "null"})
    assert resp.status_code == 404
    assert "access-control-allow-origin" not in resp.headers


# --- auth/SPA CORS: credentialed FRONTEND_URL only, never null -----------------


async def test_auth_route_never_reflects_null(client) -> None:
    resp = await client.request("OPTIONS", _AUTH_PATH, headers={"Origin": "null", **_PREFLIGHT})
    # `null` is not the frontend origin → no ACAO reflected on an auth route.
    assert resp.headers.get("access-control-allow-origin") != "null"
    assert "access-control-allow-origin" not in resp.headers


async def test_auth_route_preflight_is_credentialed_for_frontend(client) -> None:
    resp = await client.request("OPTIONS", _AUTH_PATH, headers={"Origin": _FRONTEND, **_PREFLIGHT})
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == _FRONTEND
    assert resp.headers["access-control-allow-credentials"] == "true"


async def test_spa_actual_request_is_credentialed(client) -> None:
    resp = await client.get("/v1/health", headers={"Origin": _FRONTEND})
    assert resp.headers["access-control-allow-origin"] == _FRONTEND
    assert resp.headers["access-control-allow-credentials"] == "true"


async def test_same_origin_request_has_no_cors_headers(client) -> None:
    resp = await client.get("/v1/health")
    assert "access-control-allow-origin" not in resp.headers
