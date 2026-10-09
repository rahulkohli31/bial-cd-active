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
from datetime import UTC, datetime

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.v1.build_sessions.deps import (
    sandbox_dependency,
    sandbox_or_none_dependency,
    session_manager_dependency,
)
from src.config import settings
from src.db.models.app_registry import AppRegistry
from src.db.models.audit import AuditLog
from src.db.models.deployment import Deployment, DeploymentStatus
from src.db.models.project import Project
from src.db.models.user import User
from src.db.session import get_db
from src.services.build_sessions import SessionManager
from src.services.build_sessions.appdata import resolve_app_for_project
from src.services.directory import client as directory_client
from src.services.directory import is_directory_member
from src.services.projects.shares import revoke_share
from src.services.redis import registry_key
from src.services.redis.keys import REGISTRY_FIELD_APP_NAME
from src.services.sandbox.config import SandboxConfig
from src.services.storage import accessor as storage_accessor
from src.services.storage import snapshot_key
from tests.api.v1.projects.conftest import _VALID_DESCRIPTION, DELETE_BODY
from tests.api.v1.projects.test_projects_crud import _auth
from tests.factories import ProjectFactory, ProjectShareFactory, UserFactory
from tests.fakes import (
    FakeDirectory,
    FakeSandboxClient,
    FakeStorage,
    a_name_unrelated_to_its_app,
)


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
    assert body["signedIn"] is True


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


# --- POST /{project_id}:share with a directory pick ------------------------------


@pytest.fixture
def request_sessions(app, db_session) -> None:
    """Each request gets its own savepoint session, closed uncommitted on a refusal the way
    `get_db` closes one, so what a refused share wrote is undone here as it is in production."""

    async def _request_session():
        async with AsyncSession(
            bind=db_session.bind, expire_on_commit=False, join_transaction_mode="create_savepoint"
        ) as session:
            yield session

    app.dependency_overrides[get_db] = _request_session


async def _member_auth(db_session, fake_directory: FakeDirectory):
    """`_auth` for a caller the directory holds as a member, as a directory pick requires."""
    headers, user = await _auth(db_session)
    user.azure_oid = str(fake_directory.add_user("Owner Member", mail="owner.member@bial.example"))
    await db_session.flush()
    return headers, user


def _paths_asked(fake_directory: FakeDirectory) -> list[str]:
    return [request.url.path for request in fake_directory.requests]


async def _users_keyed_to(db_session, directory_id: uuid.UUID) -> list[User]:
    rows = await db_session.scalars(select(User).where(User.azure_oid == str(directory_id)))
    return list(rows.all())


async def _audit_trail(db_session, actor_id: uuid.UUID) -> list[tuple[str, str, str | None, dict]]:
    rows = await db_session.scalars(
        select(AuditLog).where(AuditLog.actor_id == actor_id).order_by(AuditLog.id)
    )
    return [(row.action, row.resource_type, row.resource_id, row.detail) for row in rows]


async def test_sharing_with_a_directory_person_creates_them_not_yet_signed_in(
    client, db_session, bind_store, fake_directory: FakeDirectory
) -> None:
    headers, owner = await _member_auth(db_session, fake_directory)
    project_id = await _mint_project_with_snapshot(client, headers, owner, db_session, bind_store)
    directory_id = fake_directory.add_user(
        "Priya Raman", mail="priya.raman@bial.example", upn="p.raman@bial.example"
    )

    first = await client.post(
        f"/v1/projects/{project_id}:share",
        headers=headers,
        json={"directoryId": str(directory_id)},
    )
    again = await client.post(
        f"/v1/projects/{project_id}:share",
        headers=headers,
        json={"directoryId": str(directory_id)},
    )

    assert first.status_code == 200, first.text
    [created] = await _users_keyed_to(db_session, directory_id)
    assert (created.email, created.upn, created.display_name, created.has_signed_in) == (
        "priya.raman@bial.example",
        "p.raman@bial.example",
        "Priya Raman",
        False,
    )
    body = first.json()
    assert body["sharedWithUserId"] == str(created.id)
    assert body["sharedWithDisplayName"] == "Priya Raman"
    assert body["sharedWithEmailLocalPart"] == "priya.raman"
    assert body["signedIn"] is False
    assert again.status_code == 200
    assert again.json()["id"] == body["id"]
    assert _paths_asked(fake_directory) == [
        f"/v1.0/users/{owner.azure_oid}",
        f"/v1.0/users/{directory_id}",
    ]
    assert await _audit_trail(db_session, owner.id) == [
        ("user:directory_create", "user", str(created.id), None),
        (
            "project:share_create",
            "project",
            project_id,
            {"sharedWithUserId": str(created.id), "appStatus": "draft"},
        ),
    ]


