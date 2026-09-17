"""Sharing business logic (#198 R1-R11) — `services/projects/shares.py` exercised directly
against a real DB session, isolated from the HTTP layer so idempotency, self-share refusal,
and the snapshot-presence gate are pinned independent of how the router wires them.

`create_share` calls `snapshot_presence`, which resolves through the object-store accessor
singleton — same seam `test_relaunch_claim.py` binds a fake store to, not a FastAPI dependency
override (this module has no `app` in scope).
"""

from __future__ import annotations

import inspect

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
from tests.factories import ProjectFactory, ProjectShareFactory, UserFactory
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


# --- the recipient's list: search, filter, sort, numbered pages ------------------
#
# These seed shares with `ProjectShareFactory` rather than `create_share`: the snapshot gate is
# `create_share`'s subject and is pinned above, and a saved bundle per row would be scaffolding
# none of the assertions below ever look at.


async def _shared_with(db, recipient, owner, *, name: str, description: str | None = None):
    project = await ProjectFactory.create(db, owner.id, name=name, description=description)
    await ProjectShareFactory.create(db, project.id, recipient.id)
    return project


def test_the_shared_list_no_longer_takes_a_keyset_cursor() -> None:
    """The old signature is GONE, not merely unused: a caller still passing `limit=`/`cursor=`
    must fail rather than be silently served page one of an offset walk forever."""
    parameters = inspect.signature(list_shared_with_me).parameters

    assert "cursor" not in parameters
    assert "limit" not in parameters
    assert {"page", "page_size"} <= set(parameters)


async def test_the_list_is_newest_grant_first(db_session) -> None:
    recipient = await UserFactory.create(db_session, email="recipient@example.com")
    owner = await UserFactory.create(db_session, email="owner@example.com")
    first = await _shared_with(db_session, recipient, owner, name="First")
    second = await _shared_with(db_session, recipient, owner, name="Second")

    shared = await list_shared_with_me(db_session, recipient.id, page=1, page_size=10)

    assert [entry.project.id for entry in shared.entries] == [second.id, first.id]
    assert shared.total == 2


async def test_the_list_is_scoped_to_the_recipient(db_session) -> None:
    """★ The `shared_with_user_id` predicate IS the isolation boundary. Drop it and every
    citizen's shared list becomes every share on the platform."""
    recipient = await UserFactory.create(db_session, email="recipient@example.com")
    stranger = await UserFactory.create(db_session, email="stranger@example.com")
    owner = await UserFactory.create(db_session, email="owner@example.com")
    await _shared_with(db_session, recipient, owner, name="Theirs")

    mine = await list_shared_with_me(db_session, recipient.id, page=1, page_size=10)
    theirs = await list_shared_with_me(db_session, stranger.id, page=1, page_size=10)

    assert [entry.project.name for entry in mine.entries] == ["Theirs"]
    assert theirs.entries == []
    assert theirs.total == 0
    # The facet leaks the same way the rows would: a colleague who has shared nothing with THIS
    # recipient must not appear in their filter either.
    assert theirs.sharers == []


async def test_a_revoked_share_is_gone_from_the_next_read(db_session) -> None:
    recipient = await UserFactory.create(db_session, email="recipient@example.com")
    owner = await UserFactory.create(db_session, email="owner@example.com")
    kept = await _shared_with(db_session, recipient, owner, name="Kept")
    revoked = await _shared_with(db_session, recipient, owner, name="Revoked")

    await revoke_share(db_session, project=revoked, actor_id=owner.id, colleague_id=recipient.id)
    await db_session.flush()
    shared = await list_shared_with_me(db_session, recipient.id, page=1, page_size=10)

    assert [entry.project.id for entry in shared.entries] == [kept.id]
    assert shared.total == 1


async def test_search_matches_descriptions_and_not_names(db_session) -> None:
    """Description-only is this product's deliberate scope, the same scope the marketplace
    search has — so the name arm is asserted ABSENT rather than left untested."""
    recipient = await UserFactory.create(db_session, email="recipient@example.com")
    owner = await UserFactory.create(db_session, email="owner@example.com")
    await _shared_with(
        db_session, recipient, owner, name="Trolley Fleet", description="Where the bays stand."
    )
    await _shared_with(
        db_session, recipient, owner, name="Bays Report", description="Fuel uplift per stand."
    )

    shared = await list_shared_with_me(
        db_session, recipient.id, page=1, page_size=10, search="bays"
    )

    assert [entry.project.name for entry in shared.entries] == ["Trolley Fleet"]
    assert shared.total == 1


async def test_the_sharer_facet_counts_each_colleagues_rows(db_session) -> None:
    recipient = await UserFactory.create(db_session, email="recipient@example.com")
    rahul = await UserFactory.create(db_session, email="rahul@example.com", display_name="Rahul")
    varun = await UserFactory.create(db_session, email="varun@example.com", display_name="Varun")
    await _shared_with(db_session, recipient, rahul, name="One")
    await _shared_with(db_session, recipient, rahul, name="Two")
    await _shared_with(db_session, recipient, varun, name="Three")

    shared = await list_shared_with_me(db_session, recipient.id, page=1, page_size=10)

    assert [(facet.display_name, facet.share_count) for facet in shared.sharers] == [
        ("Rahul", 2),
        ("Varun", 1),
    ]
    assert [facet.user_id for facet in shared.sharers] == [rahul.id, varun.id]


