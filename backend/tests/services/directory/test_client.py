"""The directory client: what it asks Graph, what it keeps, and how it fails soft.

Graph is an `httpx.MockTransport` installed as the module's HTTP client, and the managed identity
is a fake patched over the SDK constructor, so every test runs the real request path with no
Azure. The suite-wide `fake_directory` is overridden here for that reason.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable, MutableMapping
from typing import Any

import httpx
import pytest
from azure.core.credentials import AccessToken
from azure.identity import CredentialUnavailableError
from structlog.testing import capture_logs

from src.services.directory import (
    DirectoryMiss,
    DirectoryPerson,
    aclose_directory,
    get_directory_person,
    reset_directory_for_tests,
    search_directory,
)
from src.services.directory import client as directory_client

_TOKEN = "eyJ0eXAiOiJKV1Qi.fake-graph-token"
_QUERY = "priya"
_SELECT = "id,displayName,mail,userPrincipalName"

_PRIYA: dict[str, str | None] = {
    "id": "0f8fad5b-d9cb-469f-a165-70867728950e",
    "displayName": "Priya Raman",
    "mail": "priya.raman@bial.example",
    "userPrincipalName": "praman@bial.example",
}
_PRIYA_ID = uuid.UUID("0f8fad5b-d9cb-469f-a165-70867728950e")
_PRIYANKA_WITHOUT_MAIL: dict[str, str | None] = {
    "id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
    "displayName": "Priyanka Rao",
    "mail": None,
    "userPrincipalName": "priyanka.rao@bial.example",
}
_GUEST: dict[str, str | None] = {
    "id": "16fd2706-8baf-433b-82eb-8c7fada847da",
    "displayName": "Priya Guest",
    "mail": "priya@partner.example",
    "userPrincipalName": "priya_partner.example#EXT#@bial.onmicrosoft.com",
}
_WITHOUT_ID: dict[str, str | None] = {
    "id": None,
    "displayName": "Priya Nobody",
    "mail": "nobody@bial.example",
    "userPrincipalName": "nobody@bial.example",
}


def _page(*users: dict[str, str | None]) -> dict[str, object]:
    return {
        "@odata.context": "https://graph.microsoft.com/v1.0/$metadata#users",
        "value": list(users),
    }


def _member(n: int) -> dict[str, str | None]:
    return {
        "id": f"00000000-0000-4000-8000-{n:012d}",
        "displayName": f"Priya Member {n}",
        "mail": f"priya.{n}@bial.example",
        "userPrincipalName": f"priya.{n}@bial.example",
    }


def _guest(n: int) -> dict[str, str | None]:
    return {
        "id": f"00000000-0000-4000-9000-{n:012d}",
        "displayName": f"Priya Guest {n}",
        "mail": f"priya.{n}@partner.example",
        "userPrincipalName": f"priya.{n}_partner.example#EXT#@bial.onmicrosoft.com",
    }


# --- the fakes ----------------------------------------------------------------------------------


class _FakeIdentity:
    """Patched over `ManagedIdentityCredential`: records how it was built and hands back itself."""

    def __init__(self) -> None:
        self.built_with: list[dict[str, object]] = []
        self.token_requests: list[tuple[str, ...]] = []
        self.refusal: Exception | None = None
        self.hangs = False
        self.closes = 0

    def __call__(self, *args: object, **kwargs: object) -> _FakeIdentity:
        self.built_with.append({"args": args, **kwargs})
        return self

    async def get_token(self, *scopes: str) -> AccessToken:
        self.token_requests.append(scopes)
        if self.hangs:
            await asyncio.sleep(3600)
        if self.refusal is not None:
            raise self.refusal
        return AccessToken(_TOKEN, 4_102_444_800)

    async def close(self) -> None:
        self.closes += 1


class _FakeGraph:
    """Graph behind a `MockTransport`: every request is recorded, then answered by `answer`."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.answer: Callable[[httpx.Request], httpx.Response] = lambda _: httpx.Response(
            200, json=_page()
        )
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.answer(request)


