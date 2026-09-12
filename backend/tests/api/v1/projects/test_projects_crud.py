"""Projects CRUD + rollback-safe cascade delete.

Covers create/list/get/patch/delete owner-scoping, KD-8/#191 description requirement +
length cap, KD-1 keyset stability under concurrent insert, the R7 page cap, and the
KD-3 cascade: children swept through the blob-aware core, blobs deleted only post-commit.
The description's WORD bound gets its own boundary-pinning file,
`test_project_description_words.py`, mirroring `test_project_name_words.py`.
"""

from __future__ import annotations

import uuid

import pytest
from redis.exceptions import RedisError
from sqlalchemy import delete, select

from src.config import settings
from src.db.models.app_registry import AppRegistry
from src.db.models.attachment import Attachment
from src.db.models.audit import AuditLog
from src.db.models.conversation import Conversation
from src.db.models.project import Project
from src.main import create_app
from src.services.auth.session_jwt import mint_session_jwt
from src.services.build_sessions.appdata import resolve_app_for_project
from src.services.media.lanes import PPTX_MEDIA_TYPE
from src.services.projects import delete_project_cascade
from src.services.storage import AppContainerStore, recovery_key, snapshot_key
from tests.api.v1.projects.conftest import _VALID_DESCRIPTION, DELETE_BODY
from tests.factories import (
    AppRegistryFactory,
    ConversationFactory,
    MessageFactory,
    ProjectFactory,
    UserFactory,
)
from tests.fakes import FakeStorage