async def test_a_sign_in_that_lands_first_keeps_its_own_row(
    client, db_session, bind_store, fake_directory: FakeDirectory, monkeypatch
) -> None:
    headers, owner = await _member_auth(db_session, fake_directory)
    project_id = await _mint_project_with_snapshot(client, headers, owner, db_session, bind_store)
    directory_id = fake_directory.add_user("Priya Raman", mail="priya.raman@bial.example")
    signed_in: list[User] = []

    async def _sign_in_while_graph_answers(url, params, graph_headers):
        if url.endswith(str(directory_id)):
            signed_in.append(
                await UserFactory.create(
                    db_session,
                    azure_oid=str(directory_id),
                    email="priya@bial.example",
                    display_name="Priya (signed in)",
                    token_version=4,
                )
            )
        return await fake_directory(url, params, graph_headers)

    monkeypatch.setattr(directory_client, "_graph_get", _sign_in_while_graph_answers)

    resp = await client.post(
        f"/v1/projects/{project_id}:share",
        headers=headers,
        json={"directoryId": str(directory_id)},
    )

    assert resp.status_code == 200, resp.text
    [racer] = signed_in
    assert resp.json()["sharedWithUserId"] == str(racer.id)
    assert resp.json()["signedIn"] is True
    [row] = await _users_keyed_to(db_session, directory_id)
    await db_session.refresh(row)
    assert (row.id, row.email, row.display_name, row.token_version, row.has_signed_in) == (
        racer.id,
        "priya@bial.example",
        "Priya (signed in)",
        4,
        True,
    )
    assert [action for action, *_ in await _audit_trail(db_session, owner.id)] == [
        "project:share_create"
    ]