@pytest.fixture(autouse=True)
def fake_directory() -> None:
    """Overrides the suite-wide fake: these tests drive the client's real outbound call."""


@pytest.fixture(autouse=True)
async def _fresh_directory() -> AsyncIterator[None]:
    await reset_directory_for_tests()
    yield
    await reset_directory_for_tests()


@pytest.fixture(autouse=True)
def identity(monkeypatch: pytest.MonkeyPatch) -> _FakeIdentity:
    fake = _FakeIdentity()
    monkeypatch.setattr(directory_client, "ManagedIdentityCredential", fake)
    return fake


@pytest.fixture(autouse=True)
def graph(_fresh_directory: None) -> _FakeGraph:
    fake = _FakeGraph()
    directory_client._state.http = fake.client
    return fake


def _warnings(logs: list[MutableMapping[str, Any]]) -> list[MutableMapping[str, Any]]:
    return [entry for entry in logs if entry["log_level"] == "warning"]


# --- search -------------------------------------------------------------------------------------


async def test_search_sends_one_field_scoped_query_as_the_backends_own_identity(
    identity: _FakeIdentity, graph: _FakeGraph
) -> None:
    await search_directory(_QUERY)

    [request] = graph.requests
    assert request.method == "GET"
    assert (request.url.scheme, request.url.host, request.url.path) == (
        "https",
        "graph.microsoft.com",
        "/v1.0/users",
    )
    assert request.url.params["$search"] == (
        '"displayName:priya" OR "mail:priya" OR "userPrincipalName:priya"'
    )
    assert request.url.params["$select"] == _SELECT
    assert request.url.params["$top"] == "25"
    assert request.headers["ConsistencyLevel"] == "eventual"
    assert request.headers["Authorization"] == f"Bearer {_TOKEN}"
    assert identity.token_requests == [("https://graph.microsoft.com/.default",)]


async def test_search_maps_members_and_falls_back_to_the_upn_for_email(graph: _FakeGraph) -> None:
    graph.answer = lambda _: httpx.Response(200, json=_page(_PRIYA, _PRIYANKA_WITHOUT_MAIL))

    assert await search_directory(_QUERY) == [
        DirectoryPerson(
            object_id=_PRIYA_ID,
            display_name="Priya Raman",
            email="priya.raman@bial.example",
            upn="praman@bial.example",
        ),
        DirectoryPerson(
            object_id=uuid.UUID("7c9e6679-7425-40de-944b-e07fc1f90ae7"),
            display_name="Priyanka Rao",
            email="priyanka.rao@bial.example",
            upn="priyanka.rao@bial.example",
        ),
    ]


@pytest.mark.parametrize(
    ("raw", "kept"),
    [
        pytest.param(
            [_member(n) if n % 2 == 0 else _guest(n) for n in range(25)],
            [f"00000000-0000-4000-8000-{n:012d}" for n in range(0, 25, 2)],
            id="half-of-25-are-guests",
        ),
        pytest.param([_guest(n) for n in range(25)], [], id="every-hit-is-a-guest"),
        pytest.param(
            [_WITHOUT_ID, _GUEST, _PRIYA],
            ["0f8fad5b-d9cb-469f-a165-70867728950e"],
            id="no-id-and-a-guest-beside-a-member",
        ),
    ],
)
async def test_search_drops_guests_and_entries_without_an_id(
    raw: list[dict[str, str | None]], kept: list[str], graph: _FakeGraph
) -> None:
    graph.answer = lambda _: httpx.Response(200, json=_page(*raw))

    people = await search_directory(_QUERY)

    assert [str(person.object_id) for person in people] == kept
    assert len(graph.requests) == 1


@pytest.mark.parametrize(
    "query", ['pri"ya', "pri\\ya", "pri(ya", "pri)ya", "mail:priya", "pri%ya"]
)
async def test_a_query_outside_the_allowlist_is_never_sent(
    query: str, identity: _FakeIdentity, graph: _FakeGraph
) -> None:
    with capture_logs() as logs:
        assert await search_directory(query) == []

    assert graph.requests == []
    assert identity.token_requests == []
    assert logs == []
    # The same wiring does send an allowed query, so the silence above is the allowlist's.
    await search_directory(_QUERY)
    assert len(graph.requests) == 1


