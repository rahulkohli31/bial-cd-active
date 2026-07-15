"""Projects CRUD (U4) + rollback-safe cascade delete (U6).

Covers create/list/get/patch/delete owner-scoping, KD-8 description normalization + length
cap, KD-1 keyset stability under concurrent insert (AE3), the R7 page cap, and the KD-3
cascade: children swept through the blob-aware core, blobs deleted only post-commit.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, select

from src.config import settings
from src.db.models.app_file import AppFile
from src.db.models.app_registry import AppRegistry
from src.db.models.attachment import Attachment
from src.db.models.audit import AuditLog
from src.db.models.conversation import Conversation
from src.db.models.project import Project
from src.main import create_app
from src.services.auth.session_jwt import mint_session_jwt
from src.services.extract.office import PPTX_MEDIA_TYPE
from src.services.projects import delete_project_cascade
from src.services.storage import AppContainerStore, snapshot_key
from tests.factories import (
    AppRegistryFactory,
    ConversationFactory,
    MessageFactory,
    ProjectFactory,
    UserFactory,
)

_TTL = settings.auth.access_ttl_seconds


def _cookie(jwt: str) -> dict[str, str]:
    return {"Cookie": f"session={jwt}"}


async def _auth(db_session):
    user = await UserFactory.create(db_session)
    return _cookie(mint_session_jwt(user.id, user.token_version, _TTL)), user


# --- create -------------------------------------------------------------------


async def test_create_owned_and_listed_only_for_owner(client, db_session) -> None:
    headers, user = await _auth(db_session)
    resp = await client.post(
        "/v1/projects", headers=headers, json={"name": "VIP Movement", "description": "  "}
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "VIP Movement"
    # "  " normalizes to NULL at the write boundary (KD-8).
    assert body["description"] is None
    assert "createdAt" in body and "updatedAt" in body

    row = await db_session.get(Project, uuid.UUID(body["id"]))
    assert row is not None and row.user_id == user.id
    assert row.description is None

    # Another user sees nothing.
    other_headers, _ = await _auth(db_session)
    other_list = await client.get("/v1/projects", headers=other_headers)
    assert other_list.json()["items"] == []


async def test_create_over_length_description_422(client, db_session) -> None:
    headers, _ = await _auth(db_session)
    resp = await client.post(
        "/v1/projects", headers=headers, json={"name": "X", "description": "a" * 2001}
    )
    assert resp.status_code == 422


async def test_create_blank_name_422(client, db_session) -> None:
    headers, _ = await _auth(db_session)
    resp = await client.post("/v1/projects", headers=headers, json={"name": "   "})
    assert resp.status_code == 422


async def test_create_requires_auth_401(client) -> None:
    resp = await client.post("/v1/projects", json={"name": "X"})
    assert resp.status_code == 401


# --- patch --------------------------------------------------------------------


async def test_patch_updates_name_and_clears_description(client, db_session) -> None:
    headers, user = await _auth(db_session)
    project = await ProjectFactory.create(db_session, user.id, description="original")
    await db_session.commit()

    resp = await client.patch(
        f"/v1/projects/{project.id}",
        headers=headers,
        json={"name": "Renamed", "description": None},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "Renamed"
    assert body["description"] is None


async def test_patch_over_length_description_422(client, db_session) -> None:
    headers, user = await _auth(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    await db_session.commit()
    resp = await client.patch(
        f"/v1/projects/{project.id}", headers=headers, json={"description": "a" * 2001}
    )
    assert resp.status_code == 422


@pytest.mark.filterwarnings("ignore:transaction already deassociated:sqlalchemy.exc.SAWarning")
async def test_patch_losing_race_to_delete_is_404_not_500(client, db_session, monkeypatch) -> None:
    # (the filtered SAWarning is a test-harness artifact: the endpoint's failed commit
    # plus the fixture's outer rollback double-clean the same connection)
    # PATCH vs a concurrent project delete: the deleted row makes the flush UPDATE match
    # zero rows (StaleDataError) — the loser must get the same non-leaking 404 a PATCH
    # one second later would, never a 500 (mirrors the conversations patch-vs-delete race).
    import importlib

    from src.services.projects import owned_project_or_404 as real_load

    # importlib, not `import … as`: the package __init__ re-exports the APIRouter under
    # the same name `router`, which shadows the submodule on attribute-style imports.
    proj_router = importlib.import_module("src.api.v1.projects.router")

    headers, user = await _auth(db_session)
    project = await ProjectFactory.create(db_session, user.id)

    async def _load_then_lose_race(db, user_id, project_id):
        owned = await real_load(db, user_id, project_id)
        # The concurrent DELETE commits between our load and our flush.
        # synchronize_session=False: a REAL concurrent delete happens in another
        # session, so THIS session's identity map must not learn about it — the
        # flush then emits the zero-row UPDATE exactly as in production.
        await db.execute(
            delete(Project).where(Project.id == owned.id),
            execution_options={"synchronize_session": False},
        )
        return owned

    monkeypatch.setattr(proj_router, "owned_project_or_404", _load_then_lose_race)
    resp = await client.patch(f"/v1/projects/{project.id}", headers=headers, json={"name": "T"})
    assert resp.status_code == 404
    assert resp.json() == {"error": {"message": "Project not found."}}


async def test_patch_name_cannot_be_cleared_400(client, db_session) -> None:
    headers, user = await _auth(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    await db_session.commit()
    resp = await client.patch(f"/v1/projects/{project.id}", headers=headers, json={"name": None})
    assert resp.status_code == 400


# --- cross-user isolation -----------------------------------------------------


async def test_get_patch_delete_cross_user_404(client, db_session) -> None:
    headers, _ = await _auth(db_session)
    other = await UserFactory.create(db_session)
    victim = await ProjectFactory.create(db_session, other.id, name="Secret")
    await db_session.commit()

    assert (await client.get(f"/v1/projects/{victim.id}", headers=headers)).status_code == 404
    assert (
        await client.patch(f"/v1/projects/{victim.id}", headers=headers, json={"name": "hax"})
    ).status_code == 404
    assert (await client.delete(f"/v1/projects/{victim.id}", headers=headers)).status_code == 404
    # The victim row is untouched.
    still = await db_session.get(Project, victim.id)
    assert still is not None and still.name == "Secret"


# --- keyset pagination (AE3) --------------------------------------------------


async def test_keyset_stable_under_concurrent_insert(client, db_session) -> None:
    headers, user = await _auth(db_session)
    # Three projects, oldest→newest (UUIDv7 ids are monotonic with creation).
    for name in ("p1", "p2", "p3"):
        await ProjectFactory.create(db_session, user.id, name=name)
    await db_session.commit()

    page1 = (await client.get("/v1/projects?limit=2", headers=headers)).json()
    assert [p["name"] for p in page1["items"]] == ["p3", "p2"]  # newest-first
    assert page1["hasMore"] is True
    cursor = page1["nextCursor"]
    assert cursor is not None

    # A new project is inserted BETWEEN the two page fetches (the case offset can't satisfy).
    await ProjectFactory.create(db_session, user.id, name="p4")
    await db_session.commit()

    page2 = (await client.get(f"/v1/projects?limit=2&cursor={cursor}", headers=headers)).json()
    names2 = [p["name"] for p in page2["items"]]
    # No duplicate of page 1, no skipped row: page 2 continues strictly below the cursor.
    assert names2 == ["p1"]
    assert page2["hasMore"] is False
    seen = {p["name"] for p in page1["items"]} | set(names2)
    assert "p2" not in names2 and "p3" not in names2  # no dup
    assert seen == {"p1", "p2", "p3"}  # p4 (newer than cursor) is simply not in this window


async def test_limit_out_of_range_422(client, db_session) -> None:
    headers, _ = await _auth(db_session)
    envelope = {"error": {"message": "limit must be between 1 and 100."}}
    for bad in ("0", "101"):
        resp = await client.get(f"/v1/projects?limit={bad}", headers=headers)
        assert resp.status_code == 422
        # The SAME `{error:{message}}` shape as the cursor/q 422s — never FastAPI's
        # native `{detail:[...]}` body (one endpoint, one 422 shape).
        assert resp.json() == envelope


async def test_malformed_cursor_422(client, db_session) -> None:
    headers, _ = await _auth(db_session)
    resp = await client.get("/v1/projects?cursor=not-a-uuid", headers=headers)
    assert resp.status_code == 422


def test_list_projects_documents_422_in_openapi() -> None:
    paths = create_app().openapi()["paths"]
    assert {"401", "422", "500"} <= set(paths["/v1/projects"]["get"]["responses"])


async def test_q_filters_case_insensitive(client, db_session) -> None:
    headers, user = await _auth(db_session)
    await ProjectFactory.create(db_session, user.id, name="Movement Tracker")
    await ProjectFactory.create(db_session, user.id, name="Cafeteria Menu")
    await db_session.commit()
    resp = await client.get("/v1/projects?q=movement", headers=headers)
    names = [p["name"] for p in resp.json()["items"]]
    assert names == ["Movement Tracker"]


# --- read-only app discovery (appId/appStatus on ProjectResponse) --------------


async def test_app_discovery_null_for_fresh_project_then_populated(client, db_session) -> None:
    # A fresh project exposes appId/appStatus as null — the SPA can learn "no app yet"
    # without a mutating provision; after provision both populate everywhere a
    # ProjectResponse is built (get + list here; create is by definition app-less).
    headers, _ = await _auth(db_session)
    created = (await client.post("/v1/projects", headers=headers, json={"name": "Disco"})).json()
    assert created["appId"] is None and created["appStatus"] is None

    fetched = (await client.get(f"/v1/projects/{created['id']}", headers=headers)).json()
    assert fetched["appId"] is None and fetched["appStatus"] is None

    prov = await client.post(
        "/v1/apps/provision",
        json={"conversationId": str(uuid.uuid4()), "projectId": created["id"]},
        headers=headers,
    )
    assert prov.status_code == 201
    app_id = prov.json()["appId"]

    fetched = (await client.get(f"/v1/projects/{created['id']}", headers=headers)).json()
    assert fetched["appId"] == app_id and fetched["appStatus"] == "draft"

    listed = (await client.get("/v1/projects", headers=headers)).json()
    row = next(p for p in listed["items"] if p["id"] == created["id"])
    assert row["appId"] == app_id and row["appStatus"] == "draft"


# --- cascade delete (U4 endpoint + U6 service) --------------------------------


async def _app_file(db_session, app_id, blob_key: str) -> AppFile:
    row = AppFile(
        app_id=app_id,
        filename="data.csv",
        content_type="text/csv",
        size=3,
        blob_key=blob_key,
    )
    db_session.add(row)
    await db_session.flush()
    return row


async def _attachment(db_session, user_id, storage_key: str, media_type: str = "image/png") -> str:
    att_id = uuid.uuid4().hex
    db_session.add(
        Attachment(
            user_id=user_id,
            attachment_id=att_id,
            media_type=media_type,
            size=3,
            storage_key=storage_key,
        )
    )
    await db_session.flush()
    return att_id


async def test_delete_cascades_children_and_sweeps_blobs(client, db_session, fake_storage) -> None:
    headers, user = await _auth(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    # An app with a stored file blob.
    app = await AppRegistryFactory.create(db_session, user_id=user.id, project_id=project.id)
    await _app_file(db_session, app.id, "apps/file-blob")
    fake_storage.objects["apps/file-blob"] = b"..."
    # A conversation whose message references a PPTX attachment (blob + derived .pdf sibling).
    conv = await ConversationFactory.create(db_session, user.id, project_id=project.id)
    att_id = await _attachment(db_session, user.id, "att/deck", media_type=PPTX_MEDIA_TYPE)
    fake_storage.objects["att/deck"] = b"deck"
    fake_storage.objects["att/deck.pdf"] = b"pdf"
    await MessageFactory.create(
        db_session,
        user.id,
        conv.id,
        parts=[{"type": "file", "attachmentId": att_id, "kind": "deck", "mediaType": "x"}],
    )
    await db_session.commit()

    resp = await client.delete(f"/v1/projects/{project.id}", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    # Rows gone.
    assert await db_session.get(Project, project.id) is None
    assert await db_session.get(AppRegistry, app.id) is None
    assert await db_session.get(Conversation, conv.id) is None
    assert (
        await db_session.scalar(select(Attachment).where(Attachment.attachment_id == att_id))
    ) is None
    # Blobs swept (app file + deck + derived pdf).
    assert "apps/file-blob" not in fake_storage.objects
    assert "att/deck" not in fake_storage.objects
    assert "att/deck.pdf" not in fake_storage.objects
    # Audit written.
    audit = await db_session.scalar(
        select(AuditLog).where(
            AuditLog.action == "project:delete", AuditLog.resource_id == str(project.id)
        )
    )
    assert audit is not None and audit.actor_id == user.id


async def test_delete_requires_auth_401(client, db_session) -> None:
    _, user = await _auth(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    resp = await client.delete(f"/v1/projects/{project.id}")  # no cookie
    assert resp.status_code == 401


# --- U4: the post-commit per-app Blob CONTAINER sweep (C9 §6, KTD-7) -----------


class _RecordingContainerStore(AppContainerStore):
    """A container store that records (or fails) delete_container without touching Azure.
    Overrides __init__ so no config/client is needed."""

    def __init__(self, *, fail: bool = False) -> None:  # noqa: D107
        self.deleted: list[uuid.UUID] = []
        self._fail = fail

    async def delete_container(self, app_id: uuid.UUID) -> None:
        if self._fail:
            raise RuntimeError("container delete boom")
        self.deleted.append(app_id)


def _override_container_store(app, store) -> None:
    from src.api.v1.projects.router import container_store_dependency

    app.dependency_overrides[container_store_dependency] = lambda: store


async def test_delete_sweeps_the_apps_blob_container(
    app, client, db_session, fake_storage
) -> None:
    store = _RecordingContainerStore()
    _override_container_store(app, store)
    headers, user = await _auth(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    app_row = await AppRegistryFactory.create(db_session, user_id=user.id, project_id=project.id)
    await db_session.commit()

    resp = await client.delete(f"/v1/projects/{project.id}", headers=headers)
    assert resp.status_code == 200
    # The app's per-app Blob container was swept post-commit (wholesale, by app id).
    assert store.deleted == [app_row.id]


async def test_delete_survives_a_container_sweep_failure(
    app, client, db_session, fake_storage
) -> None:
    # A failing container delete is best-effort (KD-3): it is logged, never surfaced, so an
    # already-committed project delete still returns 200.
    _override_container_store(app, _RecordingContainerStore(fail=True))
    headers, user = await _auth(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    await AppRegistryFactory.create(db_session, user_id=user.id, project_id=project.id)
    await db_session.commit()

    resp = await client.delete(f"/v1/projects/{project.id}", headers=headers)
    assert resp.status_code == 200
    assert await db_session.get(Project, project.id) is None  # the delete still committed


async def test_delete_succeeds_with_storage_disabled(client, db_session, fake_storage) -> None:
    # No container-store override: in the test env object storage is unconfigured, so
    # container_store_dependency yields None and sweep_app_containers no-ops — the delete
    # still succeeds even though the project HAS an app (non-empty app_container_ids, KTD-2).
    headers, user = await _auth(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    await AppRegistryFactory.create(db_session, user_id=user.id, project_id=project.id)
    await db_session.commit()

    resp = await client.delete(f"/v1/projects/{project.id}", headers=headers)
    assert resp.status_code == 200
    assert await db_session.get(Project, project.id) is None


# --- U6 service directly ------------------------------------------------------


async def test_cascade_deletes_rows_and_returns_blob_keys(db_session) -> None:
    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    app = await AppRegistryFactory.create(db_session, user_id=user.id, project_id=project.id)
    await _app_file(db_session, app.id, "apps/k1")
    conv = await ConversationFactory.create(db_session, user.id, project_id=project.id)
    att_id = await _attachment(db_session, user.id, "att/k2", media_type=PPTX_MEDIA_TYPE)
    await MessageFactory.create(
        db_session,
        user.id,
        conv.id,
        parts=[{"type": "file", "attachmentId": att_id, "kind": "deck", "mediaType": "x"}],
    )

    cleanup = await delete_project_cascade(db_session, project, user_id=user.id)

    # Rows deleted within the (still-uncommitted) transaction.
    assert await db_session.get(AppRegistry, app.id) is None
    assert await db_session.get(Conversation, conv.id) is None
    assert await db_session.get(Project, project.id) is None
    # Blob keys RETURNED for a post-commit sweep — the service itself touches no store.
    # The app contributes its (legacy) file blob AND its C4 snapshot bundle key.
    assert set(cleanup.blob_keys) == {"apps/k1", snapshot_key(app.id), "att/k2", "att/k2.pdf"}
    # ...and the app id, so the caller can sweep its per-app Blob container wholesale (C9 §6).
    assert cleanup.app_container_ids == [app.id]


async def test_cascade_batches_many_conversations_and_dedups_shared_attachment(db_session) -> None:
    # A project with several conversations cascades in ONE batched pass: every conversation +
    # its messages + attachment rows go, and each blob key comes back once even when two
    # conversations reference the SAME attachment (the batched union dedups it) — while a deck
    # attachment still contributes its derived `.pdf` sibling.
    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    shared = await _attachment(db_session, user.id, "att/shared")  # image → no pdf sibling
    deck = await _attachment(db_session, user.id, "att/deck", media_type=PPTX_MEDIA_TYPE)

    conv_a = await ConversationFactory.create(db_session, user.id, project_id=project.id)
    await MessageFactory.create(
        db_session,
        user.id,
        conv_a.id,
        parts=[
            {"type": "file", "attachmentId": shared, "kind": "image", "mediaType": "image/png"}
        ],
    )
    conv_b = await ConversationFactory.create(db_session, user.id, project_id=project.id)
    await MessageFactory.create(
        db_session,
        user.id,
        conv_b.id,
        parts=[{"type": "file", "attachmentId": deck, "kind": "deck", "mediaType": "x"}],
    )
    # A THIRD conversation reuses the shared attachment — the singular N+1 dedup'd this by
    # deleting the row on the first pass; the batched union must dedup it up front.
    conv_c = await ConversationFactory.create(db_session, user.id, project_id=project.id)
    await MessageFactory.create(
        db_session,
        user.id,
        conv_c.id,
        parts=[
            {"type": "file", "attachmentId": shared, "kind": "image", "mediaType": "image/png"}
        ],
    )

    cleanup = await delete_project_cascade(db_session, project, user_id=user.id)

    # Every conversation gone (messages cascade at the DB level), the project gone.
    for conv in (conv_a, conv_b, conv_c):
        assert await db_session.get(Conversation, conv.id) is None
    assert await db_session.get(Project, project.id) is None
    # Both attachment rows gone.
    assert await db_session.scalar(select(Attachment).where(Attachment.user_id == user.id)) is None
    # Shared key returned exactly once; the deck contributes its blob + derived `.pdf`.
    assert sorted(cleanup.blob_keys) == ["att/deck", "att/deck.pdf", "att/shared"]
    assert cleanup.app_container_ids == []  # this project has no app → no container to sweep


async def test_cascade_rollback_restores_rows(db_session) -> None:
    # Rollback safety (KD-3): the cascade deletes rows only (never commits, never touches the
    # store), so a rolled-back transaction fully restores them and no blob was destroyed — the
    # sweep is the caller's post-commit job. A savepoint rolled back here stands in for a
    # mid-cascade DB error / a failed commit.
    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    app = await AppRegistryFactory.create(db_session, user_id=user.id, project_id=project.id)
    conv = await ConversationFactory.create(db_session, user.id, project_id=project.id)
    app_id, conv_id, project_id = app.id, conv.id, project.id

    savepoint = await db_session.begin_nested()
    await delete_project_cascade(db_session, project, user_id=user.id)
    await savepoint.rollback()

    # Re-query at the DB level (no ORM identity-map reuse) — the savepoint rollback restored
    # every row, proving the cascade committed nothing.
    assert await db_session.scalar(select(Project).where(Project.id == project_id)) is not None
    assert await db_session.scalar(select(AppRegistry).where(AppRegistry.id == app_id)) is not None
    assert (
        await db_session.scalar(select(Conversation).where(Conversation.id == conv_id)) is not None
    )


async def test_cascade_owner_scoped(db_session) -> None:
    # The service enumerates children by (project_id, user_id): a mismatched user_id deletes
    # nothing (defense in depth around the endpoint's own 404).
    owner = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, owner.id)
    app = await AppRegistryFactory.create(db_session, user_id=owner.id, project_id=project.id)
    stranger = await UserFactory.create(db_session)

    cleanup = await delete_project_cascade(db_session, project, user_id=stranger.id)
    assert cleanup.blob_keys == []
    # Cross-user isolation: the stranger's scope matched no app, so NO container id comes back —
    # a stranger's delete can never sweep the owner's per-app Blob container (C9 §6).
    assert cleanup.app_container_ids == []
    # The owner's app survives (the stranger's scope matched no child).
    assert await db_session.get(AppRegistry, app.id) is not None
