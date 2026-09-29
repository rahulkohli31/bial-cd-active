"""Colleague search: our own users first, then directory people who have no user here yet."""

from __future__ import annotations

import uuid

import httpx
import pytest

from src.db.models.user import User
from src.services.directory import client as directory_client
from src.services.projects.shares import find_colleagues
from tests.factories import UserFactory
from tests.fakes import FakeDirectory

_SEARCH_PATH = "/v1.0/users"


async def _member(db_session, fake_directory: FakeDirectory) -> User:
    """A requester the directory holds as a member. The fake answers every search with every
    entry, so this requester also comes back from each directory search."""
    oid = fake_directory.add_user("Quorra Requester", mail="quorra.requester@bial.example")
    return await UserFactory.create(db_session, azure_oid=str(oid))


def _paths_asked(fake_directory: FakeDirectory) -> list[str]:
    return [request.url.path for request in fake_directory.requests]


async def test_a_full_page_of_our_own_users_never_asks_the_directory(
    db_session, fake_directory: FakeDirectory
) -> None:
    requester = await _member(db_session, fake_directory)
    for n in range(10):
        await UserFactory.create(
            db_session, email=f"quorra.{n}@example.com", display_name=f"Quorra {n}"
        )
    fake_directory.add_user("Quorra Outsider", mail="quorra.outsider@bial.example")

    results = await find_colleagues(db_session, requester=requester, query="Quorra")

    assert [result.display_name for result in results] == [f"Quorra {n}" for n in range(10)]
    assert fake_directory.requests == []


async def test_people_already_here_are_dropped_before_the_directory_fills_the_slots_left(
    db_session, fake_directory: FakeDirectory
) -> None:
    requester = await _member(db_session, fake_directory)
    ada_oid, bo_oid = uuid.uuid4(), uuid.uuid4()
    ada = await UserFactory.create(
        db_session,
        azure_oid=str(ada_oid),
        email="quorra.ada@example.com",
        display_name="Quorra Ada",
    )
    bo = await UserFactory.create(
        db_session, azure_oid=str(bo_oid), email="quorra.bo@example.com", display_name="Quorra Bo"
    )
    # The two already here come back first, so capping before dropping them would leave six.
    fake_directory.add_user("Quorra Ada", mail="quorra.ada@example.com", object_id=ada_oid)
    fake_directory.add_user("Quorra Bo", mail="quorra.bo@example.com", object_id=bo_oid)
    newcomers = [
        fake_directory.add_user(f"Quorra New {n}", mail=f"quorra.new{n}@bial.example")
        for n in range(8)
    ]

    results = await find_colleagues(db_session, requester=requester, query="Quorra")

    assert [(result.id, result.directory_id) for result in results] == [
        (ada.id, None),
        (bo.id, None),
        *((None, oid) for oid in newcomers),
    ]


async def test_the_directory_fills_no_more_than_the_slots_left(
    db_session, fake_directory: FakeDirectory
) -> None:
    requester = await _member(db_session, fake_directory)
    for n in range(7):
        await UserFactory.create(
            db_session, email=f"quorra.{n}@example.com", display_name=f"Quorra {n}"
        )
    newcomers = [
        fake_directory.add_user(f"Quorra New {n}", mail=f"quorra.new{n}@bial.example")
        for n in range(5)
    ]

    results = await find_colleagues(db_session, requester=requester, query="Quorra")

    assert [result.directory_id for result in results] == [None] * 7 + newcomers[:3]


async def test_a_user_here_the_directory_finds_under_another_name_comes_back_as_that_user(
    db_session, fake_directory: FakeDirectory
) -> None:
    """Their name here no longer matches the query, and they must not become a newcomer."""
    requester = await _member(db_session, fake_directory)
    renamed_oid = uuid.uuid4()
    renamed = await UserFactory.create(
        db_session,
        azure_oid=str(renamed_oid),
        email="p.raman@old-domain.example",
        display_name="P. Raman",
    )
    newcomer = fake_directory.add_user("Quorra New", mail="quorra.new@bial.example")
    fake_directory.add_user(
        "Quorra Raman", mail="quorra.raman@bial.example", object_id=renamed_oid
    )

    results = await find_colleagues(db_session, requester=requester, query="Quorra")

    assert [
        (r.id, r.directory_id, r.display_name, r.email_local_part, r.signed_in) for r in results
    ] == [
        (renamed.id, None, "P. Raman", "p.raman", True),
        (None, newcomer, "Quorra New", "quorra.new", False),
    ]


