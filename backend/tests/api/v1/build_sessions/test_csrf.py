"""The reusable CSRF dependency: mutating POSTs require a valid signed double-submit
token; the status GET is exempt."""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable, Iterator

import pytest
from fastapi.routing import APIRoute, _IncludedRouter
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps_csrf import require_csrf
from src.api.deps_rbac import superadmin_allowlist
from src.config import settings
from src.services.auth.session_jwt import mint_session_jwt
from tests.api.v1.build_sessions.conftest import a_live_session, auth_headers
from tests.factories import ProjectFactory, UserFactory

_TTL = settings.auth.access_ttl_seconds

# EVERY mutating POST under `/v1/build-sessions`, with no exemptions — the bare collection POST
# that used to be one (`start`) is deleted, and with it the three focused tests that covered it
# instead of this table. The signed double-submit CSRF dependency short-circuits with a 403
# BEFORE the route body — so a bogus path id is fine; the CSRF check fails first. `internal/reap`
# also carries the superadmin gate, so the caller is allowlisted to prove CSRF (not RBAC) is the
# failing check.
_MUTATING_POSTS = [
    "/v1/build-sessions/{sid}/stop",
    # Lock coordination (`lock/acquire`/`lock/renew`/`lock/release`/`heartbeat`, and finally
    # `lock/force-end`) is retired — the block banner's Force-end button was its last caller —
    # so THERE ARE NO LOCK OPS IN THIS TABLE AT ALL. The kill switch a citizen actually reaches
    # is `projects/{project_id}/stop-active-build`, below.
    "/v1/build-sessions/internal/reap",
    "/v1/build-sessions/projects/{project_id}/save",
    "/v1/build-sessions/projects/{project_id}/client-error",
    "/v1/build-sessions/projects/{project_id}/workspace-check",
    "/v1/build-sessions/internal/apps/{app_id}/parked",
    "/v1/build-sessions/internal/apps/{app_id}/promote",
    "/v1/build-sessions/relaunch",
    "/v1/build-sessions/projects/{project_id}/stop-active-build",
    "/v1/build-sessions/projects/{project_id}/release",
    "/v1/build-sessions/projects/{project_id}/shared-launch",
    "/v1/build-sessions/projects/{project_id}/shared-refresh",
    "/v1/build-sessions/shared-view/release",
]

_PLACEHOLDER = re.compile(r"\{[^}]+\}")


def _normalize_path(path: str) -> str:
    """Collapse every `{param}` segment to a bare `{}` so a route's real path-param name
    (`{session_id}`) compares equal to this file's shorthand (`{sid}`)."""
    return _PLACEHOLDER.sub("{}", path)


def _walk_api_routes(routes: Iterable[object]) -> Iterator[APIRoute]:
    """Recurse through FastAPI's lazy route inclusion to reach real `APIRoute`s.

    `app.routes` only lists a handful of top-level entries — this FastAPI version defers each
    `include_router()` behind `_IncludedRouter`. `.original_router.routes` is the real sub-tree;
    each leaf `APIRoute.path` is already fully resolved against every prefix except the
    top-level app mount (`/v1`), which the caller adds once (verified against `app.openapi()`'s
    own path list)."""
    for route in routes:
        if isinstance(route, _IncludedRouter):
            yield from _walk_api_routes(route.original_router.routes)
        elif isinstance(route, APIRoute):
            yield route


def _csrf_guarded_build_session_posts() -> set[str]:
    """Every POST route under `/v1/build-sessions` that actually carries `RequireCsrf` in its
    route-level `dependencies`, read straight from the live route table rather than trusted from
    a doc comment — a route can only end up in this set by the router decorator itself naming
    `require_csrf`."""
    from src.main import app

    top_prefix = next(
        r.original_router.prefix for r in app.routes if isinstance(r, _IncludedRouter)
    )
    guarded: set[str] = set()
    for route in _walk_api_routes(app.routes):
        full_path = top_prefix + route.path
        if not full_path.startswith("/v1/build-sessions"):
            continue
        if route.methods is None or "POST" not in route.methods:
            continue
        if any(dep.dependency is require_csrf for dep in route.dependencies):
            guarded.add(_normalize_path(full_path))
    return guarded


