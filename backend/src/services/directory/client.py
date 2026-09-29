"""Search and read BIAL's Entra directory as this process's own system-assigned identity.

The credential is `ManagedIdentityCredential()` with no client id: the backend's host also carries
the shared identity generated apps receive, and naming a client id would ask Graph as that one.
Failures are logged by exception class and HTTP status only, never by message: httpx puts the
request URL, and with it the search term, into its messages.
"""

from __future__ import annotations

import asyncio
import enum
import os
import re
import uuid
from dataclasses import dataclass
from typing import Final

import httpx
import structlog
from azure.identity.aio import ManagedIdentityCredential
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

_log = structlog.get_logger()

_GRAPH_SCOPE: Final = "https://graph.microsoft.com/.default"
_USERS_URL: Final = "https://graph.microsoft.com/v1.0/users"
_SELECT: Final = "id,displayName,mail,userPrincipalName"
_SEARCH_FIELDS: Final = ("displayName", "mail", "userPrincipalName")
# About two and a half pages of results: guests are roughly half the directory and are dropped.
_SEARCH_TOP: Final = "25"
# Covers the token and the request together, so a slow directory cannot hold a search open.
_TIME_BUDGET_S: Final = 2.5
_GUEST_MARKER: Final = "#EXT#"
# Set by App Service on a host that has a managed identity; without it no token can be issued.
_IDENTITY_ENDPOINT_VAR: Final = "IDENTITY_ENDPOINT"
# Letters in any script, digits, space and `.-'_@`. A quote, backslash, colon or bracket could
# break out of the quoted `$search` clause, so a query holding one is never sent.
_SEARCHABLE: Final = re.compile(r"[\w .'@-]+")


@dataclass(frozen=True, slots=True)
class DirectoryPerson:
    """An eligible directory member. `email` is `mail`, else the UPN, as sign-in derives it."""

    object_id: uuid.UUID
    display_name: str | None
    email: str
    upn: str


class DirectoryMiss(enum.Enum):
    """Why a lookup returned nobody: absent or ineligible, or the directory could not be asked."""

    NOT_FOUND = "not_found"
    UNAVAILABLE = "unavailable"


class _NoManagedIdentityError(Exception):
    """This host has no managed identity, so Graph cannot be asked."""


class _GraphUser(BaseModel):
    # Required but nullable: `$select` makes Graph send all four keys, so a missing one means the
    # body is not the answer that was asked for.
    model_config = ConfigDict(alias_generator=to_camel, frozen=True)

    id: uuid.UUID | None
    display_name: str | None
    mail: str | None
    user_principal_name: str | None


class _GraphUserPage(BaseModel):
    value: list[_GraphUser]


@dataclass
class _Connection:
    credential: ManagedIdentityCredential | None = None
    http: httpx.AsyncClient | None = None


_state = _Connection()
_members: dict[uuid.UUID, bool] = {}


async def _graph_get(url: str, params: dict[str, str], headers: dict[str, str]) -> httpx.Response:
    """One Graph GET as this process's identity, inside the time budget."""
    if not os.environ.get(_IDENTITY_ENDPOINT_VAR):
        raise _NoManagedIdentityError
    if _state.credential is None:
        _state.credential = ManagedIdentityCredential()
    if _state.http is None:
        _state.http = httpx.AsyncClient(timeout=_TIME_BUDGET_S)
    credential, http = _state.credential, _state.http
    async with asyncio.timeout(_TIME_BUDGET_S):
        token = await credential.get_token(_GRAPH_SCOPE)
        return await http.get(
            url, params=params, headers={**headers, "Authorization": f"Bearer {token.token}"}
        )


def _warn(exc: Exception, response: httpx.Response | None) -> None:
    _log.warning(
        "directory_unavailable",
        error=type(exc).__name__,
        status=None if response is None else response.status_code,
    )


def _eligible(user: _GraphUser) -> DirectoryPerson | None:
    """A member the platform can key. Guests, and entries with no id or UPN, are not offered."""
    upn = user.user_principal_name
    if user.id is None or not upn or _GUEST_MARKER in upn.upper():
        return None
    return DirectoryPerson(
        object_id=user.id, display_name=user.display_name, email=user.mail or upn, upn=upn
    )


async def search_directory(query: str) -> list[DirectoryPerson]:
    """Every eligible member whose name, mail or UPN matches `query`, from one Graph call.

    Neither capped nor deduplicated: the caller merges these with its own users. A query outside
    the allowlist returns nothing without a call, and so does any failure."""
    if _SEARCHABLE.fullmatch(query) is None:
        return []
    params = {
        "$search": " OR ".join(f'"{field}:{query}"' for field in _SEARCH_FIELDS),
        "$select": _SELECT,
        "$top": _SEARCH_TOP,
    }
    response: httpx.Response | None = None
    try:
        response = await _graph_get(_USERS_URL, params, {"ConsistencyLevel": "eventual"})
        response.raise_for_status()
        page = _GraphUserPage.model_validate_json(response.content)
    except _NoManagedIdentityError:
        return []
    except Exception as exc:
        _warn(exc, response)
        return []
    return [person for user in page.value if (person := _eligible(user)) is not None]


async def get_directory_person(object_id: uuid.UUID) -> DirectoryPerson | DirectoryMiss:
    """The eligible member with this object id, `NOT_FOUND` when Graph has no such person or
    they are not eligible, or `UNAVAILABLE` on any failure."""
    response: httpx.Response | None = None
    try:
        response = await _graph_get(f"{_USERS_URL}/{object_id}", {"$select": _SELECT}, {})
        if response.status_code == httpx.codes.NOT_FOUND:
            return DirectoryMiss.NOT_FOUND
        response.raise_for_status()
        user = _GraphUser.model_validate_json(response.content)
    except _NoManagedIdentityError:
        return DirectoryMiss.UNAVAILABLE
    except Exception as exc:
        _warn(exc, response)
        return DirectoryMiss.UNAVAILABLE
    person = _eligible(user)
    return DirectoryMiss.NOT_FOUND if person is None else person


async def is_directory_member(object_id: str) -> bool:
    """Whether `object_id` is an eligible member. Fails closed: a guest, an id that is not a UUID
    and a directory that cannot be asked all read as not a member. A definite answer is kept for
    the life of the process; an unavailable directory is asked again next time."""
    try:
        oid = uuid.UUID(object_id)
    except ValueError:
        return False
    if (member := _members.get(oid)) is not None:
        return member
    person = await get_directory_person(oid)
    if person is DirectoryMiss.UNAVAILABLE:
        return False
    member = isinstance(person, DirectoryPerson)
    _members[oid] = member
    return member


async def aclose_directory() -> None:
    """Close the cached HTTP client and credential, and forget every membership answer."""
    _members.clear()
    http, credential = _state.http, _state.credential
    _state.http = None
    _state.credential = None
    try:
        if http is not None:
            await http.aclose()
    finally:
        if credential is not None:
            await credential.close()
