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


# --- a configured-but-broken SPA image refuses to boot (fail-closed) -----------


def test_dist_dir_without_index_refuses_to_boot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Silently skipping the mount served a JSON 404 for `/` — a broken image looked healthy.
    dist = tmp_path / "dist"
    dist.mkdir()
    monkeypatch.setattr(settings, "spa_dist_dir", dist)
    with pytest.raises(RuntimeError, match="index.html"):
        create_app()


def test_absent_dist_dir_refuses_to_boot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "spa_dist_dir", tmp_path / "never-built")
    with pytest.raises(RuntimeError, match="index.html"):
        create_app()


def test_unset_spa_dist_dir_boots_without_a_spa(monkeypatch: pytest.MonkeyPatch) -> None:
    # The defined None meaning: two-process local dev, where Vite serves the SPA on :5173.
    monkeypatch.setattr(settings, "spa_dist_dir", None)
    assert create_app() is not None