async def test_users_here_take_the_slots_left_before_newcomers(
    db_session, fake_directory: FakeDirectory
) -> None:
    requester = await _member(db_session, fake_directory)
    found = [
        await UserFactory.create(
            db_session, email=f"quorra.{n}@example.com", display_name=f"Quorra {n}"
        )
        for n in range(9)
    ]
    renamed_oid = uuid.uuid4()
    renamed = await UserFactory.create(
        db_session,
        azure_oid=str(renamed_oid),
        email="p.raman@example.com",
        display_name="P. Raman",
    )
    fake_directory.add_user("Quorra New", mail="quorra.new@bial.example")
    fake_directory.add_user(
        "Quorra Raman", mail="quorra.raman@bial.example", object_id=renamed_oid
    )

    results = await find_colleagues(db_session, requester=requester, query="Quorra")

    assert [(result.id, result.directory_id) for result in results] == [
        *((user.id, None) for user in found),
        (renamed.id, None),
    ]


async def test_the_directory_is_asked_only_after_the_read_transaction_has_ended(
    db_session, fake_directory: FakeDirectory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pooled connection held idle in a transaction while Graph is slow starves the pool."""
    requester = await _member(db_session, fake_directory)
    newcomer = fake_directory.add_user("Quorra New", mail="quorra.new@bial.example")
    in_transaction_when_asked: list[bool] = []

    async def _asked(url: str, params: dict[str, str], headers: dict[str, str]) -> httpx.Response:
        in_transaction_when_asked.append(db_session.in_transaction())
        return await fake_directory(url, params, headers)

    monkeypatch.setattr(directory_client, "_graph_get", _asked)

    results = await find_colleagues(db_session, requester=requester, query="Quorra")

    assert [result.directory_id for result in results] == [newcomer]
    assert in_transaction_when_asked == [False, False]


@pytest.mark.parametrize("who", ["guest", "absent", "not-a-uuid"])
async def test_a_requester_who_is_not_a_directory_member_gets_our_own_users_only(
    db_session, fake_directory: FakeDirectory, who: str
) -> None:
    """The directory is for BIAL's own people: a guest must not browse it through the picker."""
    if who == "guest":
        oid = str(
            fake_directory.add_user(
                "Quorra Guest",
                mail="quorra@partner.example",
                upn="quorra_partner.example#EXT#@bial.onmicrosoft.com",
            )
        )
    elif who == "absent":
        oid = str(uuid.uuid4())
    else:
        oid = f"oid-{uuid.uuid4()}"
    requester = await UserFactory.create(db_session, azure_oid=oid)
    ada = await UserFactory.create(
        db_session, email="quorra.ada@example.com", display_name="Quorra Ada"
    )
    fake_directory.add_user("Quorra New", mail="quorra.new@bial.example")

    results = await find_colleagues(db_session, requester=requester, query="Quorra")

    assert [(result.id, result.directory_id) for result in results] == [(ada.id, None)]
    assert _paths_asked(fake_directory) == ([] if who == "not-a-uuid" else [f"/v1.0/users/{oid}"])


async def test_an_unreachable_directory_leaves_our_own_users_and_is_asked_again_next_time(
    db_session, fake_directory: FakeDirectory
) -> None:
    requester = await _member(db_session, fake_directory)
    ada = await UserFactory.create(
        db_session, email="quorra.ada@example.com", display_name="Quorra Ada"
    )
    newcomer = fake_directory.add_user("Quorra New", mail="quorra.new@bial.example")
    fake_directory.unavailable = True

    while_down = await find_colleagues(db_session, requester=requester, query="Quorra")
    fake_directory.unavailable = False
    once_back = await find_colleagues(db_session, requester=requester, query="Quorra")

    membership = f"/v1.0/users/{requester.azure_oid}"
    assert [(result.id, result.directory_id) for result in while_down] == [(ada.id, None)]
    assert [(result.id, result.directory_id) for result in once_back] == [
        (ada.id, None),
        (None, newcomer),
    ]
    assert _paths_asked(fake_directory) == [membership, membership, _SEARCH_PATH]


async def test_a_members_later_searches_ask_the_directory_only_to_search(
    db_session, fake_directory: FakeDirectory
) -> None:
    requester = await _member(db_session, fake_directory)
    newcomer = fake_directory.add_user("Quorra New", mail="quorra.new@bial.example")

    first = await find_colleagues(db_session, requester=requester, query="Quorra")
    second = await find_colleagues(db_session, requester=requester, query="Quorra")

    assert [result.directory_id for result in first] == [newcomer]
    assert [result.directory_id for result in second] == [newcomer]
    assert _paths_asked(fake_directory) == [
        f"/v1.0/users/{requester.azure_oid}",
        _SEARCH_PATH,
        _SEARCH_PATH,
    ]