async def test_a_directory_pick_already_keyed_to_a_user_asks_the_directory_only_about_the_owner(
    client, db_session, bind_store, fake_directory: FakeDirectory
) -> None:
    headers, owner = await _member_auth(db_session, fake_directory)
    project_id = await _mint_project_with_snapshot(client, headers, owner, db_session, bind_store)
    directory_id = uuid.uuid4()
    known = await UserFactory.create(db_session, azure_oid=str(directory_id))

    resp = await client.post(
        f"/v1/projects/{project_id}:share",
        headers=headers,
        json={"directoryId": str(directory_id)},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["sharedWithUserId"] == str(known.id)
    assert _paths_asked(fake_directory) == [f"/v1.0/users/{owner.azure_oid}"]


async def test_a_refused_directory_share_leaves_no_user_behind(
    client, db_session, fake_directory: FakeDirectory, request_sessions
) -> None:
    headers, owner = await _member_auth(db_session, fake_directory)
    project = await ProjectFactory.create(db_session, owner.id)
    directory_id = fake_directory.add_user()

    resp = await client.post(
        f"/v1/projects/{project.id}:share",
        headers=headers,
        json={"directoryId": str(directory_id)},
    )

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "no_saved_snapshot"
    assert _paths_asked(fake_directory) == [
        f"/v1.0/users/{owner.azure_oid}",
        f"/v1.0/users/{directory_id}",
    ]
    assert await _users_keyed_to(db_session, directory_id) == []
    assert await _audit_trail(db_session, owner.id) == []


async def test_a_directory_that_cannot_be_reached_refuses_the_pick_with_a_503(
    client, db_session, bind_store, fake_directory: FakeDirectory
) -> None:
    headers, owner = await _member_auth(db_session, fake_directory)
    project_id = await _mint_project_with_snapshot(client, headers, owner, db_session, bind_store)
    directory_id = fake_directory.add_user()
    # The owner's membership was settled by an earlier search, before the directory went down.
    assert await is_directory_member(owner.azure_oid) is True
    fake_directory.unavailable = True

    resp = await client.post(
        f"/v1/projects/{project_id}:share",
        headers=headers,
        json={"directoryId": str(directory_id)},
    )

    assert resp.status_code == 503
    assert resp.json()["error"]["message"] == (
        "Couldn't look this person up right now. Try again in a moment."
    )
    assert _paths_asked(fake_directory) == [
        f"/v1.0/users/{owner.azure_oid}",
        f"/v1.0/users/{directory_id}",
    ]
    assert await _users_keyed_to(db_session, directory_id) == []
    shares = await client.get(f"/v1/projects/{project_id}/shares", headers=headers)
    assert shares.json()["shares"] == []


@pytest.mark.parametrize("who", ["absent", "guest"])
async def test_a_directory_pick_nobody_can_be_shared_with_is_a_404(
    client, db_session, bind_store, fake_directory: FakeDirectory, who: str
) -> None:
    headers, owner = await _member_auth(db_session, fake_directory)
    project_id = await _mint_project_with_snapshot(client, headers, owner, db_session, bind_store)
    directory_id = (
        uuid.uuid4()
        if who == "absent"
        else fake_directory.add_user(
            "Guest Person", mail="guest@partner.example", upn="guest_partner#EXT#@bial.example"
        )
    )

    resp = await client.post(
        f"/v1/projects/{project_id}:share",
        headers=headers,
        json={"directoryId": str(directory_id)},
    )

    assert resp.status_code == 404
    assert resp.json()["error"]["message"] == "That colleague could not be found."
    assert _paths_asked(fake_directory) == [
        f"/v1.0/users/{owner.azure_oid}",
        f"/v1.0/users/{directory_id}",
    ]
    assert await _users_keyed_to(db_session, directory_id) == []


@pytest.mark.parametrize("owner_is", ["guest", "absent", "unconfirmed"])
async def test_an_owner_who_is_not_a_directory_member_cannot_pick_from_the_directory(
    client, db_session, bind_store, fake_directory: FakeDirectory, owner_is: str
) -> None:
    """The directory is for BIAL's own people: a guest must not reach it through a share."""
    headers, owner = await _auth(db_session)
    if owner_is == "guest":
        owner.azure_oid = str(
            fake_directory.add_user(
                "Guest Owner",
                mail="owner@partner.example",
                upn="owner_partner.example#EXT#@bial.onmicrosoft.com",
            )
        )
    else:
        owner.azure_oid = str(uuid.uuid4())
    await db_session.flush()
    project_id = await _mint_project_with_snapshot(client, headers, owner, db_session, bind_store)
    directory_id = fake_directory.add_user()
    fake_directory.unavailable = owner_is == "unconfirmed"

    resp = await client.post(
        f"/v1/projects/{project_id}:share",
        headers=headers,
        json={"directoryId": str(directory_id)},
    )

    assert resp.status_code == 404
    assert resp.json()["error"]["message"] == "That colleague could not be found."
    assert _paths_asked(fake_directory) == [f"/v1.0/users/{owner.azure_oid}"]
    assert await _users_keyed_to(db_session, directory_id) == []
    assert await _audit_trail(db_session, owner.id) == []


_NOT_EXACTLY_ONE = {
    "type": "value_error",
    "loc": ["body"],
    "msg": "Choose exactly one colleague to share with.",
}


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {"sharedWithUserId": str(uuid.uuid4()), "directoryId": str(uuid.uuid4())},
            _NOT_EXACTLY_ONE,
        ),
        ({}, _NOT_EXACTLY_ONE),
        ({"directoryId": "not-a-uuid"}, {"type": "uuid_parsing", "loc": ["body", "directoryId"]}),
    ],
    ids=["both", "neither", "directory-id-not-a-uuid"],
)
async def test_a_share_must_name_exactly_one_valid_colleague(
    client, db_session, fake_directory: FakeDirectory, payload: dict, expected: dict
) -> None:
    headers, owner = await _auth(db_session)
    project = await ProjectFactory.create(db_session, owner.id)

    resp = await client.post(f"/v1/projects/{project.id}:share", headers=headers, json=payload)

    assert resp.status_code == 422
    [error] = resp.json()["detail"]
    assert {key: error[key] for key in expected} == expected
    assert fake_directory.requests == []


