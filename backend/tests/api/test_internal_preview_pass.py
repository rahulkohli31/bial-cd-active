"""The entry route: a ticket turns into a preview pass only in the browser that asked for it, and
every outcome is a redirect, never a page."""

from __future__ import annotations

import httpx
import redis.asyncio as aioredis

from src.config import settings
from src.services.auth.preview_pass import (
    HANDOFF_COOKIE,
    PASS_COOKIE,
    mint_ticket,
    new_binding,
    read_pass,
)
from tests.factories import UserFactory

EDGE = {"X-Internal-Route-Token": settings.INTERNAL_ROUTE_TOKEN.get_secret_value()}
RETURN_PATH = "/a/0123456789abcdef0123456789abcdef/orders?id=7"


def _set_cookies(resp: httpx.Response) -> dict[str, str]:
    return {line.split("=", 1)[0]: line for line in resp.headers.get_list("set-cookie")}


async def test_the_right_browser_gets_a_pass_and_goes_where_it_asked(
    client: httpx.AsyncClient, fake_redis: aioredis.Redis, db_session
) -> None:
    user = await UserFactory.create(db_session)
    binding = new_binding()
    ticket = await mint_ticket(
        user_id=user.id, token_version=user.token_version, binding=binding, return_path=RETURN_PATH
    )

    resp = await client.get(
        "/internal/preview-pass",
        headers={**EDGE, "X-Preview-Ticket": ticket, "X-Preview-Binding": binding},
    )

    assert resp.status_code == 302
    assert resp.headers["location"] == f"{settings.APPS_BASE_URL}{RETURN_PATH}"
    cookies = _set_cookies(resp)
    pass_line = cookies[PASS_COOKIE]
    for attribute in ("HttpOnly", "Secure", "SameSite=lax", "Path=/"):
        assert attribute.lower() in pass_line.lower()
    assert "domain" not in pass_line.lower()
    raw_pass = pass_line.split("=", 1)[1].split(";", 1)[0]
    record = await read_pass(raw_pass)
    assert record is not None
    assert record.user_id == user.id
    assert HANDOFF_COOKIE in cookies


async def test_a_ticket_opened_in_another_browser_plants_no_pass(
    client: httpx.AsyncClient, fake_redis: aioredis.Redis, db_session
) -> None:
    user = await UserFactory.create(db_session)
    binding = new_binding()
    ticket = await mint_ticket(
        user_id=user.id, token_version=user.token_version, binding=binding, return_path=RETURN_PATH
    )

    elsewhere = await client.get(
        "/internal/preview-pass",
        headers={**EDGE, "X-Preview-Ticket": ticket, "X-Preview-Binding": new_binding()},
    )
    retried = await client.get(
        "/internal/preview-pass",
        headers={**EDGE, "X-Preview-Ticket": ticket, "X-Preview-Binding": binding},
    )

    for resp in (elsewhere, retried):
        assert resp.status_code == 302
        assert resp.headers["location"] == f"{settings.APPS_BASE_URL}/__bial_gone"
        assert PASS_COOKIE not in _set_cookies(resp)
