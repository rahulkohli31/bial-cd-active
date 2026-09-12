"""The five live endpoints Slice 1 adds (#198 R1-R14): `POST/{project_id}:share`,
`POST /{project_id}:unshare`, `GET /{project_id}/shares`, `GET /colleagues`,
`GET /shared`, plus `GET /{project_id}`'s new `access` field.

`services/projects/shares.py` and `resolve.py`'s own branching are already covered directly
in `tests/services/projects/` — these tests are about the HTTP surface: auth, owner-only
enforcement, the one relaxed read (R11), and cross-user isolation (R11's own explicit
instruction to test this).
"""

from __future__ import annotations

import contextlib
import uuid

import pytest
from pydantic import SecretStr

from src.api.v1.build_sessions.deps import (
    sandbox_dependency,
    sandbox_or_none_dependency,
    session_manager_dependency,
)
from src.config import settings
from src.db.models.project import Project
from src.services.build_sessions import SessionManager
from src.services.build_sessions.appdata import resolve_app_for_project
from src.services.build_sessions.manager import shr_name_for
from src.services.sandbox.config import SandboxConfig
from src.services.storage import accessor as storage_accessor
from src.services.storage import snapshot_key
from tests.api.v1.projects.conftest import _VALID_DESCRIPTION, DELETE_BODY
from tests.api.v1.projects.test_projects_crud import _auth
from tests.factories import UserFactory
from tests.fakes import FakeSandboxClient, FakeStorage


@pytest.fixture
def _sandbox_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """`launch_shared_preview` (used by `test_unshare_tears_down_the_colleagues_live_container`)
    provisions through the real `SessionManager`, which reads `settings.sandbox` to build the
    container spec — unset in the test environment. Mirrors `test_manager.py`'s own fixture of
    the same name. NOT autouse: `sandbox_or_none_dependency` returning a real client (rather than
    `None`) changes `:unshare`'s own branching for every OTHER test in this file, routing it into
    `revoke_shared_preview`'s Redis-backed teardown instead of the no-op arm most of them exercise
    — so only the one test that actually launches a shared container should request this.

    This file's manager DOES touch Redis directly — `_launch_shared_preview_under_one_build_id`
    (`manager.py:3585`) and `revoke_shared_preview` (`manager.py:2635`) both call `get_redis()` —
    so the one test that requests this fixture also needs `fake_redis` alongside it; sandbox
    config alone gets it past `SandboxNotConfiguredError` only to fail at `RedisNotConfiguredError`
    one call later."""
    monkeypatch.setattr(
        settings,
        "sandbox",
        SandboxConfig(
            subscription_id="s",
            resource_group="r",
            region="westeurope",
            managed_environment_name="aca-env",
            acr_server="acr.azurecr.io",
            acr_username="acr-user",
            acr_password=SecretStr("acr-pass"),
            image_ref="acr/img:latest",
        ),
    )


@pytest.fixture
def bind_store(monkeypatch: pytest.MonkeyPatch):
    """`create_share` gates on `snapshot_presence`, which resolves through the object-store
    accessor singleton (same seam `test_relaunch_claim.py` binds to) — NOT the
    `storage_dependency` FastAPI override `conftest.py`'s `_override_storage` installs, which
    only covers the attachments router."""

    def _bind(store: FakeStorage) -> FakeStorage:
        monkeypatch.setattr(storage_accessor, "_backend_singleton", store)
        return store

    return _bind


async def _mint_project_with_snapshot(
    client, headers, user, db_session, bind_store, *, name="App"
):
    store = bind_store(FakeStorage())
    created = (
        await client.post(
            "/v1/projects",
            headers=headers,
            json={"name": name, "description": _VALID_DESCRIPTION},
        )
    ).json()
    project_id = created["id"]
    app_id = await resolve_app_for_project(db_session, user.id, uuid.UUID(project_id))
    await db_session.commit()
    await store.put(snapshot_key(app_id), b"BUNDLE")
    return project_id


# --- POST /{project_id}:share ---------------------------------------------------


async def test_share_requires_auth_401(client) -> None:
    resp = await client.post(
        f"/v1/projects/{uuid.uuid4()}:share",
        json={"sharedWithUserId": str(uuid.uuid4())},
    )
    assert resp.status_code == 401