@pytest.mark.parametrize(
    "query", ["o'brien", "anne-marie", "j_doe", "priya.raman@bial", "Priya Raman", "007"]
)
async def test_a_query_inside_the_allowlist_is_sent(query: str, graph: _FakeGraph) -> None:
    await search_directory(query)

    [request] = graph.requests
    assert request.url.params["$search"].startswith(f'"displayName:{query}" OR ')


async def test_an_allowed_non_ascii_name_is_sent_percent_encoded(graph: _FakeGraph) -> None:
    await search_directory("Zoë")

    [request] = graph.requests
    assert b"Zo%C3%AB" in request.url.query
    assert request.url.params["$search"] == (
        '"displayName:Zoë" OR "mail:Zoë" OR "userPrincipalName:Zoë"'
    )


async def test_the_credential_is_the_system_identity_whatever_azure_client_id_says(
    identity: _FakeIdentity, graph: _FakeGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The host also carries the identity generated apps receive; a client id would ask Graph as
    that one. Built once, then reused."""
    monkeypatch.setenv("AZURE_CLIENT_ID", "52b74947-0621-46e2-a523-a6b466f47c33")

    await search_directory(_QUERY)
    await search_directory(_QUERY)

    assert identity.built_with == [{"args": ()}]
    assert len(graph.requests) == 2


# --- failures -----------------------------------------------------------------------------------

_Arrange = Callable[[_FakeIdentity, _FakeGraph], None]


def _credential_raises(error: Exception) -> _Arrange:
    def arrange(identity: _FakeIdentity, _graph: _FakeGraph) -> None:
        identity.refusal = error

    return arrange


def _graph_answers(respond: Callable[[], httpx.Response]) -> _Arrange:
    def arrange(_identity: _FakeIdentity, graph: _FakeGraph) -> None:
        graph.answer = lambda _: respond()

    return arrange


def _graph_raises(error: type[httpx.TransportError]) -> _Arrange:
    def arrange(_identity: _FakeIdentity, graph: _FakeGraph) -> None:
        def answer(request: httpx.Request) -> httpx.Response:
            raise error(f"failed for {request.url}", request=request)

        graph.answer = answer

    return arrange


# Echoes the query, so a logged body would show up in the assertions below.
_A_GRAPH_ERROR_BODY = {"error": {"code": "Denied", "message": f"no access for {_QUERY}"}}

_FAILURES = [
    pytest.param(
        _credential_raises(CredentialUnavailableError(f"no managed identity for {_QUERY}")),
        "CredentialUnavailableError",
        None,
        id="credential-unavailable",
    ),
    pytest.param(
        _credential_raises(RuntimeError(f"something else about {_QUERY}")),
        "RuntimeError",
        None,
        id="any-other-exception",
    ),
    pytest.param(
        _graph_answers(lambda: httpx.Response(401, json=_A_GRAPH_ERROR_BODY)),
        "HTTPStatusError",
        401,
        id="401",
    ),
    pytest.param(
        _graph_answers(lambda: httpx.Response(403, json=_A_GRAPH_ERROR_BODY)),
        "HTTPStatusError",
        403,
        id="403",
    ),
    pytest.param(
        _graph_answers(
            lambda: httpx.Response(429, headers={"Retry-After": "1"}, json=_A_GRAPH_ERROR_BODY)
        ),
        "HTTPStatusError",
        429,
        id="429",
    ),
    pytest.param(
        _graph_answers(lambda: httpx.Response(500, json=_A_GRAPH_ERROR_BODY)),
        "HTTPStatusError",
        500,
        id="500",
    ),
    pytest.param(_graph_raises(httpx.ReadTimeout), "ReadTimeout", None, id="timeout"),
    pytest.param(_graph_raises(httpx.ConnectError), "ConnectError", None, id="unreachable"),
    pytest.param(
        _graph_answers(lambda: httpx.Response(200, content=f"<html>{_QUERY}</html>".encode())),
        "ValidationError",
        200,
        id="not-json",
    ),
    pytest.param(
        _graph_answers(lambda: httpx.Response(200, json={"value": _QUERY})),
        "ValidationError",
        200,
        id="wrong-shape",
    ),
]


@pytest.mark.parametrize(("arrange", "error", "status"), _FAILURES)
async def test_a_failed_search_returns_nothing_and_warns_once_without_the_query(
    arrange: _Arrange,
    error: str,
    status: int | None,
    identity: _FakeIdentity,
    graph: _FakeGraph,
) -> None:
    arrange(identity, graph)

    with capture_logs() as logs:
        assert await search_directory(_QUERY) == []

    assert _warnings(logs) == [
        {
            "event": "directory_unavailable",
            "error": error,
            "status": status,
            "log_level": "warning",
        }
    ]
    rendered = repr(logs)
    assert _QUERY not in rendered
    assert "Bearer" not in rendered
    assert _TOKEN not in rendered


async def test_a_credential_that_hangs_is_cut_off_by_the_time_budget(
    identity: _FakeIdentity, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(directory_client, "_TIME_BUDGET_S", 0.05)
    identity.hangs = True

    with capture_logs() as logs:
        assert await search_directory(_QUERY) == []

    assert [entry["error"] for entry in _warnings(logs)] == ["TimeoutError"]


# --- get ----------------------------------------------------------------------------------------


async def test_get_reads_one_person_by_canonical_object_id(graph: _FakeGraph) -> None:
    graph.answer = lambda _: httpx.Response(200, json=_PRIYA)

    person = await get_directory_person(uuid.UUID("0F8FAD5B-D9CB-469F-A165-70867728950E"))

    assert person == DirectoryPerson(
        object_id=_PRIYA_ID,
        display_name="Priya Raman",
        email="priya.raman@bial.example",
        upn="praman@bial.example",
    )
    [request] = graph.requests
    assert request.url.host == "graph.microsoft.com"
    assert request.url.path == "/v1.0/users/0f8fad5b-d9cb-469f-a165-70867728950e"
    assert request.url.params["$select"] == _SELECT
    assert request.headers["Authorization"] == f"Bearer {_TOKEN}"


@pytest.mark.parametrize(
    "respond",
    [
        pytest.param(lambda: httpx.Response(404, json=_A_GRAPH_ERROR_BODY), id="404"),
        pytest.param(lambda: httpx.Response(200, json=_GUEST), id="guest"),
    ],
)
async def test_get_of_a_missing_or_guest_person_is_not_found_and_logs_nothing(
    respond: Callable[[], httpx.Response], graph: _FakeGraph
) -> None:
    graph.answer = lambda _: respond()

    with capture_logs() as logs:
        assert await get_directory_person(uuid.UUID(_GUEST["id"])) is DirectoryMiss.NOT_FOUND

    assert len(graph.requests) == 1
    assert logs == []


@pytest.mark.parametrize(("arrange", "error", "status"), _FAILURES)
async def test_get_during_a_failure_is_unavailable_and_warns_without_the_id(
    arrange: _Arrange,
    error: str,
    status: int | None,
    identity: _FakeIdentity,
    graph: _FakeGraph,
) -> None:
    arrange(identity, graph)

    with capture_logs() as logs:
        assert await get_directory_person(_PRIYA_ID) is DirectoryMiss.UNAVAILABLE

    assert [(entry["error"], entry["status"]) for entry in _warnings(logs)] == [(error, status)]
    assert str(_PRIYA_ID) not in repr(logs)
    assert "Bearer" not in repr(logs)


# --- closing ------------------------------------------------------------------------------------


async def test_aclose_closes_the_credential_and_client_and_a_second_close_is_a_no_op(
    identity: _FakeIdentity, graph: _FakeGraph
) -> None:
    await search_directory(_QUERY)
    assert identity.built_with == [{"args": ()}]

    await aclose_directory()
    assert identity.closes == 1
    assert graph.client.is_closed

    await aclose_directory()
    assert identity.closes == 1
