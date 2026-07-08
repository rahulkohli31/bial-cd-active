"""SPA history fallback (U15): a reserved-root or traversal path 404s as JSON, a real
deep link returns index.html. The route's `responses={404}` is Sonar-only metadata;
these lock the unchanged runtime behavior."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from src.config import settings
from src.main import create_app


@pytest.fixture
def spa_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>SPA shell</title>")
    monkeypatch.setattr(settings, "spa_dist_dir", dist)
    return create_app()


@pytest.fixture
async def spa_client(spa_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=spa_app), base_url="http://test"
    ) as c:
        yield c


async def test_reserved_root_path_is_json_404(spa_client: httpx.AsyncClient) -> None:
    # A genuinely unmatched /v1/... must 404 as JSON, never fall through to index.html.
    resp = await spa_client.get("/v1/does-not-exist")
    assert resp.status_code == 404
    assert "text/html" not in resp.headers.get("content-type", "")


async def test_traversal_path_is_404(spa_client: httpx.AsyncClient) -> None:
    # URL-encoded `..` segments that would escape the dist root are refused
    # (arbitrary-file-read guard intact) rather than served.
    resp = await spa_client.get("/%2e%2e/%2e%2e/etc/passwd")
    assert resp.status_code == 404
    assert "text/html" not in resp.headers.get("content-type", "")


async def test_deep_link_returns_index(spa_client: httpx.AsyncClient) -> None:
    resp = await spa_client.get("/dashboard/settings")
    assert resp.status_code == 200
    assert "SPA shell" in resp.text
