"""Projects CRUD (U4) + rollback-safe cascade delete (U6).

Covers create/list/get/patch/delete owner-scoping, KD-8 description normalization + length
cap, KD-1 keyset stability under concurrent insert (AE3), the R7 page cap, and the KD-3
cascade: children swept through the blob-aware core, blobs deleted only post-commit.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from src.config import settings
from src.db.models.app_file import AppFile
from src.db.models.app_registry import AppRegistry
from src.db.models.attachment import Attachment
from src.db.models.audit import AuditLog
from src.db.models.conversation import Conversation
from src.db.models.project import Project
from src.services.auth.session_jwt import mint_session_jwt
from src.services.extract.office import PPTX_MEDIA_TYPE
from src.services.projects import delete_project_cascade
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
    assert (await client.get("/v1/projects?limit=0", headers=headers)).status_code == 422
    assert (await client.get("/v1/projects?limit=101", headers=headers)).status_code == 422


async def test_malformed_cursor_422(client, db_session) -> None:
    headers, _ = await _auth(db_session)
    resp = await client.get("/v1/projects?cursor=not-a-uuid", headers=headers)
    assert resp.status_code == 422


async def test_q_filters_case_insensitive(client, db_session) -> None:
    headers, user = await _auth(db_session)
    await ProjectFactory.create(db_session, user.id, name="Movement Tracker")
    await ProjectFactory.create(db_session, user.id, name="Cafeteria Menu")
    await db_session.commit()
    resp = await client.get("/v1/projects?q=movement", headers=headers)
    names = [p["name"] for p in resp.json()["items"]]
    assert names == ["Movement Tracker"]


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

    keys = await delete_project_cascade(db_session, project, user_id=user.id)

    # Rows deleted within the (still-uncommitted) transaction.
    assert await db_session.get(AppRegistry, app.id) is None
    assert await db_session.get(Conversation, conv.id) is None
    assert await db_session.get(Project, project.id) is None
    # Blob keys RETURNED for a post-commit sweep — the service itself touches no store.
    assert set(keys) == {"apps/k1", "att/k2", "att/k2.pdf"}


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

    keys = await delete_project_cascade(db_session, project, user_id=stranger.id)
    assert keys == []
    # The owner's app survives (the stranger's scope matched no child).
    assert await db_session.get(AppRegistry, app.id) is not None
