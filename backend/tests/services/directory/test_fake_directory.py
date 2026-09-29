"""The suite-wide directory fake: installed for every test, empty until a test fills it."""

from __future__ import annotations

import uuid

from src.services.directory import DirectoryMiss, get_directory_person, search_directory
from tests.fakes import FakeDirectory


async def test_every_test_reaches_the_fake_rather_than_azure(
    fake_directory: FakeDirectory,
) -> None:
    assert await search_directory("priya") == []
    assert [request.url.path for request in fake_directory.requests] == ["/v1.0/users"]


async def test_a_test_fills_the_directory_through_the_fake(fake_directory: FakeDirectory) -> None:
    priya = fake_directory.add_user("Priya Raman", mail="priya.raman@bial.example")
    fake_directory.add_user(
        "Priya Guest",
        mail="priya@partner.example",
        upn="priya_partner.example#EXT#@bial.onmicrosoft.com",
    )

    [person] = await search_directory("priya")

    assert (person.object_id, person.email) == (priya, "priya.raman@bial.example")
    assert await get_directory_person(priya) == person
    assert await get_directory_person(uuid.UUID(int=1)) is DirectoryMiss.NOT_FOUND


async def test_an_unavailable_fake_reads_as_an_unavailable_directory(
    fake_directory: FakeDirectory,
) -> None:
    priya = fake_directory.add_user()
    fake_directory.unavailable = True

    assert await search_directory("priya") == []
    assert await get_directory_person(priya) is DirectoryMiss.UNAVAILABLE