async def test_the_facet_keeps_every_colleague_while_one_of_them_is_filtered_on(
    db_session,
) -> None:
    """★ The sharer filter is not applied to its OWN options. Applied, the recipient is left
    holding a filter that offers only the colleague they already picked, with no way back."""
    recipient = await UserFactory.create(db_session, email="recipient@example.com")
    rahul = await UserFactory.create(db_session, email="rahul@example.com", display_name="Rahul")
    varun = await UserFactory.create(db_session, email="varun@example.com", display_name="Varun")
    await _shared_with(db_session, recipient, rahul, name="One")
    await _shared_with(db_session, recipient, varun, name="Two")

    shared = await list_shared_with_me(
        db_session, recipient.id, page=1, page_size=10, shared_by=rahul.id
    )

    assert [entry.project.name for entry in shared.entries] == ["One"]
    assert [facet.user_id for facet in shared.sharers] == [rahul.id, varun.id]


async def test_the_facet_counts_describe_the_search(db_session) -> None:
    """A colleague whose rows the search excluded must not be offered as a filter: picking them
    would produce an empty list from a filter that claimed rows behind it."""
    recipient = await UserFactory.create(db_session, email="recipient@example.com")
    rahul = await UserFactory.create(db_session, email="rahul@example.com", display_name="Rahul")
    varun = await UserFactory.create(db_session, email="varun@example.com", display_name="Varun")
    await _shared_with(db_session, recipient, rahul, name="One", description="Fuel uplift.")
    await _shared_with(db_session, recipient, varun, name="Two", description="Queue readings.")

    shared = await list_shared_with_me(
        db_session, recipient.id, page=1, page_size=10, search="fuel"
    )

    assert [(facet.user_id, facet.share_count) for facet in shared.sharers] == [(rahul.id, 1)]


async def test_two_colleagues_sharing_a_display_name_stay_two_filter_entries(db_session) -> None:
    """★ `users.display_name` is NULLABLE AND NOT UNIQUE. Facet or filter on the name and these
    two collapse into one entry that hands the recipient both people's applications."""
    recipient = await UserFactory.create(db_session, email="recipient@example.com")
    one = await UserFactory.create(db_session, email="r.kohli@example.com", display_name="R Kohli")
    other = await UserFactory.create(
        db_session, email="ravi.k@example.com", display_name="R Kohli"
    )
    await _shared_with(db_session, recipient, one, name="Hers")
    await _shared_with(db_session, recipient, other, name="His")

    shared = await list_shared_with_me(db_session, recipient.id, page=1, page_size=10)
    just_one = await list_shared_with_me(
        db_session, recipient.id, page=1, page_size=10, shared_by=one.id
    )

    assert [facet.display_name for facet in shared.sharers] == ["R Kohli", "R Kohli"]
    assert sorted(facet.user_id for facet in shared.sharers) == sorted([one.id, other.id])
    assert [entry.project.name for entry in just_one.entries] == ["Hers"]


async def test_sorting_by_name_is_alphabetical_regardless_of_grant_order(db_session) -> None:
    recipient = await UserFactory.create(db_session, email="recipient@example.com")
    owner = await UserFactory.create(db_session, email="owner@example.com")
    await _shared_with(db_session, recipient, owner, name="apple")
    await _shared_with(db_session, recipient, owner, name="Zebra")

    by_name = await list_shared_with_me(
        db_session, recipient.id, page=1, page_size=10, sort="name"
    )
    by_grant = await list_shared_with_me(db_session, recipient.id, page=1, page_size=10)

    # `lower()` is what makes this hold under a `C` collation too, where byte order would put
    # every capital ahead of every lowercase.
    assert [entry.project.name for entry in by_name.entries] == ["apple", "Zebra"]
    assert [entry.project.name for entry in by_grant.entries] == ["Zebra", "apple"]


async def test_a_page_past_the_end_is_empty_with_the_real_total(db_session) -> None:
    """What a reader left past the end by a revoke meets: an empty page and a `total` big enough
    to work out which page still exists. A 404 there would be a dead end."""
    recipient = await UserFactory.create(db_session, email="recipient@example.com")
    owner = await UserFactory.create(db_session, email="owner@example.com")
    await _shared_with(db_session, recipient, owner, name="Only")

    shared = await list_shared_with_me(db_session, recipient.id, page=4, page_size=2)

    assert shared.entries == []
    assert shared.total == 1


async def test_a_share_arriving_mid_walk_shifts_the_window_rather_than_losing_a_row(
    db_session,
) -> None:
    """★ THE COST OF OFFSET ON A LIST OTHER PEOPLE WRITE INTO, pinned rather than assumed away.

    Newest-grant-first puts a new share at position 0, so one arriving between two page reads
    pushes every row down by one: the page boundary is seen TWICE, and nothing that existed when
    the walk began is missed. That is the whole of what this shape can promise — the repeat is
    the accepted cost; a loss would not be.

    Turn the order around and the repeat becomes a skip, which is why the exact sequence is
    asserted and not just what the walk contains."""
    recipient = await UserFactory.create(db_session, email="recipient@example.com")
    owner = await UserFactory.create(db_session, email="owner@example.com")
    started_with = [
        (await _shared_with(db_session, recipient, owner, name=f"App {index}")).id
        for index in range(4)
    ]

    first_page = await list_shared_with_me(db_session, recipient.id, page=1, page_size=2)
    await _shared_with(db_session, recipient, owner, name="Arrived mid-walk")
    rest = [
        await list_shared_with_me(db_session, recipient.id, page=page, page_size=2)
        for page in (2, 3)
    ]

    walked = [entry.project.id for shared in [first_page, *rest] for entry in shared.entries]
    assert set(started_with) <= set(walked)
    assert walked == [
        started_with[3],
        started_with[2],
        started_with[2],
        started_with[1],
        started_with[0],
    ]
