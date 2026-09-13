"""Sharing business logic (#198 R1-R11) — `services/projects/shares.py` exercised directly
against a real DB session, isolated from the HTTP layer so idempotency, self-share refusal,
and the snapshot-presence gate are pinned independent of how the router wires them.

`create_share` calls `snapshot_presence`, which resolves through the object-store accessor
singleton — same seam `test_relaunch_claim.py` binds a fake store to, not a FastAPI dependency
override (this module has no `app` in scope).
"""

from __future__ import annotations

import pytest

from src.core.errors import AppApiError
from src.services.build_sessions.appdata import resolve_app_for_project
from src.services.projects.shares import (
    create_share,
    list_shared_with_me,
    list_shares_for_project,
    revoke_share,
    search_colleagues,
)
from src.services.storage import accessor as storage_accessor
from src.services.storage import snapshot_key
from tests.factories import ProjectFactory, UserFactory
from tests.fakes import FakeStorage


@pytest.fixture
def bind_store(monkeypatch: pytest.MonkeyPatch):
    def _bind(store: FakeStorage) -> FakeStorage:
        monkeypatch.setattr(storage_accessor, "_backend_singleton", store)
        return store

    return _bind


async def _saved_project(db_session, bind_store, *, owner_email="owner@example.com", store=None):
    """An owner with a project that has a saved snapshot — the one precondition `create_share`
    gates on (R10).

    `store` lets a caller minting a SECOND project reuse the first's — `bind_store` REPLACES
    `storage_accessor`'s singleton wholesale, so binding a second fresh `FakeStorage()` in the
    same test does not add to the first project's store, it makes it unreachable, and a share
    on that first project then refuses with "nothing saved" against a store that never held it."""
    if store is None:
        store = bind_store(FakeStorage())
    owner = await UserFactory.create(db_session, email=owner_email)
    project = await ProjectFactory.create(db_session, owner.id, description="A project")
    app_id = await resolve_app_for_project(db_session, owner.id, project.id)
    await db_session.flush()
    await store.put(snapshot_key(app_id), b"BUNDLE")
    return project, owner, store


async def test_sharing_with_yourself_is_refused(db_session, bind_store) -> None:
    project, owner, _store = await _saved_project(db_session, bind_store)
    with pytest.raises(AppApiError) as exc_info:
        await create_share(db_session, project=project, actor_id=owner.id, colleague_id=owner.id)
    assert exc_info.value.status_code == 400


async def test_sharing_a_project_with_nothing_saved_is_refused(db_session) -> None:
    owner = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, owner.id, description="A project")
    colleague = await UserFactory.create(db_session, email="colleague@example.com")

    with pytest.raises(AppApiError) as exc_info:
        await create_share(
            db_session, project=project, actor_id=owner.id, colleague_id=colleague.id
        )
    assert exc_info.value.status_code == 400
    assert exc_info.value.code == "no_saved_snapshot"


async def test_sharing_a_project_with_only_an_autosave_is_still_refused(
    db_session, bind_store
) -> None:
    """R10 gates on `snapshot_presence` specifically, never `restorable_presence` — the shared
    runtime only ever restores from the SAVED bundle (R21), so a share backed only by an
    autosave would hand a recipient nothing launchable."""
    from src.services.storage import recovery_key

    store = bind_store(FakeStorage())
    owner = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, owner.id, description="A project")
    app_id = await resolve_app_for_project(db_session, owner.id, project.id)
    await db_session.flush()
    await store.put(recovery_key(app_id), b"AUTOSAVE ONLY")  # no snapshot_key put
    colleague = await UserFactory.create(db_session, email="colleague@example.com")

    with pytest.raises(AppApiError) as exc_info:
        await create_share(
            db_session, project=project, actor_id=owner.id, colleague_id=colleague.id
        )
    assert exc_info.value.status_code == 400
    assert exc_info.value.code == "no_saved_snapshot"


async def test_a_normal_share_succeeds_and_is_returned(db_session, bind_store) -> None:
    project, owner, _store = await _saved_project(db_session, bind_store)
    colleague = await UserFactory.create(db_session, email="colleague@example.com")

    share = await create_share(
        db_session, project=project, actor_id=owner.id, colleague_id=colleague.id
    )
    assert share.project_id == project.id
    assert share.shared_with_user_id == colleague.id


async def test_re_sharing_the_same_colleague_is_idempotent(db_session, bind_store) -> None:
    project, owner, _store = await _saved_project(db_session, bind_store)
    colleague = await UserFactory.create(db_session, email="colleague@example.com")

    first = await create_share(
        db_session, project=project, actor_id=owner.id, colleague_id=colleague.id
    )
    await db_session.flush()
    second = await create_share(
        db_session, project=project, actor_id=owner.id, colleague_id=colleague.id
    )
    assert first.id == second.id

    rows = await list_shares_for_project(db_session, project.id)
    assert len(rows) == 1