async def test_a_directory_pick_on_someone_else_s_project_asks_no_directory(
    client, db_session, bind_store, fake_directory: FakeDirectory
) -> None:
    owner_headers, owner = await _auth(db_session)
    project_id = await _mint_project_with_snapshot(
        client, owner_headers, owner, db_session, bind_store
    )
    stranger_headers, _ = await _member_auth(db_session, fake_directory)
    directory_id = fake_directory.add_user()

    resp = await client.post(
        f"/v1/projects/{project_id}:share",
        headers=stranger_headers,
        json={"directoryId": str(directory_id)},
    )

    assert resp.status_code == 404
    assert fake_directory.requests == []
    assert await _users_keyed_to(db_session, directory_id) == []


async def test_share_rate_limit_enforced(client, db_session) -> None:
    headers, _ = await _auth(db_session)
    body = {"sharedWithUserId": str(uuid.uuid4())}
    for _ in range(30):
        resp = await client.post(f"/v1/projects/{uuid.uuid4()}:share", headers=headers, json=body)
        assert resp.status_code == 404
    blocked = await client.post(f"/v1/projects/{uuid.uuid4()}:share", headers=headers, json=body)
    assert blocked.status_code == 429
    assert blocked.json()["error"]["message"] == (
        "Too many shares. Please wait a moment and try again."
    )


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
    [shared_name] = sbx.restored

    resp = await client.post(
        f"/v1/projects/{project_id}:unshare",
        headers=headers,
        json={"sharedWithUserId": str(colleague.id)},
    )

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert shared_name in sbx.torn_down


async def _a_colleagues_view_named_like_a_build_sandbox(
    client, db_session, bind_store, manager, sbx, fake_redis
) -> tuple[dict[str, str], str, User, str]:
    """A shared, launched view whose container carries a name unrelated to its app."""
    headers, owner = await _auth(db_session)
    project_id = await _mint_project_with_snapshot(client, headers, owner, db_session, bind_store)
    colleague = await UserFactory.create(db_session, email="colleague@example.com")
    await db_session.commit()
    await client.post(
        f"/v1/projects/{project_id}:share",
        headers=headers,
        json={"sharedWithUserId": str(colleague.id)},
    )
    project = await db_session.get(Project, uuid.UUID(project_id))
    await manager.launch_shared_preview(db_session, colleague, project, sbx)
    name = a_name_unrelated_to_its_app()
    await fake_redis.hset(registry_key(colleague.id), REGISTRY_FIELD_APP_NAME, name)
    return headers, project_id, colleague, name


async def test_unshare_tears_down_a_colleagues_view_named_like_a_build_sandbox(
    client, db_session, bind_store, wired_sandbox, _sandbox_configured, fake_redis
) -> None:
    """★ A missed revoke leaves a removed colleague a running copy of the owner's app.

    Mutation check: hand the revoke any other project's stamp and the view keeps running."""
    manager, sbx = wired_sandbox
    headers, project_id, colleague, name = await _a_colleagues_view_named_like_a_build_sandbox(
        client, db_session, bind_store, manager, sbx, fake_redis
    )

    resp = await client.post(
        f"/v1/projects/{project_id}:unshare",
        headers=headers,
        json={"sharedWithUserId": str(colleague.id)},
    )

    assert resp.status_code == 200
    assert sbx.torn_down == [name]


