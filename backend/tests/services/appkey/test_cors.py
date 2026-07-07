"""Path-branching CORS (U4, R23) — `Origin: null` reflected ONLY on the sandbox
data routes (records/files/parse), credential-free; SPA/auth routes get credentialed
CORS for FRONTEND_URL only and NEVER reflect `null`."""

from __future__ import annotations

import uuid

from src.config import settings

_FRONTEND = settings.FRONTEND_URL
_DATA_PATH = f"/v1/apps/{uuid.uuid4()}/records"
_AUTH_PATH = "/v1/auth/me"
_PREFLIGHT = {"Access-Control-Request-Method": "POST"}


# --- data-route CORS: reflect any origin incl. null, no credentials ------------


async def test_data_route_preflight_reflects_null_origin(client) -> None:
    resp = await client.request("OPTIONS", _DATA_PATH, headers={"Origin": "null", **_PREFLIGHT})
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == "null"
    # Header-auth only — no cookies, so no ambient authority is granted by reflecting null.
    assert "access-control-allow-credentials" not in resp.headers
    assert "X-App-Key" in resp.headers["access-control-allow-headers"]
    assert resp.headers["access-control-max-age"] == "600"


async def test_data_route_actual_request_carries_reflected_origin(client) -> None:
    # No records route exists yet (U5) → 404, but the CORS middleware still attaches
    # the reflected-origin header to the response.
    resp = await client.get(_DATA_PATH, headers={"Origin": "null"})
    assert resp.headers["access-control-allow-origin"] == "null"
    assert "access-control-allow-credentials" not in resp.headers


async def test_data_route_never_sends_credentials(client) -> None:
    resp = await client.request("OPTIONS", _DATA_PATH, headers={"Origin": _FRONTEND, **_PREFLIGHT})
    assert resp.headers["access-control-allow-origin"] == _FRONTEND
    assert "access-control-allow-credentials" not in resp.headers


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