def test_mutating_posts_matrix_matches_the_route_table() -> None:
    """Nothing asserts the collected parametrization count, so a row silently dropped from
    this hand-maintained table would make the CSRF matrix SHRINK while staying green.

    Walks the real FastAPI route table for every POST under `/v1/build-sessions` that carries
    `RequireCsrf`, and asserts that set equals `_MUTATING_POSTS` EXACTLY. A route added with
    `RequireCsrf` but no row here fails this test; so does a row left behind for a route that
    lost the dependency or was deleted.

    The exemption set this used to subtract (`start`, covered by focused tests instead) is gone
    with the route: an equality against the whole table is the stronger guard, and there is
    nothing left to exempt."""
    actual = _csrf_guarded_build_session_posts()
    expected = {_normalize_path(p) for p in _MUTATING_POSTS}
    assert actual == expected, (
        "_MUTATING_POSTS is out of step with the route table.\n"
        f"In the route table but missing a row here: {sorted(actual - expected)}\n"
        f"Has a row here but not CSRF-guarded (or gone) in the route table: "
        f"{sorted(expected - actual)}"
    )


def _slug(path_tmpl: str) -> str:
    return (
        path_tmpl.strip("/")
        .replace("/", "-")
        .replace("{sid}", "sid")
        .replace("{project_id}", "project_id")
        .replace("{app_id}", "app_id")
    )


async def _user_project(db: AsyncSession, email: str):
    user = await UserFactory.create(db, email=email)
    project = await ProjectFactory.create(db, user.id)
    return user, project


@pytest.mark.parametrize("path_tmpl", _MUTATING_POSTS)
async def test_missing_csrf_header_is_403_on_every_mutating_post(
    client: AsyncClient, db_session: AsyncSession, wire, path_tmpl: str
) -> None:
    user = await UserFactory.create(
        db_session, email=f"csrf-miss-{_slug(path_tmpl)}@rvaiglobal.com"
    )
    wire.app.dependency_overrides[superadmin_allowlist] = lambda: frozenset({user.email})
    path = path_tmpl.format(sid=uuid.uuid4(), project_id=uuid.uuid4(), app_id=uuid.uuid4())
    resp = await client.post(path, headers=auth_headers(user, with_csrf=False))
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "csrf_failed"


@pytest.mark.parametrize("path_tmpl", _MUTATING_POSTS)
async def test_mismatched_csrf_token_is_403_on_every_mutating_post(
    client: AsyncClient, db_session: AsyncSession, wire, path_tmpl: str
) -> None:
    user = await UserFactory.create(
        db_session, email=f"csrf-mism-{_slug(path_tmpl)}@rvaiglobal.com"
    )
    wire.app.dependency_overrides[superadmin_allowlist] = lambda: frozenset({user.email})
    jwt = mint_session_jwt(user.id, user.token_version, _TTL)
    headers = {"Cookie": f"session={jwt}; csrf=aaa.bbb", "X-CSRF-Token": "ccc.ddd"}
    path = path_tmpl.format(sid=uuid.uuid4(), project_id=uuid.uuid4(), app_id=uuid.uuid4())
    resp = await client.post(path, headers=headers)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "csrf_failed"


async def test_status_get_needs_no_csrf(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    user, project = await _user_project(db_session, "csrf4@rvaiglobal.com")
    session = await a_live_session(wire, db_session, user, project.id)
    # A cookie-only GET (no X-CSRF-Token) is accepted.
    jwt = mint_session_jwt(user.id, user.token_version, _TTL)
    s = await client.get(
        f"/v1/build-sessions/{session.session_id}", headers={"Cookie": f"session={jwt}"}
    )
    assert s.status_code == 200