async def test_deleting_a_project_tears_down_a_colleagues_view_named_like_a_build_sandbox(
    client, db_session, bind_store, wired_sandbox, _sandbox_configured, fake_redis
) -> None:
    """Mutation check: look the view up by any other stamp and it outlives the project."""
    manager, sbx = wired_sandbox
    headers, project_id, colleague, name = await _a_colleagues_view_named_like_a_build_sandbox(
        client, db_session, bind_store, manager, sbx, fake_redis
    )

    resp = await client.request(
        "DELETE", f"/v1/projects/{project_id}", headers=headers, json=DELETE_BODY
    )

    assert resp.status_code == 200, resp.text
    assert sbx.torn_down == [name]
    assert await fake_redis.exists(registry_key(colleague.id)) == 0


async def test_a_colleagues_view_nobody_could_read_is_recorded_by_its_app_and_colleague(
    client, db_session, bind_store, wired_sandbox, _sandbox_configured, fake_redis, monkeypatch
) -> None:
    """★ With no record to name it, the view is found in ARM by the app it runs AND the colleague
    holding it: the app alone matches every colleague's view and the owner's own container.

    Mutation check: record the bare app id and the survivor no longer says whose view it is."""
    import importlib

    import structlog.testing
    from redis.exceptions import RedisError

    from src.core.alarms import TEARDOWN_ARTEFACT_SURVIVED_EVENT

    manager, sbx = wired_sandbox
    headers, project_id, colleague, _name = await _a_colleagues_view_named_like_a_build_sandbox(
        client, db_session, bind_store, manager, sbx, fake_redis
    )
    owner_app = await db_session.scalar(
        select(AppRegistry.id).where(AppRegistry.project_id == uuid.UUID(project_id))
    )

    async def _unreadable(*args, **kwargs):
        raise RedisError("the registry will not answer")

    monkeypatch.setattr(
        importlib.import_module("src.api.v1.projects.router"), "read_registry", _unreadable
    )

    with structlog.testing.capture_logs() as captured:
        resp = await client.request(
            "DELETE", f"/v1/projects/{project_id}", headers=headers, json=DELETE_BODY
        )

    assert resp.status_code == 200, resp.text
    assert [
        entry["artefact_id"]
        for entry in captured
        if entry.get("event") == TEARDOWN_ARTEFACT_SURVIVED_EVENT
        and entry.get("artefact") == "shared_sandbox_container"
    ] == [f"{owner_app}/{colleague.id}"]
    record = await db_session.scalar(
        select(AuditLog).where(
            AuditLog.action == "project:teardown-incomplete", AuditLog.resource_id == project_id
        )
    )
    assert record is not None and record.detail is not None
    assert {"artefact": "shared_sandbox_container", "id": f"{owner_app}/{colleague.id}"} in (
        record.detail["survived"]
    )


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


async def test_the_share_list_says_which_colleagues_have_never_signed_in(
    client, db_session
) -> None:
    owner_headers, owner = await _auth(db_session)
    project = await ProjectFactory.create(db_session, owner.id)
    signed_in = await UserFactory.create(db_session, email="signed.in@example.com")
    never = await UserFactory.create(
        db_session, email="never.signed.in@example.com", has_signed_in=False
    )
    await ProjectShareFactory.create(db_session, project.id, signed_in.id)
    await ProjectShareFactory.create(db_session, project.id, never.id)

    resp = await client.get(f"/v1/projects/{project.id}/shares", headers=owner_headers)

    assert resp.status_code == 200
    rows = resp.json()["shares"]
    assert {row["sharedWithEmailLocalPart"]: row["signedIn"] for row in rows} == {
        "signed.in": True,
        "never.signed.in": False,
    }


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


