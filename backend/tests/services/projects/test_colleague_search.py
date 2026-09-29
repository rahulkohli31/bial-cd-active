"""Colleague search: our own users first, then directory people who have no user here yet."""

from __future__ import annotations

import uuid

import httpx
import pytest

from src.services.directory import client as directory_client
from src.services.projects.shares import find_colleagues
from tests.factories import UserFactory
from tests.fakes import FakeDirectory


async def test_a_full_page_of_our_own_users_never_asks_the_directory(
    db_session, fake_directory: FakeDirectory
) -> None:
    requester = await UserFactory.create(db_session)
    for n in range(10):
        await UserFactory.create(
            db_session, email=f"quorra.{n}@example.com", display_name=f"Quorra {n}"
        )
    fake_directory.add_user("Quorra Outsider", mail="quorra.outsider@bial.example")

    results = await find_colleagues(db_session, requester_id=requester.id, query="Quorra")

    assert [result.display_name for result in results] == [f"Quorra {n}" for n in range(10)]
    assert fake_directory.requests == []


async def test_people_already_here_are_dropped_before_the_directory_fills_the_slots_left(
    db_session, fake_directory: FakeDirectory
) -> None:
    requester = await UserFactory.create(db_session)
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

    results = await find_colleagues(db_session, requester_id=requester.id, query="Quorra")

    assert [(result.id, result.directory_id) for result in results] == [
        (ada.id, None),
        (bo.id, None),
        *((None, oid) for oid in newcomers),
    ]


async def test_the_directory_fills_no_more_than_the_slots_left(
    db_session, fake_directory: FakeDirectory
) -> None:
    requester = await UserFactory.create(db_session)
    for n in range(7):
        await UserFactory.create(
            db_session, email=f"quorra.{n}@example.com", display_name=f"Quorra {n}"
        )
    newcomers = [
        fake_directory.add_user(f"Quorra New {n}", mail=f"quorra.new{n}@bial.example")
        for n in range(5)
    ]

    results = await find_colleagues(db_session, requester_id=requester.id, query="Quorra")

    assert [result.directory_id for result in results] == [None] * 7 + newcomers[:3]


async def test_the_requester_and_anyone_known_by_object_id_never_come_back_from_the_directory(
    db_session, fake_directory: FakeDirectory
) -> None:
    requester_oid, renamed_oid = uuid.uuid4(), uuid.uuid4()
    requester = await UserFactory.create(db_session, azure_oid=str(requester_oid))
    await UserFactory.create(
        db_session,
        azure_oid=str(renamed_oid),
        email="p.raman@old-domain.example",
        display_name="P. Raman",
    )
    fake_directory.add_user(
        "Quorra Self", mail="quorra.self@bial.example", object_id=requester_oid
    )
    fake_directory.add_user(
        "Quorra Raman", mail="quorra.raman@bial.example", object_id=renamed_oid
    )
    newcomer = fake_directory.add_user("Quorra New", mail="quorra.new@bial.example")

    results = await find_colleagues(db_session, requester_id=requester.id, query="Quorra")

    assert [(result.id, result.directory_id) for result in results] == [(None, newcomer)]


async def test_the_directory_is_asked_only_after_the_read_transaction_has_ended(
    db_session, fake_directory: FakeDirectory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pooled connection held idle in a transaction while Graph is slow starves the pool."""
    requester = await UserFactory.create(db_session)
    newcomer = fake_directory.add_user("Quorra New", mail="quorra.new@bial.example")
    in_transaction_when_asked: list[bool] = []

    async def _asked(url: str, params: dict[str, str], headers: dict[str, str]) -> httpx.Response:
        in_transaction_when_asked.append(db_session.in_transaction())
        return await fake_directory(url, params, headers)

    monkeypatch.setattr(directory_client, "_graph_get", _asked)

    results = await find_colleagues(db_session, requester_id=requester.id, query="Quorra")

    assert [result.directory_id for result in results] == [newcomer]
    assert in_transaction_when_asked == [False]