def _ref_payload(attachment_id: str) -> list:
    """A native user turn referencing an attachment, in the stored ref-marker shape."""
    from pydantic_ai import BinaryContent
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    from src.services.messages.store import dump_for_row

    return dump_for_row(
        [
            ModelRequest(
                parts=[
                    UserPromptPart(
                        content=[
                            BinaryContent(
                                data=b"\x89PNGx", media_type="image/png", identifier=attachment_id
                            )
                        ]
                    )
                ]
            )
        ]
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
        "/v1/projects",
        headers=headers,
        json={"name": "VIP Movement", "description": _VALID_DESCRIPTION},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "VIP Movement"
    assert body["description"] == _VALID_DESCRIPTION
    assert "createdAt" in body and "updatedAt" in body

    row = await db_session.get(Project, uuid.UUID(body["id"]))
    assert row is not None and row.user_id == user.id
    assert row.description == _VALID_DESCRIPTION

    other_headers, _ = await _auth(db_session)
    other_list = await client.get("/v1/projects", headers=other_headers)
    assert other_list.json()["items"] == []


async def test_create_blank_description_422(client, db_session) -> None:
    # #191: description is now required — whitespace-only no longer normalizes to NULL,
    # it is refused, the same as a missing one would be.
    headers, _ = await _auth(db_session)
    resp = await client.post(
        "/v1/projects", headers=headers, json={"name": "VIP Movement", "description": "  "}
    )
    assert resp.status_code == 422


async def test_create_missing_description_422(client, db_session) -> None:
    headers, _ = await _auth(db_session)
    resp = await client.post("/v1/projects", headers=headers, json={"name": "VIP Movement"})
    assert resp.status_code == 422


async def test_create_over_length_description_422(client, db_session) -> None:
    headers, _ = await _auth(db_session)
    resp = await client.post(
        "/v1/projects", headers=headers, json={"name": "X", "description": "a" * 2001}
    )
    assert resp.status_code == 422


async def test_create_blank_name_422(client, db_session) -> None:
    headers, _ = await _auth(db_session)
    resp = await client.post(
        "/v1/projects", headers=headers, json={"name": "   ", "description": _VALID_DESCRIPTION}
    )
    assert resp.status_code == 422


async def test_create_requires_auth_401(client) -> None:
    resp = await client.post("/v1/projects", json={"name": "X", "description": _VALID_DESCRIPTION})
    assert resp.status_code == 401


# --- patch --------------------------------------------------------------------


async def test_patch_updates_name_and_description_together(client, db_session) -> None:
    headers, user = await _auth(db_session)
    project = await ProjectFactory.create(db_session, user.id, description="original")
    await db_session.commit()

    resp = await client.patch(
        f"/v1/projects/{project.id}",
        headers=headers,
        json={"name": "Renamed", "description": _VALID_DESCRIPTION},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "Renamed"
    assert body["description"] == _VALID_DESCRIPTION


async def test_patch_description_cannot_be_cleared_400(client, db_session) -> None:
    # #191 widened the rename path's existing "cannot be cleared" rule to cover
    # description too — mirrors test_patch_name_cannot_be_cleared_400 below exactly.
    headers, user = await _auth(db_session)
    project = await ProjectFactory.create(db_session, user.id, description="original")
    await db_session.commit()

    resp = await client.patch(
        f"/v1/projects/{project.id}", headers=headers, json={"description": None}
    )
    assert resp.status_code == 400

    # ...and the stored description is untouched by the refusal.
    unchanged = await db_session.get(Project, project.id)
    assert unchanged is not None and unchanged.description == "original"


async def test_patch_blank_description_422(client, db_session) -> None:
    # Whitespace-only is a WRITE, not a clear-to-null request — it goes through the same
    # required/word-bounded validator a create would, and fails it the same way.
    headers, user = await _auth(db_session)
    project = await ProjectFactory.create(db_session, user.id, description="original")
    await db_session.commit()

    resp = await client.patch(
        f"/v1/projects/{project.id}", headers=headers, json={"description": "   "}
    )
    assert resp.status_code == 422


async def test_patch_a_project_with_no_prior_description_can_add_one(client, db_session) -> None:
    # R14: a project that predates #191 (or was never given one) keeps working untouched —
    # editing it is exactly how it stops being absent from search.
    headers, user = await _auth(db_session)
    project = await ProjectFactory.create(db_session, user.id, description=None)
    await db_session.commit()

    resp = await client.patch(
        f"/v1/projects/{project.id}", headers=headers, json={"description": _VALID_DESCRIPTION}
    )
    assert resp.status_code == 200
    assert resp.json()["description"] == _VALID_DESCRIPTION


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
    # The filtered SAWarning is a harness artifact: the endpoint's failed commit plus the
    # fixture's outer rollback double-clean the same connection.
    import importlib

    from src.services.projects import owned_project_or_404 as real_load

    # importlib, not `import … as`: the package __init__ re-exports the APIRouter under
    # the same name `router`, which shadows the submodule on attribute-style imports.
    proj_router = importlib.import_module("src.api.v1.projects.router")

    headers, user = await _auth(db_session)
    project = await ProjectFactory.create(db_session, user.id)

    async def _load_then_lose_race(db, user_id, project_id):
        owned = await real_load(db, user_id, project_id)
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
    assert (
        await client.request(
            "DELETE",
            f"/v1/projects/{victim.id}",
            headers=headers,
            json=DELETE_BODY,
        )
    ).status_code == 404
    still = await db_session.get(Project, victim.id)
    assert still is not None and still.name == "Secret"


# --- paging -------------------------------------------------------------------


async def test_offset_paging_walks_the_whole_list_newest_first(client, db_session) -> None:
    headers, user = await _auth(db_session)
    for name in ("p1", "p2", "p3"):
        await ProjectFactory.create(db_session, user.id, name=name)
    await db_session.commit()

    page1 = (await client.get("/v1/projects?limit=2", headers=headers)).json()
    assert [p["name"] for p in page1["items"]] == ["p3", "p2"]
    assert (page1["page"], page1["pageSize"]) == (1, 2)
    assert (page1["total"], page1["totalPages"]) == (3, 2)

    page2 = (await client.get("/v1/projects?limit=2&page=2", headers=headers)).json()
    assert [p["name"] for p in page2["items"]] == ["p1"]
    assert page2["total"] == 3

    assert [p["name"] for p in page1["items"] + page2["items"]] == ["p3", "p2", "p1"]


async def test_a_page_past_the_end_is_empty_with_a_real_total(client, db_session) -> None:
    headers, user = await _auth(db_session)
    await ProjectFactory.create(db_session, user.id, name="only")
    await db_session.commit()

    body = (await client.get("/v1/projects?limit=10&page=9", headers=headers)).json()

    assert body["items"] == []
    assert body["total"] == 1
    assert body["totalPages"] == 1


async def test_total_counts_the_search_not_the_collection(client, db_session) -> None:
    headers, user = await _auth(db_session)
    for name in ("VIP Movement", "VIP Transfer", "Baggage Desk"):
        await ProjectFactory.create(db_session, user.id, name=name)
    await db_session.commit()

    body = (await client.get("/v1/projects?q=vip", headers=headers)).json()

    assert body["total"] == 2
    assert {p["name"] for p in body["items"]} == {"VIP Movement", "VIP Transfer"}


async def test_out_of_range_page_422(client, db_session) -> None:
    """Bounded so an absurd page cannot overflow asyncpg's OFFSET parameter into a raw
    `DataError` and a 500 — a refusal the client can read, not a crash."""
    headers, _ = await _auth(db_session)

    for bad in ("0", "-1", "100001"):
        resp = await client.get(f"/v1/projects?page={bad}", headers=headers)
        assert resp.status_code == 422, f"page={bad}: {resp.text}"
        assert "page must be between" in resp.text


async def test_limit_out_of_range_422(client, db_session) -> None:
    headers, _ = await _auth(db_session)
    envelope = {"error": {"message": "limit must be between 1 and 100."}}
    for bad in ("0", "101"):
        resp = await client.get(f"/v1/projects?limit={bad}", headers=headers)
        assert resp.status_code == 422
        assert resp.json() == envelope


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
    headers, user = await _auth(db_session)
    created = (
        await client.post(
            "/v1/projects",
            headers=headers,
            json={"name": "Disco", "description": _VALID_DESCRIPTION},
        )
    ).json()
    assert created["appId"] is None and created["appStatus"] is None

    fetched = (await client.get(f"/v1/projects/{created['id']}", headers=headers)).json()
    assert fetched["appId"] is None and fetched["appStatus"] is None

    app_id = str(await resolve_app_for_project(db_session, user.id, uuid.UUID(created["id"])))
    await db_session.commit()

    fetched = (await client.get(f"/v1/projects/{created['id']}", headers=headers)).json()
    assert fetched["appId"] == app_id and fetched["appStatus"] == "draft"

    listed = (await client.get("/v1/projects", headers=headers)).json()
    row = next(p for p in listed["items"] if p["id"] == created["id"])
    assert row["appId"] == app_id and row["appStatus"] == "draft"


# --- cascade delete -----------------------------------------------------------


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
    app = await AppRegistryFactory.create(db_session, user_id=user.id, project_id=project.id)
    fake_storage.objects[snapshot_key(app.id)] = b"bundle"
    # A PPTX attachment carries a derived `.pdf` sibling; both have to go.
    conv = await ConversationFactory.create(db_session, user.id, project_id=project.id)
    att_id = await _attachment(db_session, user.id, "att/deck", media_type=PPTX_MEDIA_TYPE)
    fake_storage.objects["att/deck"] = b"deck"
    await MessageFactory.create(
        db_session,
        user.id,
        conv.id,
        payload=_ref_payload(att_id),
    )
    await db_session.commit()

    resp = await client.request(
        "DELETE",
        f"/v1/projects/{project.id}",
        headers=headers,
        json=DELETE_BODY,
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    assert await db_session.get(Project, project.id) is None
    assert await db_session.get(AppRegistry, app.id) is None
    assert await db_session.get(Conversation, conv.id) is None
    assert (
        await db_session.scalar(select(Attachment).where(Attachment.attachment_id == att_id))
    ) is None
    assert snapshot_key(app.id) not in fake_storage.objects
    assert "att/deck" not in fake_storage.objects
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


# --- the post-commit per-app Blob CONTAINER sweep ------------------------------


class _RecordingContainerStore(AppContainerStore):
    """Records (or fails) `delete_container` without touching Azure; `__init__` is overridden
    so no config or client is needed."""

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

    resp = await client.request(
        "DELETE",
        f"/v1/projects/{project.id}",
        headers=headers,
        json=DELETE_BODY,
    )
    assert resp.status_code == 200
    assert store.deleted == [app_row.id]


async def test_delete_survives_a_container_sweep_failure(
    app, client, db_session, fake_storage
) -> None:
    _override_container_store(app, _RecordingContainerStore(fail=True))
    headers, user = await _auth(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    await AppRegistryFactory.create(db_session, user_id=user.id, project_id=project.id)
    await db_session.commit()

    resp = await client.request(
        "DELETE",
        f"/v1/projects/{project.id}",
        headers=headers,
        json=DELETE_BODY,
    )
    assert resp.status_code == 200
    assert await db_session.get(Project, project.id) is None


async def test_delete_succeeds_with_storage_disabled(client, db_session, fake_storage) -> None:
    # NO container-store override, deliberately: with object storage unconfigured the
    # dependency yields None, which is the branch this pins. Overriding it makes the branch
    # unreachable even though the project HAS an app.
    headers, user = await _auth(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    await AppRegistryFactory.create(db_session, user_id=user.id, project_id=project.id)
    await db_session.commit()

    resp = await client.request(
        "DELETE",
        f"/v1/projects/{project.id}",
        headers=headers,
        json=DELETE_BODY,
    )
    assert resp.status_code == 200
    assert await db_session.get(Project, project.id) is None


# --- the cascade service, directly --------------------------------------------


async def test_cascade_deletes_rows_and_returns_blob_keys(db_session) -> None:
    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    app = await AppRegistryFactory.create(db_session, user_id=user.id, project_id=project.id)
    conv = await ConversationFactory.create(db_session, user.id, project_id=project.id)
    att_id = await _attachment(db_session, user.id, "att/k2", media_type=PPTX_MEDIA_TYPE)
    await MessageFactory.create(
        db_session,
        user.id,
        conv.id,
        payload=_ref_payload(att_id),
    )

    cleanup = await delete_project_cascade(db_session, project, FakeStorage(), user_id=user.id)

    assert await db_session.get(AppRegistry, app.id) is None
    assert await db_session.get(Conversation, conv.id) is None
    assert await db_session.get(Project, project.id) is None
    # BOTH bundle keys, the saved snapshot and its crash-recovery twin: each holds the app's
    # whole source tree, so a cascade that forgets one leaves a deleted project's code in
    # Blob with no owning row and nothing that lists it.
    assert set(cleanup.blob_keys) == {
        snapshot_key(app.id),
        recovery_key(app.id),
        "att/k2",
    }
    assert cleanup.app_container_ids == [app.id]


async def test_cascade_batches_many_conversations_and_dedups_shared_attachment(db_session) -> None:
    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    shared = await _attachment(db_session, user.id, "att/shared")  # image → no pdf sibling
    deck = await _attachment(db_session, user.id, "att/deck", media_type=PPTX_MEDIA_TYPE)

    conv_a = await ConversationFactory.create(db_session, user.id, project_id=project.id)
    await MessageFactory.create(
        db_session,
        user.id,
        conv_a.id,
        payload=_ref_payload(shared),
    )
    conv_b = await ConversationFactory.create(db_session, user.id, project_id=project.id)
    await MessageFactory.create(
        db_session,
        user.id,
        conv_b.id,
        payload=_ref_payload(deck),
    )
    # A THIRD conversation reuses the shared attachment: the batched union has to dedup it up
    # front, since nothing deletes the row between conversations any more.
    conv_c = await ConversationFactory.create(db_session, user.id, project_id=project.id)
    await MessageFactory.create(
        db_session,
        user.id,
        conv_c.id,
        payload=_ref_payload(shared),
    )

    cleanup = await delete_project_cascade(db_session, project, FakeStorage(), user_id=user.id)

    for conv in (conv_a, conv_b, conv_c):
        assert await db_session.get(Conversation, conv.id) is None
    assert await db_session.get(Project, project.id) is None
    assert await db_session.scalar(select(Attachment).where(Attachment.user_id == user.id)) is None
    # Shared key returned exactly once; the deck contributes its blob + derived `.pdf`.
    # ONE KEY PER ROW. A .pptx used to carry a derived `{key}.pdf` from the converter,
    # so a cascade had to sweep both. Nothing derives anything from an attachment now.
    assert sorted(cleanup.blob_keys) == ["att/deck", "att/shared"]
    assert cleanup.app_container_ids == []  # this project has no app → no container to sweep


async def test_cascade_gathers_every_submission_key(db_session) -> None:
    import uuid as _uuid

    from src.services.storage import submission_key, submissions_prefix

    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    app = await AppRegistryFactory.create(db_session, user_id=user.id, project_id=project.id)
    store = FakeStorage()
    sub_keys = {submission_key(app.id, _uuid.uuid4()) for _ in range(3)}
    for key in sub_keys:
        store.objects[key] = b"# v2 git bundle\nretained"
    # A bystander under a DIFFERENT app's prefix: it must not be gathered.
    store.objects[f"submissions/{_uuid.uuid4()}/{_uuid.uuid4()}.bundle"] = b"bystander"

    cleanup = await delete_project_cascade(db_session, project, store, user_id=user.id)
    assert set(cleanup.blob_keys) == {snapshot_key(app.id), recovery_key(app.id), *sub_keys}
    assert all(k.startswith(submissions_prefix(app.id)) for k in sub_keys)


async def test_cascade_storage_error_during_gather_rolls_back(db_session) -> None:
    from src.services.storage.errors import StorageError

    class _ExplodingListStorage(FakeStorage):
        async def list(self, prefix, *, page_size=1000, token=None):
            raise StorageError("listing blew up", provider="fake", key=prefix)

    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    app = await AppRegistryFactory.create(db_session, user_id=user.id, project_id=project.id)

    savepoint = await db_session.begin_nested()
    with pytest.raises(StorageError):
        await delete_project_cascade(db_session, project, _ExplodingListStorage(), user_id=user.id)
    await savepoint.rollback()

    assert await db_session.scalar(select(Project).where(Project.id == project.id)) is not None
    assert await db_session.scalar(select(AppRegistry).where(AppRegistry.id == app.id)) is not None


async def test_cascade_rollback_restores_rows(db_session) -> None:
    # The savepoint rolled back here stands in for a mid-cascade DB error or a failed commit.
    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    app = await AppRegistryFactory.create(db_session, user_id=user.id, project_id=project.id)
    conv = await ConversationFactory.create(db_session, user.id, project_id=project.id)
    app_id, conv_id, project_id = app.id, conv.id, project.id

    savepoint = await db_session.begin_nested()
    await delete_project_cascade(db_session, project, FakeStorage(), user_id=user.id)
    await savepoint.rollback()

    # Re-query at the DB level: `db_session.get` would answer from the identity map rather
    # than from the database, and prove nothing about what the rollback restored.
    assert await db_session.scalar(select(Project).where(Project.id == project_id)) is not None
    assert await db_session.scalar(select(AppRegistry).where(AppRegistry.id == app_id)) is not None
    assert (
        await db_session.scalar(select(Conversation).where(Conversation.id == conv_id)) is not None
    )


async def test_cascade_owner_scoped(db_session) -> None:
    owner = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, owner.id)
    app = await AppRegistryFactory.create(db_session, user_id=owner.id, project_id=project.id)
    stranger = await UserFactory.create(db_session)

    cleanup = await delete_project_cascade(db_session, project, FakeStorage(), user_id=stranger.id)
    assert cleanup.blob_keys == []
    assert cleanup.app_container_ids == []
    assert await db_session.get(AppRegistry, app.id) is not None


# --- the blob-stranding race between the pre-commit gather and the commit -----------------
#
# ROUTER-LEVEL BY NECESSITY. The window these pin only exists around the router's
# `await db.commit()`; a service-level test would pass just as green against a still-
# pre-commit re-walk and prove nothing.


class _MidDeleteWriteStorage(FakeStorage):
    """A `FakeStorage` that drops extra objects into the store immediately AFTER its Nth
    `list` call returns — standing in for a `submit` bundle landing mid-delete.

    Call 1 is the cascade's pre-commit gather; call 2 is the post-commit re-walk. Injecting
    after call 1 lands a bundle in the window the re-walk closes; after call 2, in the
    residual window nothing closes."""

    def __init__(self, *, inject_after_list_call: int, objects: dict[str, bytes]) -> None:
        super().__init__()
        self.list_calls = 0
        self._inject_after = inject_after_list_call
        self._inject = objects

    async def list(self, prefix, *, page_size=1000, token=None):
        page = await super().list(prefix, page_size=page_size, token=token)
        self.list_calls += 1
        if self.list_calls == self._inject_after:
            self.objects.update(self._inject)
        return page


def _override_storage(app, store) -> None:
    from src.api.v1.attachments.router import storage_dependency

    app.dependency_overrides[storage_dependency] = lambda: store


async def _project_with_app(db_session):
    headers, user = await _auth(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    app_row = await AppRegistryFactory.create(db_session, user_id=user.id, project_id=project.id)
    await db_session.commit()
    return headers, user, project, app_row


def _under(store, app_id) -> list[str]:
    from src.services.storage import submissions_prefix

    return sorted(k for k in store.objects if k.startswith(submissions_prefix(app_id)))


async def test_delete_sweeps_a_bundle_written_between_the_gather_and_the_commit(
    app, client, db_session
) -> None:
    # Mutation check: revert the router's `resweep_submission_prefixes` call and this goes red
    # while every other cascade test stays green.
    from src.services.storage import submission_key

    headers, _user, project, app_row = await _project_with_app(db_session)
    late = submission_key(app_row.id, uuid.uuid4())
    store = _MidDeleteWriteStorage(inject_after_list_call=1, objects={late: b"# v2 git bundle"})
    _override_storage(app, store)

    resp = await client.request(
        "DELETE",
        f"/v1/projects/{project.id}",
        headers=headers,
        json=DELETE_BODY,
    )

    assert resp.status_code == 200
    assert store.list_calls == 2
    assert _under(store, app_row.id) == []


async def test_delete_sweeps_a_bundle_written_before_the_delete_starts(
    app, client, db_session
) -> None:
    from src.services.storage import submission_key

    headers, _user, project, app_row = await _project_with_app(db_session)
    store = _MidDeleteWriteStorage(inject_after_list_call=0, objects={})
    early = submission_key(app_row.id, uuid.uuid4())
    store.objects[early] = b"# v2 git bundle"
    store.objects[snapshot_key(app_row.id)] = b"bundle"
    _override_storage(app, store)

    resp = await client.request(
        "DELETE",
        f"/v1/projects/{project.id}",
        headers=headers,
        json=DELETE_BODY,
    )

    assert resp.status_code == 200
    assert _under(store, app_row.id) == []
    assert snapshot_key(app_row.id) not in store.objects


async def test_delete_fails_and_keeps_the_project_when_the_pre_commit_gather_raises(
    app, client, db_session
) -> None:
    # The savepoint stands in for the rollback `get_db` owns in production: this test's db
    # override deliberately does not roll back.
    from src.services.storage.errors import StorageError

    class _ExplodingListStorage(FakeStorage):
        async def list(self, prefix, *, page_size=1000, token=None):
            raise StorageError("listing blew up", provider="fake", key=prefix)

    headers, _user, project, _app_row = await _project_with_app(db_session)
    _override_storage(app, _ExplodingListStorage())

    savepoint = await db_session.begin_nested()
    with pytest.raises(StorageError):
        await client.request(
            "DELETE",
            f"/v1/projects/{project.id}",
            headers=headers,
            json=DELETE_BODY,
        )
    await savepoint.rollback()

    assert await db_session.scalar(select(Project).where(Project.id == project.id)) is not None


async def test_delete_returns_200_and_records_what_survived_when_the_sweep_fails(
    app, client, db_session
) -> None:
    # The POST-commit sweep is best-effort in the opposite direction: the rows are already
    # committed-deleted, so a failing `delete` must never 500 a delete that succeeded.
    #
    # WHAT HAPPENS TO THE KEY INSTEAD. It used to be logged "so a future reconcile has a
    # trail" — there is no reconcile that will collect it: the storage reconciler is
    # operator-invoked, and nothing on this path runs on a timer outside production. So the arm
    # raises the pinned survival alarm AND the delete files one audit row naming the key, which
    # is the artefact an operator can act on.
    from structlog.testing import capture_logs

    from src.core.alarms import TEARDOWN_ARTEFACT_SURVIVED_EVENT
    from src.db.models.audit import AuditLog
    from src.services.storage import submission_key

    class _ExplodingDeleteStorage(FakeStorage):
        async def delete(self, key):
            raise RuntimeError("blob delete boom")

    headers, _user, project, app_row = await _project_with_app(db_session)
    store = _ExplodingDeleteStorage()
    doomed = submission_key(app_row.id, uuid.uuid4())
    store.objects[doomed] = b"# v2 git bundle"
    _override_storage(app, store)

    with capture_logs() as logs:
        resp = await client.request(
            "DELETE",
            f"/v1/projects/{project.id}",
            headers=headers,
            json=DELETE_BODY,
        )

    assert resp.status_code == 200
    assert await db_session.get(Project, project.id) is None
    survived = [
        entry["artefact_id"]
        for entry in logs
        if entry["event"] == TEARDOWN_ARTEFACT_SURVIVED_EVENT and entry["artefact"] == "blob"
    ]
    assert doomed in survived
    record = await db_session.scalar(
        select(AuditLog).where(
            AuditLog.action == "project:teardown-incomplete",
            AuditLog.resource_id == str(project.id),
        )
    )
    assert record is not None
    assert {"artefact": "blob", "id": doomed} in record.detail["survived"]


async def test_resweep_continues_past_an_app_whose_re_walk_raises(db_session) -> None:
    # A project owns exactly ONE app by construction (`uq_app_registry_project` enforces it), so
    # the multi-app loop cannot be reached through the route at all: it is driven here on the
    # service with two hand-built app ids, the FIRST of which fails.
    from structlog.testing import capture_logs

    from src.services.projects import resweep_submission_prefixes
    from src.services.storage import submission_key
    from src.services.storage.errors import StorageError

    app_boom, app_ok = uuid.uuid4(), uuid.uuid4()
    key_ok = submission_key(app_ok, uuid.uuid4())

    class _OneAppListExplodes(FakeStorage):
        async def list(self, prefix, *, page_size=1000, token=None):
            if prefix.startswith(f"submissions/{app_boom}/"):
                raise StorageError("re-walk list blew up", provider="fake", key=prefix)
            return await super().list(prefix, page_size=page_size, token=token)

    store = _OneAppListExplodes()
    store.objects[key_ok] = b"# v2 git bundle"

    with capture_logs() as logs:
        keys = await resweep_submission_prefixes(store, [app_boom, app_ok])

    assert any(entry["event"] == "post_commit_submission_resweep_failed" for entry in logs)
    assert key_ok in keys


async def test_delete_returns_200_and_logs_when_the_re_walk_list_raises(
    app, client, db_session
) -> None:
    from structlog.testing import capture_logs

    from src.services.storage import submission_key
    from src.services.storage.errors import StorageError

    class _ReWalkListExplodes(FakeStorage):
        def __init__(self) -> None:
            super().__init__()
            self.list_calls = 0

        async def list(self, prefix, *, page_size=1000, token=None):
            self.list_calls += 1
            if self.list_calls > 1:  # call 1 = pre-commit gather (ok); call 2 = re-walk (boom)
                raise StorageError("re-walk list blew up", provider="fake", key=prefix)
            return await super().list(prefix, page_size=page_size, token=token)

    headers, _user, project, app_row = await _project_with_app(db_session)
    store = _ReWalkListExplodes()
    bundle = submission_key(app_row.id, uuid.uuid4())
    store.objects[bundle] = b"# v2 git bundle"
    _override_storage(app, store)

    with capture_logs() as logs:
        resp = await client.request(
            "DELETE",
            f"/v1/projects/{project.id}",
            headers=headers,
            json=DELETE_BODY,
        )

    assert resp.status_code == 200
    assert await db_session.get(Project, project.id) is None
    assert any(entry["event"] == "post_commit_submission_resweep_failed" for entry in logs)
    assert store.list_calls == 2
    assert bundle not in store.objects


async def test_the_re_walk_pages_past_the_first_page(app, client, db_session) -> None:
    from src.services.storage import submission_key
    from src.services.storage.constants import DEFAULT_PAGE_SIZE

    headers, _user, project, app_row = await _project_with_app(db_session)
    late = {
        submission_key(app_row.id, uuid.uuid4()): b"# v2 git bundle"
        for _ in range(DEFAULT_PAGE_SIZE + 1)
    }
    assert len(late) == DEFAULT_PAGE_SIZE + 1  # uuid4 collision would silently weaken this
    store = _MidDeleteWriteStorage(inject_after_list_call=1, objects=late)
    _override_storage(app, store)

    resp = await client.request(
        "DELETE",
        f"/v1/projects/{project.id}",
        headers=headers,
        json=DELETE_BODY,
    )

    assert resp.status_code == 200
    assert _under(store, app_row.id) == []
    assert store.list_calls == 3  # gather + two re-walk pages


async def test_the_re_walk_never_reaches_another_users_app(app, client, db_session) -> None:
    # `AppRegistry` carries no name column and the prefix is keyed on `app_id` alone, so the
    # identity that matters here is the app id, never a name.
    from src.services.storage import submission_key

    headers_a, _user_a, project_a, app_a = await _project_with_app(db_session)
    user_b = await UserFactory.create(db_session)
    project_b = await ProjectFactory.create(db_session, user_b.id)
    app_b = await AppRegistryFactory.create(db_session, user_id=user_b.id, project_id=project_b.id)
    await db_session.commit()

    store = FakeStorage()
    key_a = submission_key(app_a.id, uuid.uuid4())
    key_b = submission_key(app_b.id, uuid.uuid4())
    store.objects[key_a] = b"# v2 git bundle"
    store.objects[key_b] = b"# v2 git bundle"
    _override_storage(app, store)

    resp = await client.request(
        "DELETE",
        f"/v1/projects/{project_a.id}",
        headers=headers_a,
        json=DELETE_BODY,
    )

    assert resp.status_code == 200
    assert key_a not in store.objects
    assert store.objects[key_b] == b"# v2 git bundle"
    assert await db_session.get(AppRegistry, app_b.id) is not None


async def test_a_bundle_written_after_the_re_walk_survives_the_delete(
    app, client, db_session
) -> None:
    # PINS A KNOWN-OPEN GAP, not a bug report. `submit` puts its bundle before the guarded
    # UPDATE and `delete_project` takes no submit interlock, so a write landing after the
    # post-commit re-walk is swept by nothing, and the reconciling sweep is report-only on
    # this prefix until a retention policy is decided — an operator reclaims it by hand.
    from src.services.storage import submission_key

    headers, _user, project, app_row = await _project_with_app(db_session)
    too_late = submission_key(app_row.id, uuid.uuid4())
    store = _MidDeleteWriteStorage(
        inject_after_list_call=2, objects={too_late: b"# v2 git bundle"}
    )
    _override_storage(app, store)

    resp = await client.request(
        "DELETE",
        f"/v1/projects/{project.id}",
        headers=headers,
        json=DELETE_BODY,
    )

    assert resp.status_code == 200
    assert store.list_calls == 2  # the re-walk DID run — the write simply landed after it
    assert await db_session.get(AppRegistry, app_row.id) is None
    assert store.objects[too_late] == b"# v2 git bundle"
    assert _under(store, app_row.id) == [too_late]


def test_the_cascade_docstring_does_not_over_claim_the_residual_window() -> None:
    doc = delete_project_cascade.__doc__ or ""
    assert "surfaced, not closed" in doc
    assert "does not eliminate it" in doc
    assert "D7" in doc
    lowered = doc.lower()
    for over_claim in ("handled", "fully closes", "closes the window", "no bundle can be"):
        assert over_claim not in lowered, f"docstring over-claims coverage: {over_claim!r}"


# --- refusing a project delete while a build session is live -------------------------------
#
# APP-SCOPED. The lock carries no app or project axis, so the guard recovers the live
# session's app identity from the sandbox registry hash instead.


def _lock(user_id):
    from src.services.redis.keys import lock_key

    return lock_key(user_id)


async def _live_session_for(fake_redis, user_id, app_id) -> None:
    """The state a real live build leaves behind: the lock held, plus a registry hash naming
    the app being built."""
    from src.services.build_sessions import app_name_for
    from src.services.redis.keys import (
        REGISTRY_FIELD_APP_NAME,
        REGISTRY_FIELD_FQDN,
        REGISTRY_FIELD_STATE,
        registry_key,
    )

    await fake_redis.set(_lock(user_id), "holder-token")
    await fake_redis.hset(
        registry_key(user_id),
        mapping={
            REGISTRY_FIELD_APP_NAME: app_name_for(app_id),
            REGISTRY_FIELD_FQDN: "sbx.example.net",
            REGISTRY_FIELD_STATE: "ready",
        },
    )


async def test_delete_is_refused_while_this_projects_app_is_building(
    app, client, db_session, fake_redis
) -> None:
    # REFUSE rather than force: snapshots are written only at finalize, so forcing the delete
    # would destroy every file change since the last one with no signal that it was unsaved.
    from src.services.storage import submission_key

    headers, user, project, app_row = await _project_with_app(db_session)
    store = FakeStorage()
    bundle_key = submission_key(app_row.id, uuid.uuid4())
    store.objects[bundle_key] = b"# v2 git bundle"
    store.objects[snapshot_key(app_row.id)] = b"# v2 git bundle"
    _override_storage(app, store)
    await _live_session_for(fake_redis, user.id, app_row.id)

    resp = await client.request(
        "DELETE",
        f"/v1/projects/{project.id}",
        headers=headers,
        json=DELETE_BODY,
    )

    assert resp.status_code == 409
    assert "build session" in resp.json()["error"]["message"]
    assert "end it before deleting" in resp.json()["error"]["message"]
    assert await db_session.get(Project, project.id) is not None
    assert await db_session.get(AppRegistry, app_row.id) is not None
    assert store.objects[bundle_key] == b"# v2 git bundle"
    assert snapshot_key(app_row.id) in store.objects


async def test_delete_proceeds_when_no_build_session_is_live(
    app, client, db_session, fake_redis
) -> None:
    headers, _user, project, app_row = await _project_with_app(db_session)
    store = FakeStorage()
    store.objects[snapshot_key(app_row.id)] = b"# v2 git bundle"
    _override_storage(app, store)

    resp = await client.request(
        "DELETE",
        f"/v1/projects/{project.id}",
        headers=headers,
        json=DELETE_BODY,
    )

    assert resp.status_code == 200
    assert await db_session.get(Project, project.id) is None
    assert snapshot_key(app_row.id) not in store.objects


async def test_a_live_session_in_another_project_does_not_block_this_delete(
    app, client, db_session, fake_redis
) -> None:
    # THE CASE THAT ACTUALLY BREAKS with a naive guard. One app per project + many projects
    # per user means a bare `lock_is_held(redis, user_id)` would 409 this delete because the
    # SAME user happens to be building something else.
    # Mutation check: drop the `app_id=` argument at the call site and this goes red while every
    # other live-build-session-lock test in this section stays green.
    headers, user, project_b, app_b = await _project_with_app(db_session)
    project_a = await ProjectFactory.create(db_session, user.id)
    app_a = await AppRegistryFactory.create(db_session, user_id=user.id, project_id=project_a.id)
    await db_session.commit()
    _override_storage(app, FakeStorage())
    await _live_session_for(fake_redis, user.id, app_a.id)

    resp = await client.request(
        "DELETE",
        f"/v1/projects/{project_b.id}",
        headers=headers,
        json=DELETE_BODY,
    )

    assert resp.status_code == 200
    assert await db_session.get(Project, project_b.id) is None
    assert await db_session.get(AppRegistry, app_b.id) is None
    assert await fake_redis.exists(_lock(user.id))
    assert await db_session.get(Project, project_a.id) is not None


async def test_delete_is_503_when_redis_errors_during_the_guard(
    app, client, db_session, fake_redis, monkeypatch
) -> None:
    import src.services.build_sessions as build_sessions
    from src.services.redis import BUILD_COORDINATION_UNAVAILABLE_MSG

    async def _boom(_redis, _user_uuid):
        raise RedisError("redis blip")

    monkeypatch.setattr(build_sessions, "lock_is_held", _boom)
    headers, _user, project, app_row = await _project_with_app(db_session)
    store = FakeStorage()
    store.objects[snapshot_key(app_row.id)] = b"# v2 git bundle"
    _override_storage(app, store)

    resp = await client.request(
        "DELETE",
        f"/v1/projects/{project.id}",
        headers=headers,
        json=DELETE_BODY,
    )

    assert resp.status_code == 503
    assert resp.json()["error"]["message"] == BUILD_COORDINATION_UNAVAILABLE_MSG
    assert await db_session.get(Project, project.id) is not None
    assert snapshot_key(app_row.id) in store.objects


async def test_delete_is_503_when_the_registry_read_errors_during_the_guard(
    app, client, db_session, fake_redis, monkeypatch
) -> None:
    import src.services.build_sessions as build_sessions
    from src.services.redis import BUILD_COORDINATION_UNAVAILABLE_MSG

    async def _boom(_redis, _user_uuid):
        raise RedisError("registry read blip")

    monkeypatch.setattr(build_sessions, "read_registry", _boom)
    headers, user, project, app_row = await _project_with_app(db_session)
    _override_storage(app, FakeStorage())
    await fake_redis.set(_lock(user.id), "holder-token")

    resp = await client.request(
        "DELETE",
        f"/v1/projects/{project.id}",
        headers=headers,
        json=DELETE_BODY,
    )

    assert resp.status_code == 503
    assert resp.json()["error"]["message"] == BUILD_COORDINATION_UNAVAILABLE_MSG
    assert await db_session.get(Project, project.id) is not None


async def test_delete_proceeds_when_redis_is_not_configured(app, client, db_session) -> None:
    # NO `fake_redis` fixture, deliberately: a fixture that always binds a client makes this
    # branch unreachable. With no Redis configured there is no build-session subsystem at
    # all, so no lock CAN be held and the delete proceeds on a certain answer.
    headers, _user, project, app_row = await _project_with_app(db_session)
    store = FakeStorage()
    store.objects[snapshot_key(app_row.id)] = b"# v2 git bundle"
    _override_storage(app, store)

    resp = await client.request(
        "DELETE",
        f"/v1/projects/{project.id}",
        headers=headers,
        json=DELETE_BODY,
    )

    assert resp.status_code == 200
    assert await db_session.get(Project, project.id) is None


async def test_delete_fails_closed_when_the_lock_names_no_app(
    app, client, db_session, fake_redis
) -> None:
    # FAIL CLOSED on ambiguity. A held lock with no resolvable registry is a REAL state, not a
    # hypothetical: `ensure_sandbox` takes the lock BEFORE provisioning the container that
    # writes the registry hash, so every turn that allocates a workspace passes through this
    # window (as did the deleted `_start_locked` before it). Proceeding here would land the
    # delete mid-provision — a silent race — so it refuses instead.
    headers, user, project, app_row = await _project_with_app(db_session)
    _override_storage(app, FakeStorage())
    await fake_redis.set(_lock(user.id), "holder-token")  # lock held, NO registry hash

    resp = await client.request(
        "DELETE",
        f"/v1/projects/{project.id}",
        headers=headers,
        json=DELETE_BODY,
    )

    assert resp.status_code == 409
    assert await db_session.get(Project, project.id) is not None
    assert await db_session.get(AppRegistry, app_row.id) is not None


async def test_delete_fails_closed_when_the_registry_carries_no_app_name(
    app, client, db_session, fake_redis
) -> None:
    from src.services.redis.keys import REGISTRY_FIELD_STATE, registry_key

    headers, user, project, _app_row = await _project_with_app(db_session)
    _override_storage(app, FakeStorage())
    await fake_redis.set(_lock(user.id), "holder-token")
    await fake_redis.hset(registry_key(user.id), REGISTRY_FIELD_STATE, "ready")

    resp = await client.request(
        "DELETE",
        f"/v1/projects/{project.id}",
        headers=headers,
        json=DELETE_BODY,
    )

    assert resp.status_code == 409
    assert await db_session.get(Project, project.id) is not None


async def test_delete_of_an_app_less_project_is_never_blocked_by_a_live_build(
    app, client, db_session, fake_redis
) -> None:
    headers, user = await _auth(db_session)
    empty = await ProjectFactory.create(db_session, user.id)
    other = await ProjectFactory.create(db_session, user.id)
    building = await AppRegistryFactory.create(db_session, user_id=user.id, project_id=other.id)
    await db_session.commit()
    _override_storage(app, FakeStorage())
    await _live_session_for(fake_redis, user.id, building.id)

    resp = await client.request(
        "DELETE",
        f"/v1/projects/{empty.id}",
        headers=headers,
        json=DELETE_BODY,
    )

    assert resp.status_code == 200
    assert await db_session.get(Project, empty.id) is None


async def test_a_relaunched_preview_is_torn_down_with_the_project_it_was_serving(
    app, client, db_session, fake_redis
) -> None:
    # A relaunched preview holds NO lock by design (`relaunch_preview`'s scope releases it on
    # exit; the container's lifetime is owned by the `preview_stay_until` lease instead), so
    # `refuse_while_build_session_live` returns without refusing and the delete proceeds. The
    # post-commit reap then asks the registry whether it still names THIS project's container
    # and hands it to `reap_user`, so the container, its registry record and its stay all go
    # with the project.
    from datetime import UTC, datetime, timedelta

    from src.api.v1.build_sessions.deps import sandbox_or_none_dependency
    from src.services.build_sessions import app_name_for
    from src.services.redis.keys import (
        REGISTRY_FIELD_APP_NAME,
        REGISTRY_FIELD_FQDN,
        REGISTRY_FIELD_PREVIEW_STAY_UNTIL,
        REGISTRY_FIELD_STATE,
        registry_key,
    )
    from tests.fakes import FakeSandboxClient

    headers, user, project, app_row = await _project_with_app(db_session)
    _override_storage(app, FakeStorage())
    # THE SANDBOX HAS TO BE WIRED IN, and that is not test scaffolding — it is the arm under
    # test. `.env.test` carries no `SANDBOX__*`, so `OptionalSandbox` resolves to `None` and
    # the reap is skipped; without this override the test would pass while asserting nothing.
    sandbox = FakeSandboxClient()
    app.dependency_overrides[sandbox_or_none_dependency] = lambda: sandbox
    # Exactly what a relaunch leaves behind: a READY registry entry under a live stay, and
    # NO lock.
    stay_until = datetime.now(UTC) + timedelta(minutes=30)
    await fake_redis.hset(
        registry_key(user.id),
        mapping={
            REGISTRY_FIELD_APP_NAME: app_name_for(app_row.id),
            REGISTRY_FIELD_FQDN: "sbx.example.net",
            REGISTRY_FIELD_STATE: "ready",
            REGISTRY_FIELD_PREVIEW_STAY_UNTIL: stay_until.isoformat(),
        },
    )
    assert not await fake_redis.exists(_lock(user.id))

    resp = await client.request(
        "DELETE",
        f"/v1/projects/{project.id}",
        headers=headers,
        json=DELETE_BODY,
    )

    # The delete still PROCEEDS — the guard never fires, because there is no lock to see.
    assert resp.status_code == 200
    assert await db_session.get(Project, project.id) is None
    # ...and the container goes with it: the ARM delete was called for this app's name, and the
    # registry hash — which carried the stay — is gone entirely.
    assert sandbox.torn_down == [app_name_for(app_row.id)]
    assert await fake_redis.hgetall(registry_key(user.id)) == {}


def test_delete_project_documents_409_and_503_in_openapi() -> None:
    spec = create_app().openapi()
    responses = spec["paths"]["/v1/projects/{project_id}"]["delete"]["responses"]
    assert "409" in responses
    assert "503" in responses