async def test_get_project_reports_publishing_from_the_owners_app_for_a_recipient(
    client, db_session, bind_store
) -> None:
    """The recipient has no app of their own, so a wrongly scoped read answers false here."""
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
    db_session.add(
        Deployment(
            app_id=app_id,
            user_id=owner.id,
            status=DeploymentStatus.RUNNING,
            image_digest="sha256:" + "cd" * 32,
            url=None,
        )
    )
    await db_session.commit()
    await store.put(snapshot_key(app_id), b"BUNDLE")
    recipient_headers, recipient = await _auth(db_session)
    shared = await client.post(
        f"/v1/projects/{project_id}:share",
        headers=owner_headers,
        json={"sharedWithUserId": str(recipient.id)},
    )
    assert shared.status_code < 300, shared.text

    resp = await client.get(f"/v1/projects/{project_id}", headers=recipient_headers)

    assert resp.status_code == 200, resp.text
    assert resp.json()["access"] == "shared"
    assert resp.json()["isPublishing"] is True


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


async def test_colleague_search_fills_the_page_from_the_directory_after_our_own_users(
    client, db_session, fake_directory: FakeDirectory
) -> None:
    headers, _ = await _member_auth(db_session, fake_directory)
    ada = await UserFactory.create(
        db_session, email="quill.ada@example.com", display_name="Quill Ada"
    )
    never_signed_in = await UserFactory.create(
        db_session, email="quill.bo@example.com", display_name="Quill Bo", has_signed_in=False
    )
    newcomers = [
        fake_directory.add_user(f"Quill New {n}", mail=f"quill.new{n}@bial.example")
        for n in range(3)
    ]

    resp = await client.get("/v1/projects/colleagues", headers=headers, params={"q": "Quill"})

    assert resp.status_code == 200
    assert resp.json()["colleagues"] == [
        {
            "id": str(ada.id),
            "directoryId": None,
            "displayName": "Quill Ada",
            "emailLocalPart": "quill.ada",
            "signedIn": True,
        },
        {
            "id": str(never_signed_in.id),
            "directoryId": None,
            "displayName": "Quill Bo",
            "emailLocalPart": "quill.bo",
            "signedIn": False,
        },
        *(
            {
                "id": None,
                "directoryId": str(oid),
                "displayName": f"Quill New {n}",
                "emailLocalPart": f"quill.new{n}",
                "signedIn": False,
            }
            for n, oid in enumerate(newcomers)
        ),
    ]


async def test_colleague_search_answers_with_our_own_users_when_the_directory_is_down(
    client, db_session, fake_directory: FakeDirectory
) -> None:
    headers, user = await _member_auth(db_session, fake_directory)
    ada = await UserFactory.create(
        db_session, email="quill.ada@example.com", display_name="Quill Ada"
    )
    fake_directory.add_user("Quill New", mail="quill.new@bial.example")
    fake_directory.unavailable = True

    resp = await client.get("/v1/projects/colleagues", headers=headers, params={"q": "Quill"})

    assert resp.status_code == 200
    assert resp.json()["colleagues"] == [
        {
            "id": str(ada.id),
            "directoryId": None,
            "displayName": "Quill Ada",
            "emailLocalPart": "quill.ada",
            "signedIn": True,
        }
    ]
    assert _paths_asked(fake_directory) == [f"/v1.0/users/{user.azure_oid}"]


async def test_colleague_search_by_a_guest_answers_with_our_own_users_and_never_searches(
    client, db_session, fake_directory: FakeDirectory
) -> None:
    headers, user = await _auth(db_session)
    user.azure_oid = str(
        fake_directory.add_user(
            "Quill Guest",
            mail="quill@partner.example",
            upn="quill_partner.example#EXT#@bial.onmicrosoft.com",
        )
    )
    await db_session.flush()
    ada = await UserFactory.create(
        db_session, email="quill.ada@example.com", display_name="Quill Ada"
    )
    fake_directory.add_user("Quill New", mail="quill.new@bial.example")

    resp = await client.get("/v1/projects/colleagues", headers=headers, params={"q": "Quill"})

    assert resp.status_code == 200
    assert [(c["id"], c["directoryId"]) for c in resp.json()["colleagues"]] == [
        (str(ada.id), None)
    ]
    assert _paths_asked(fake_directory) == [f"/v1.0/users/{user.azure_oid}"]


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


