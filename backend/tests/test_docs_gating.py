"""Interactive API docs are disabled in production (U17): the enriched OpenAPI spec
(error taxonomy, named codes, admin route enumeration) must not be served pre-auth."""

from __future__ import annotations

import pathlib

import httpx
import pytest
from fastapi import FastAPI

from src.config import settings
from src.main import create_app


async def _get(app: FastAPI, path: str) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        return await c.get(path)


@pytest.mark.parametrize("path", ["/openapi.json", "/docs", "/redoc"])
async def test_docs_routes_404_in_production(monkeypatch, path: str) -> None:
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    app = create_app()
    assert (await _get(app, path)).status_code == 404


async def test_openapi_served_in_development(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    app = create_app()
    schema = await _get(app, "/openapi.json")
    assert schema.status_code == 200
    assert schema.json()["info"]["title"] == "BIAL Backend"
    assert (await _get(app, "/docs")).status_code == 200


async def test_openapi_json_in_production_serves_spa_index_not_schema(
    monkeypatch, tmp_path: pathlib.Path
) -> None:
    # In production `openapi_url=None`, so `/openapi.json` is NOT an API route. With the
    # SPA mounted, that path is a non-reserved root → the history fallback serves
    # index.html. The prod-fidelity guard: a pre-auth `/openapi.json` returns the SPA
    # shell (200 text/html), never the enriched schema.
    (tmp_path / "index.html").write_text("<!doctype html><title>SPA</title><div id=root></div>")
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "spa_dist_dir", tmp_path)
    app = create_app()

    resp = await _get(app, "/openapi.json")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "openapi" not in resp.text