async def test_revoking_a_share_that_exists_returns_true_and_removes_it(
    db_session, bind_store
) -> None:
    project, owner, _store = await _saved_project(db_session, bind_store)
    colleague = await UserFactory.create(db_session, email="colleague@example.com")
    await create_share(db_session, project=project, actor_id=owner.id, colleague_id=colleague.id)
    await db_session.flush()

    revoked = await revoke_share(
        db_session, project=project, actor_id=owner.id, colleague_id=colleague.id
    )
    assert revoked is True
    assert await list_shares_for_project(db_session, project.id) == []


async def test_revoking_a_share_that_never_existed_returns_false_not_an_error(
    db_session,
) -> None:
    # Mirrors `release_project_sandbox`'s "released: false is a success" posture — a
    # double-click/retry, not an error.
    owner = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, owner.id, description="A project")
    colleague = await UserFactory.create(db_session, email="colleague@example.com")

    revoked = await revoke_share(
        db_session, project=project, actor_id=owner.id, colleague_id=colleague.id
    )
    assert revoked is False


async def test_colleague_search_matches_the_start_of_a_display_name_token(db_session) -> None:
    requester = await UserFactory.create(db_session)
    match = await UserFactory.create(db_session, email="ana@example.com", display_name="Ana Reyes")
    # "Reyes" is the SECOND token — proves this is a token-start match, not first-word-only.
    results = await search_colleagues(db_session, requester_id=requester.id, query="Rey")
    assert [u.id for u in results] == [match.id]


async def test_colleague_search_never_matches_a_substring_mid_token(db_session) -> None:
    """The anchored-match requirement (R4): a substring match on a tenant where every user
    shares one email domain would match every user in it against any three characters of that
    shared domain."""
    requester = await UserFactory.create(db_session)
    await UserFactory.create(db_session, email="someone@example.com", display_name="Barnabas")
    # "arna" is a mid-token substring of "Barnabas" — must NOT match.
    results = await search_colleagues(db_session, requester_id=requester.id, query="arn")
    assert results == []


async def test_colleague_search_matches_the_start_of_the_email_local_part(db_session) -> None:
    requester = await UserFactory.create(db_session)
    match = await UserFactory.create(db_session, email="priya.k@example.com", display_name=None)
    results = await search_colleagues(db_session, requester_id=requester.id, query="priy")
    assert [u.id for u in results] == [match.id]


async def test_colleague_search_excludes_the_requester(db_session) -> None:
    requester = await UserFactory.create(db_session, display_name="Selfsame")
    results = await search_colleagues(db_session, requester_id=requester.id, query="Self")
    assert results == []


async def test_shared_with_me_lists_newest_grant_first(db_session, bind_store) -> None:
    project_a, owner, store = await _saved_project(
        db_session, bind_store, owner_email="owner-a@example.com"
    )
    # THE SAME STORE AS PROJECT A, NOT A SECOND `bind_store(FakeStorage())` — that call REPLACES
    # `storage_accessor`'s singleton wholesale rather than adding to it, which made project A's
    # already-saved snapshot unreachable and its `create_share` below refuse with "nothing saved"
    # against a store that never held it in the first place.
    project_b = await ProjectFactory.create(db_session, owner.id, description="Second project")
    app_id_b = await resolve_app_for_project(db_session, owner.id, project_b.id)
    await db_session.flush()
    await store.put(snapshot_key(app_id_b), b"BUNDLE")

    colleague = await UserFactory.create(db_session, email="colleague@example.com")
    await create_share(db_session, project=project_a, actor_id=owner.id, colleague_id=colleague.id)
    await db_session.flush()
    await create_share(db_session, project=project_b, actor_id=owner.id, colleague_id=colleague.id)
    await db_session.flush()

    entries = await list_shared_with_me(db_session, colleague.id, limit=10, cursor=None)
    assert [e.project.id for e in entries] == [project_b.id, project_a.id]


async def test_shared_with_me_is_scoped_to_the_recipient(db_session, bind_store) -> None:
    project, owner, _store = await _saved_project(db_session, bind_store)
    colleague = await UserFactory.create(db_session, email="colleague@example.com")
    stranger = await UserFactory.create(db_session, email="stranger@example.com")
    await create_share(db_session, project=project, actor_id=owner.id, colleague_id=colleague.id)
    await db_session.flush()

    assert await list_shared_with_me(db_session, colleague.id, limit=10, cursor=None) != []
    assert await list_shared_with_me(db_session, stranger.id, limit=10, cursor=None) == []