# The wire half of the shared list: the two projection fields, the facet, the numbered-page
# envelope and the refusals. The query logic itself — search scope, the facet's own predicate,
# ordering and the offset skew — is pinned directly against the service in
# `tests/services/projects/test_shares.py`.


async def _share_with(db_session, recipient, owner, *, name: str, **project_fields):
    project = await ProjectFactory.create(db_session, owner.id, name=name, **project_fields)
    share = await ProjectShareFactory.create(db_session, project.id, recipient.id)
    return project, share


async def test_the_shared_row_carries_the_sharer_id_and_the_owners_updated_date(
    client, db_session
) -> None:
    """★ Both fields are new on this wire and neither is derivable from what was already there.
    `sharedByUserId` is what the filter matches on — `display_name` is nullable and not unique —
    and `projectUpdatedAt` is the OWNER's last change, which rides the owner's own project read
    and has never reached a recipient before."""
    recipient_headers, recipient = await _auth(db_session)
    owner = await UserFactory.create(
        db_session, email="rahul@example.com", display_name="Rahul Kohli"
    )
    # An explicit `updated_at` is what makes the two dates DIFFERENT. Both columns default to
    # `now()`, which in PostgreSQL is the TRANSACTION's timestamp — left to the defaults, a row
    # wired to the share's date would read identically to one wired to the project's.
    changed = datetime(2026, 9, 14, 9, 30, tzinfo=UTC)
    project, share = await _share_with(
        db_session, recipient, owner, name="Apron Fuel Truck Log", updated_at=changed
    )

    resp = await client.get("/v1/projects/shared", headers=recipient_headers)

    assert resp.status_code == 200, resp.text
    row = resp.json()["items"][0]
    assert row["projectName"] == "Apron Fuel Truck Log"
    assert row["sharedByUserId"] == str(owner.id)
    assert row["sharedByDisplayName"] == "Rahul Kohli"
    assert datetime.fromisoformat(row["projectUpdatedAt"]) == project.updated_at == changed
    # The grant's own date is a different fact and both travel.
    assert datetime.fromisoformat(row["sharedAt"]) == share.created_at != changed


async def test_the_read_carries_the_shared_by_facet(client, db_session) -> None:
    recipient_headers, recipient = await _auth(db_session)
    rahul = await UserFactory.create(
        db_session, email="rahul@example.com", display_name="Rahul Kohli"
    )
    varun = await UserFactory.create(
        db_session, email="varun@example.com", display_name="Varun Menon"
    )
    await _share_with(db_session, recipient, rahul, name="One")
    await _share_with(db_session, recipient, rahul, name="Two")
    await _share_with(db_session, recipient, varun, name="Three")

    resp = await client.get("/v1/projects/shared", headers=recipient_headers)

    assert resp.json()["sharers"] == [
        {"userId": str(rahul.id), "displayName": "Rahul Kohli", "shareCount": 2},
        {"userId": str(varun.id), "displayName": "Varun Menon", "shareCount": 1},
    ]


async def test_the_shared_by_filter_narrows_the_page_and_its_total(client, db_session) -> None:
    recipient_headers, recipient = await _auth(db_session)
    rahul = await UserFactory.create(db_session, email="rahul@example.com")
    varun = await UserFactory.create(db_session, email="varun@example.com")
    await _share_with(db_session, recipient, rahul, name="Rahul's")
    await _share_with(db_session, recipient, varun, name="Varun's")

    resp = await client.get(
        "/v1/projects/shared", headers=recipient_headers, params={"sharedBy": str(rahul.id)}
    )

    body = resp.json()
    assert [item["projectName"] for item in body["items"]] == ["Rahul's"]
    assert body["total"] == 1
    assert body["totalPages"] == 1


