"""The preview hand-over: a ticket only for the person whose workspace runs the preview, and a
destination that never loops and never puts a sign-in page in a frame."""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import redis.asyncio as aioredis

from src.config import settings
from src.services.auth.preview_pass import redeem_ticket
from src.services.auth.session_jwt import mint_session_jwt
from src.services.redis import registry_key
from src.services.redis.keys import REGISTRY_FIELD_ALIAS
from src.services.sandbox.base import new_alias
from tests.factories import UserFactory

BINDING = "0123456789abcdef0123456789abcdef"
GONE = f"{settings.APPS_BASE_URL}/__bial_gone"


def _session(user) -> dict[str, str]:
    jwt = mint_session_jwt(user.id, user.token_version, settings.auth.access_ttl_seconds)
    return {"Cookie": f"session={jwt}"}


def _handoff(alias: str, rest: str = "/") -> str:
    return f"/v1/auth/preview-handoff/{BINDING}/{alias}{rest}"


async def _holding(db_session, redis: aioredis.Redis):
    user = await UserFactory.create(db_session)
    alias = new_alias()
    await redis.hset(registry_key(user.id), REGISTRY_FIELD_ALIAS, alias)
    return user, alias


@pytest.mark.parametrize(
    ("asked", "returned"),
    [
        ("/a/{alias}/dashboard?tab=2", "/a/{alias}/dashboard?tab=2"),
        ("/dashboard?tab=2", "/a/{alias}/dashboard?tab=2"),
        ("/a/{alias}/?next=https://example.com/x", "/a/{alias}/?next=https://example.com/x"),
    ],
    ids=["a-preview-address", "a-keyless-address", "a-link-in-the-query"],
)
async def test_the_holder_gets_a_ticket_back_to_the_page_they_asked_for(
    client: httpx.AsyncClient, fake_redis: aioredis.Redis, db_session, asked: str, returned: str
) -> None:
    user, alias = await _holding(db_session, fake_redis)

    resp = await client.get(
        _handoff(alias, asked.format(alias=alias)),
        headers={**_session(user), "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"},
    )

    assert resp.status_code == 302
    location = urlsplit(resp.headers["location"])
    assert f"{location.scheme}://{location.netloc}" == settings.APPS_BASE_URL
    assert location.path == "/__bial_enter"
    ticket = parse_qs(location.query)["ticket"][0]
    record = await redeem_ticket(ticket, BINDING)
    assert record is not None
    assert record.user_id == user.id
    assert record.return_path == returned.format(alias=alias)


async def test_a_suspended_holder_gets_no_ticket(
    client: httpx.AsyncClient, fake_redis: aioredis.Redis, db_session
) -> None:
    user, alias = await _holding(db_session, fake_redis)
    session = _session(user)
    user.suspended_at = datetime.now(UTC)
    await db_session.flush()

    resp = await client.get(_handoff(alias), headers=session)

    assert resp.headers["location"] == GONE


async def test_someone_elses_preview_gets_no_ticket(
    client: httpx.AsyncClient, fake_redis: aioredis.Redis, db_session
) -> None:
    _, alias = await _holding(db_session, fake_redis)
    stranger = await UserFactory.create(db_session)

    resp = await client.get(_handoff(alias), headers=_session(stranger))

    assert resp.status_code == 302
    assert resp.headers["location"] == GONE


@pytest.mark.parametrize("mode", ["cors", "no-cors", "same-origin"])
async def test_a_request_that_is_not_a_navigation_gets_no_ticket(
    client: httpx.AsyncClient, fake_redis: aioredis.Redis, db_session, mode: str
) -> None:
    user, alias = await _holding(db_session, fake_redis)

    resp = await client.get(_handoff(alias), headers={**_session(user), "Sec-Fetch-Mode": mode})

    assert resp.status_code == 400
    assert "location" not in resp.headers


async def test_without_a_session_a_tab_goes_to_the_portal_and_a_frame_never_does(
    client: httpx.AsyncClient, fake_redis: aioredis.Redis, db_session
) -> None:
    _, alias = await _holding(db_session, fake_redis)

    tab = await client.get(_handoff(alias), headers={"Sec-Fetch-Dest": "document"})
    frame = await client.get(_handoff(alias), headers={"Sec-Fetch-Dest": "iframe"})

    assert tab.headers["location"] == settings.FRONTEND_URL
    assert frame.headers["location"] == GONE
    for resp in (tab, frame):
        assert "/auth/login" not in resp.headers["location"]


async def test_a_return_address_that_could_leave_the_preview_is_refused(
    client: httpx.AsyncClient, fake_redis: aioredis.Redis, db_session
) -> None:
    user, alias = await _holding(db_session, fake_redis)

    escaping = await client.get(_handoff(alias, "//evil.example/x"), headers=_session(user))
    staying = await client.get(_handoff(alias, "/x"), headers=_session(user))

    assert escaping.headers["location"] == GONE
    assert urlsplit(staying.headers["location"]).path == "/__bial_enter"