async def test_share_succeeds_and_returns_the_recipient(client, db_session, bind_store) -> None:
    headers, owner = await _auth(db_session)
    project_id = await _mint_project_with_snapshot(client, headers, owner, db_session, bind_store)
    colleague = await UserFactory.create(
        db_session, email="colleague@example.com", display_name="Colleague Name"
    )
    await db_session.commit()

    resp = await client.post(
        f"/v1/projects/{project_id}:share",
        headers=headers,
        json={"sharedWithUserId": str(colleague.id)},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["sharedWithUserId"] == str(colleague.id)
    assert body["sharedWithDisplayName"] == "Colleague Name"
    assert body["sharedWithEmailLocalPart"] == "colleague"


async def test_share_refuses_self_share(client, db_session, bind_store) -> None:
    headers, owner = await _auth(db_session)
    project_id = await _mint_project_with_snapshot(client, headers, owner, db_session, bind_store)

    resp = await client.post(
        f"/v1/projects/{project_id}:share",
        headers=headers,
        json={"sharedWithUserId": str(owner.id)},
    )
    assert resp.status_code == 400


async def test_share_refuses_a_project_with_nothing_saved(client, db_session) -> None:
    headers, owner = await _auth(db_session)
    created = (
        await client.post(
            "/v1/projects",
            headers=headers,
            json={"name": "Unsaved", "description": _VALID_DESCRIPTION},
        )
    ).json()
    colleague = await UserFactory.create(db_session, email="colleague@example.com")
    await db_session.commit()

    resp = await client.post(
        f"/v1/projects/{created['id']}:share",
        headers=headers,
        json={"sharedWithUserId": str(colleague.id)},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "no_saved_snapshot"


async def test_share_404s_for_an_unknown_colleague(client, db_session, bind_store) -> None:
    headers, owner = await _auth(db_session)
    project_id = await _mint_project_with_snapshot(client, headers, owner, db_session, bind_store)

    resp = await client.post(
        f"/v1/projects/{project_id}:share",
        headers=headers,
        json={"sharedWithUserId": str(uuid.uuid4())},
    )
    assert resp.status_code == 404


async def test_share_404s_for_someone_else_s_project(client, db_session, bind_store) -> None:
    owner_headers, owner = await _auth(db_session)
    project_id = await _mint_project_with_snapshot(
        client, owner_headers, owner, db_session, bind_store
    )
    stranger_headers, _ = await _auth(db_session)
    colleague = await UserFactory.create(db_session, email="colleague@example.com")
    await db_session.commit()

    resp = await client.post(
        f"/v1/projects/{project_id}:share",
        headers=stranger_headers,
        json={"sharedWithUserId": str(colleague.id)},
    )
    assert resp.status_code == 404


async def test_re_sharing_is_idempotent_over_http(client, db_session, bind_store) -> None:
    headers, owner = await _auth(db_session)
    project_id = await _mint_project_with_snapshot(client, headers, owner, db_session, bind_store)
    colleague = await UserFactory.create(db_session, email="colleague@example.com")
    await db_session.commit()

    first = await client.post(
        f"/v1/projects/{project_id}:share",
        headers=headers,
        json={"sharedWithUserId": str(colleague.id)},
    )
    second = await client.post(
        f"/v1/projects/{project_id}:share",
        headers=headers,
        json={"sharedWithUserId": str(colleague.id)},
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]

    shares = await client.get(f"/v1/projects/{project_id}/shares", headers=headers)
    assert len(shares.json()["shares"]) == 1


# --- POST /{project_id}:unshare -------------------------------------------------


async def test_unshare_requires_auth_401(client) -> None:
    resp = await client.post(
        f"/v1/projects/{uuid.uuid4()}:unshare",
        json={"sharedWithUserId": str(uuid.uuid4())},
    )
    assert resp.status_code == 401


async def test_unshare_removes_the_share_and_is_idempotent(client, db_session, bind_store) -> None:
    headers, owner = await _auth(db_session)
    project_id = await _mint_project_with_snapshot(client, headers, owner, db_session, bind_store)
    colleague = await UserFactory.create(db_session, email="colleague@example.com")
    await db_session.commit()
    await client.post(
        f"/v1/projects/{project_id}:share",
        headers=headers,
        json={"sharedWithUserId": str(colleague.id)},
    )

    first = await client.post(
        f"/v1/projects/{project_id}:unshare",
        headers=headers,
        json={"sharedWithUserId": str(colleague.id)},
    )
    second = await client.post(
        f"/v1/projects/{project_id}:unshare",
        headers=headers,
        json={"sharedWithUserId": str(colleague.id)},
    )
    assert first.status_code == 200
    assert first.json() == {"ok": True}
    assert second.status_code == 200  # a double-click/retry, not an error
    assert second.json() == {"ok": True}

    shares = await client.get(f"/v1/projects/{project_id}/shares", headers=headers)
    assert shares.json()["shares"] == []


async def test_unshare_404s_for_someone_else_s_project(client, db_session, bind_store) -> None:
    owner_headers, owner = await _auth(db_session)
    project_id = await _mint_project_with_snapshot(
        client, owner_headers, owner, db_session, bind_store
    )
    stranger_headers, _ = await _auth(db_session)

    resp = await client.post(
        f"/v1/projects/{project_id}:unshare",
        headers=stranger_headers,
        json={"sharedWithUserId": str(owner.id)},
    )
    assert resp.status_code == 404


@pytest.fixture
def wired_sandbox(app, db_session):
    """#198 R25 — `unshare_project`'s teardown seam needs BOTH `OptionalSandbox` and
    `SessionManagerDep` wired to fakes, mirroring `tests/api/v1/build_sessions/conftest.py`'s
    own `wire` fixture: a manager bound to the ROLLED-BACK test session (never a real commit
    outside it) plus one `FakeSandboxClient` both DI seams share, since some routes take the
    raising dependency and others the None-tolerant one."""

    @contextlib.asynccontextmanager
    async def _session():
        yield db_session

    manager = SessionManager(session_factory=lambda: _session())
    sbx = FakeSandboxClient()
    app.dependency_overrides[session_manager_dependency] = lambda: manager
    app.dependency_overrides[sandbox_dependency] = lambda: sbx
    app.dependency_overrides[sandbox_or_none_dependency] = lambda: sbx
    return manager, sbx


async def test_unshare_tears_down_the_colleagues_live_container(
    client, db_session, bind_store, wired_sandbox, _sandbox_configured, fake_redis
) -> None:
    """#198 R25 — Slice 1's teardown seam, filled in: revoking access tears down the
    recipient's live view of the project, not merely the membership row."""
    manager, sbx = wired_sandbox
    headers, owner = await _auth(db_session)
    project_id = await _mint_project_with_snapshot(client, headers, owner, db_session, bind_store)
    colleague = await UserFactory.create(db_session, email="colleague@example.com")
    await db_session.commit()
    await client.post(
        f"/v1/projects/{project_id}:share",
        headers=headers,
        json={"sharedWithUserId": str(colleague.id)},
    )
    app_id = await resolve_app_for_project(db_session, owner.id, uuid.UUID(project_id))
    await db_session.commit()
    project = await db_session.get(Project, uuid.UUID(project_id))
    launched = await manager.launch_shared_preview(db_session, colleague, project, sbx)
    assert launched.app_id == app_id
    shared_name = shr_name_for(app_id, colleague.id)
    assert shared_name in sbx.restored

    resp = await client.post(
        f"/v1/projects/{project_id}:unshare",
        headers=headers,
        json={"sharedWithUserId": str(colleague.id)},
    )

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert shared_name in sbx.torn_down


# --- GET /{project_id}/shares ---------------------------------------------------


async def test_list_shares_is_owner_only(client, db_session, bind_store) -> None:
    owner_headers, owner = await _auth(db_session)
    project_id = await _mint_project_with_snapshot(
        client, owner_headers, owner, db_session, bind_store
    )
    colleague = await UserFactory.create(
        db_session, email="colleague@example.com", display_name="Colleague"
    )
    await db_session.commit()
    await client.post(
        f"/v1/projects/{project_id}:share",
        headers=owner_headers,
        json={"sharedWithUserId": str(colleague.id)},
    )

    owner_view = await client.get(f"/v1/projects/{project_id}/shares", headers=owner_headers)
    assert owner_view.status_code == 200
    assert len(owner_view.json()["shares"]) == 1

    stranger_headers, _ = await _auth(db_session)
    stranger_view = await client.get(f"/v1/projects/{project_id}/shares", headers=stranger_headers)
    assert stranger_view.status_code == 404


# --- GET /{project_id} — the one relaxed read (R11) -----------------------------


async def test_get_project_reports_owner_access_for_the_owner(
    client, db_session, bind_store
) -> None:
    headers, owner = await _auth(db_session)
    project_id = await _mint_project_with_snapshot(client, headers, owner, db_session, bind_store)

    resp = await client.get(f"/v1/projects/{project_id}", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["access"] == "owner"


async def test_get_project_reports_shared_access_for_a_recipient(
    client, db_session, bind_store
) -> None:
    owner_headers, owner = await _auth(db_session)
    project_id = await _mint_project_with_snapshot(
        client, owner_headers, owner, db_session, bind_store
    )
    recipient_headers, recipient = await _auth(db_session)
    await client.post(
        f"/v1/projects/{project_id}:share",
        headers=owner_headers,
        json={"sharedWithUserId": str(recipient.id)},
    )

    resp = await client.get(f"/v1/projects/{project_id}", headers=recipient_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["access"] == "shared"
    assert body["hasSavedSnapshot"] is True  # R10's second sentence: Launch is not disabled


async def test_get_project_reports_no_saved_snapshot_for_a_recipient_when_it_vanishes(
    client, db_session, bind_store
) -> None:
    """R10's second sentence, the disable side: an existing share whose bundle is gone reports
    `hasSavedSnapshot: false` so the restricted workspace can disable Launch and explain why,
    rather than letting a recipient press it into a failure the API already knows about."""
    owner_headers, owner = await _auth(db_session)
    store = bind_store(FakeStorage())
    created = (
        await client.post(
            "/v1/projects",
            headers=owner_headers,
            json={"name": "App", "description": _VALID_DESCRIPTION},
        )
    ).json()
    project_id = created["id"]
    app_id = await resolve_app_for_project(db_session, owner.id, uuid.UUID(project_id))
    await db_session.commit()
    await store.put(snapshot_key(app_id), b"BUNDLE")
    recipient_headers, recipient = await _auth(db_session)
    await client.post(
        f"/v1/projects/{project_id}:share",
        headers=owner_headers,
        json={"sharedWithUserId": str(recipient.id)},
    )
    await store.delete(snapshot_key(app_id))  # the owner's bundle is gone AFTER the share

    resp = await client.get(f"/v1/projects/{project_id}", headers=recipient_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["access"] == "shared"
    assert body["hasSavedSnapshot"] is False


async def test_get_project_reports_no_saved_snapshot_field_for_an_owner(
    client, db_session, bind_store
) -> None:
    """The field is scoped to a SHARED viewer — an owner reads `hasRelaunchableSnapshot`
    instead, and paying for a second object-store HEAD on the far more common owner page load,
    for a field nothing on that path consults, would be pure cost."""
    headers, owner = await _auth(db_session)
    project_id = await _mint_project_with_snapshot(client, headers, owner, db_session, bind_store)

    resp = await client.get(f"/v1/projects/{project_id}", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["hasSavedSnapshot"] is None


async def test_get_project_404s_for_a_stranger_not_shared_with(
    client, db_session, bind_store
) -> None:
    owner_headers, owner = await _auth(db_session)
    project_id = await _mint_project_with_snapshot(
        client, owner_headers, owner, db_session, bind_store
    )
    stranger_headers, _ = await _auth(db_session)

    resp = await client.get(f"/v1/projects/{project_id}", headers=stranger_headers)
    assert resp.status_code == 404


# --- Cross-user isolation: a share widens READS only, never mutations (R6/R11) -


async def test_a_share_recipient_cannot_patch_the_project(client, db_session, bind_store) -> None:
    owner_headers, owner = await _auth(db_session)
    project_id = await _mint_project_with_snapshot(
        client, owner_headers, owner, db_session, bind_store
    )
    recipient_headers, recipient = await _auth(db_session)
    await client.post(
        f"/v1/projects/{project_id}:share",
        headers=owner_headers,
        json={"sharedWithUserId": str(recipient.id)},
    )

    resp = await client.patch(
        f"/v1/projects/{project_id}", headers=recipient_headers, json={"name": "Hijacked"}
    )
    assert resp.status_code == 404


async def test_a_share_recipient_cannot_delete_the_project(client, db_session, bind_store) -> None:
    owner_headers, owner = await _auth(db_session)
    project_id = await _mint_project_with_snapshot(
        client, owner_headers, owner, db_session, bind_store
    )
    recipient_headers, recipient = await _auth(db_session)
    await client.post(
        f"/v1/projects/{project_id}:share",
        headers=owner_headers,
        json={"sharedWithUserId": str(recipient.id)},
    )

    resp = await client.request(
        "DELETE",
        f"/v1/projects/{project_id}",
        headers=recipient_headers,
        json=DELETE_BODY,
    )
    assert resp.status_code == 404


async def test_a_share_recipient_cannot_share_the_project_onward(
    client, db_session, bind_store
) -> None:
    owner_headers, owner = await _auth(db_session)
    project_id = await _mint_project_with_snapshot(
        client, owner_headers, owner, db_session, bind_store
    )
    recipient_headers, recipient = await _auth(db_session)
    await client.post(
        f"/v1/projects/{project_id}:share",
        headers=owner_headers,
        json={"sharedWithUserId": str(recipient.id)},
    )
    third_party = await UserFactory.create(db_session, email="third@example.com")
    await db_session.commit()

    resp = await client.post(
        f"/v1/projects/{project_id}:share",
        headers=recipient_headers,
        json={"sharedWithUserId": str(third_party.id)},
    )
    assert resp.status_code == 404


# --- GET /colleagues -------------------------------------------------------------


async def test_colleague_search_requires_auth_401(client) -> None:
    resp = await client.get("/v1/projects/colleagues", params={"q": "abc"})
    assert resp.status_code == 401


async def test_colleague_search_rejects_a_too_short_query(client, db_session) -> None:
    headers, _ = await _auth(db_session)
    resp = await client.get("/v1/projects/colleagues", headers=headers, params={"q": "ab"})
    assert resp.status_code == 422


async def test_colleague_search_finds_a_matching_colleague(client, db_session) -> None:
    headers, _ = await _auth(db_session)
    await UserFactory.create(db_session, email="priya@example.com", display_name="Priya Nair")
    await db_session.commit()

    resp = await client.get("/v1/projects/colleagues", headers=headers, params={"q": "Pri"})
    assert resp.status_code == 200
    names = [c["displayName"] for c in resp.json()["colleagues"]]
    assert "Priya Nair" in names


async def test_colleague_search_never_returns_the_requester(client, db_session) -> None:
    headers, user = await _auth(db_session)
    resp = await client.get(
        "/v1/projects/colleagues", headers=headers, params={"q": user.email[:3]}
    )
    assert resp.status_code == 200
    ids = [c["id"] for c in resp.json()["colleagues"]]
    assert str(user.id) not in ids


async def test_colleague_search_rate_limit_enforced(client, db_session) -> None:
    from src.api.v1.projects.router import COLLEAGUE_SEARCH_RATE_LIMIT

    headers, _ = await _auth(db_session)
    for _ in range(COLLEAGUE_SEARCH_RATE_LIMIT):
        resp = await client.get("/v1/projects/colleagues", headers=headers, params={"q": "abc"})
        assert resp.status_code == 200
    blocked = await client.get("/v1/projects/colleagues", headers=headers, params={"q": "abc"})
    assert blocked.status_code == 429


# --- GET /shared -----------------------------------------------------------------


async def test_shared_with_me_requires_auth_401(client) -> None:
    resp = await client.get("/v1/projects/shared")
    assert resp.status_code == 401


async def test_shared_with_me_lists_only_what_was_shared_with_this_caller(
    client, db_session, bind_store
) -> None:
    owner_headers, owner = await _auth(db_session)
    project_id = await _mint_project_with_snapshot(
        client, owner_headers, owner, db_session, bind_store, name="Shared App"
    )
    recipient_headers, recipient = await _auth(db_session)
    await client.post(
        f"/v1/projects/{project_id}:share",
        headers=owner_headers,
        json={"sharedWithUserId": str(recipient.id)},
    )
    stranger_headers, _ = await _auth(db_session)

    recipient_view = await client.get("/v1/projects/shared", headers=recipient_headers)
    assert recipient_view.status_code == 200
    items = recipient_view.json()["items"]
    assert len(items) == 1
    assert items[0]["projectName"] == "Shared App"
    assert items[0]["projectId"] == project_id

    stranger_view = await client.get("/v1/projects/shared", headers=stranger_headers)
    assert stranger_view.status_code == 200
    assert stranger_view.json()["items"] == []