async def test_search_and_sort_reach_the_read(client, db_session) -> None:
    recipient_headers, recipient = await _auth(db_session)
    owner = await UserFactory.create(db_session, email="owner@example.com")
    await _share_with(db_session, recipient, owner, name="Zebra", description="Queue readings.")
    await _share_with(db_session, recipient, owner, name="apple", description="Queue readings.")
    await _share_with(db_session, recipient, owner, name="Ignored", description="Fuel uplift.")

    resp = await client.get(
        "/v1/projects/shared", headers=recipient_headers, params={"q": "queue", "sort": "name"}
    )

    assert [item["projectName"] for item in resp.json()["items"]] == ["apple", "Zebra"]
    assert resp.json()["total"] == 2


async def test_an_unrecognised_sort_is_refused_in_this_platforms_envelope(
    client, db_session
) -> None:
    """A typo'd `sort` served quietly in the default order looks, to the person using the
    control, exactly like a control that does not work. The envelope matters too: a native
    FastAPI refusal would put a second 422 body shape on this one endpoint."""
    headers, _ = await _auth(db_session)

    resp = await client.get(
        "/v1/projects/shared", headers=headers, params={"sort": "recentlyShareed"}
    )

    assert resp.status_code == 422
    assert resp.json()["error"]["message"] == "sort must be one of: recentlyShared, name."


async def test_a_malformed_shared_by_is_refused_in_this_platforms_envelope(
    client, db_session
) -> None:
    headers, _ = await _auth(db_session)

    resp = await client.get("/v1/projects/shared", headers=headers, params={"sharedBy": "nobody"})

    assert resp.status_code == 422
    assert resp.json()["error"]["message"] == "sharedBy must be a colleague id."


async def test_a_revoke_under_the_last_page_leaves_a_page_number_to_step_back_to(
    client, db_session
) -> None:
    """★ THE PAGE-SHRINK GUARD'S SERVER HALF. A colleague revoking while the recipient sits on
    the last page must leave them a real `totalPages` to step back to — an empty `items` beside
    a stale total, or a 404, is the empty table the guard exists to prevent."""
    recipient_headers, recipient = await _auth(db_session)
    owner = await UserFactory.create(db_session, email="owner@example.com")
    for index in range(3):
        await _share_with(db_session, recipient, owner, name=f"App {index}")
    last, _share = await _share_with(db_session, recipient, owner, name="Last")

    before = await client.get(
        "/v1/projects/shared", headers=recipient_headers, params={"page": 2, "limit": 3}
    )
    await revoke_share(db_session, project=last, actor_id=owner.id, colleague_id=recipient.id)
    await db_session.flush()
    after = await client.get(
        "/v1/projects/shared", headers=recipient_headers, params={"page": 2, "limit": 3}
    )

    assert [item["projectName"] for item in before.json()["items"]] == ["App 0"]
    assert before.json()["totalPages"] == 2
    assert after.status_code == 200
    assert after.json()["items"] == []
    assert after.json()["total"] == 3
    assert after.json()["totalPages"] == 1


async def test_the_shared_envelope_no_longer_carries_a_keyset_cursor(client, db_session) -> None:
    """The keyset envelope is GONE, not merely unread: a client still branching on `hasMore`
    would page forever against a response that never sets it."""
    recipient_headers, recipient = await _auth(db_session)
    owner = await UserFactory.create(db_session, email="owner@example.com")
    await _share_with(db_session, recipient, owner, name="Only")

    body = (await client.get("/v1/projects/shared", headers=recipient_headers)).json()

    assert "nextCursor" not in body
    assert "hasMore" not in body
    # Liveness: the envelope really did answer, so the two absences are about its shape rather
    # than about an empty response.
    assert [item["projectName"] for item in body["items"]] == ["Only"]
    assert (body["page"], body["pageSize"], body["total"], body["totalPages"]) == (1, 25, 1, 1)
