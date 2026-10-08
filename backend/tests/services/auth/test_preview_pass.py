"""The hand-over ticket is spent on first use, and only the browser it was issued to gets its
record back."""

from __future__ import annotations

import hashlib

import redis.asyncio as aioredis

from src.services.auth.preview_pass import (
    mint_pass,
    mint_ticket,
    new_binding,
    read_pass,
    redeem_ticket,
)
from src.services.redis.keys import preview_pass_key
from tests.factories import UserFactory


async def test_a_ticket_redeems_once_in_the_browser_it_was_issued_to(
    fake_redis: aioredis.Redis, db_session
) -> None:
    user = await UserFactory.create(db_session)
    binding = new_binding()
    ticket = await mint_ticket(
        user_id=user.id, token_version=user.token_version, binding=binding, return_path="/a/x/"
    )

    first = await redeem_ticket(ticket, binding)
    second = await redeem_ticket(ticket, binding)

    assert first is not None
    assert first.user_id == user.id
    assert first.return_path == "/a/x/"
    assert second is None


async def test_a_ticket_carried_to_another_browser_is_spent_and_opens_nothing(
    fake_redis: aioredis.Redis, db_session
) -> None:
    user = await UserFactory.create(db_session)
    binding = new_binding()
    ticket = await mint_ticket(
        user_id=user.id, token_version=user.token_version, binding=binding, return_path="/a/x/"
    )

    assert await redeem_ticket(ticket, new_binding()) is None
    assert await redeem_ticket(ticket, binding) is None


async def test_a_pass_record_this_code_cannot_read_is_no_pass(
    fake_redis: aioredis.Redis, db_session
) -> None:
    """Sends the browser through the hand-over for a fresh pass instead of failing every lookup."""
    user = await UserFactory.create(db_session)
    readable = await mint_pass(user_id=user.id, token_version=user.token_version)
    unreadable = await mint_pass(user_id=user.id, token_version=user.token_version)
    await fake_redis.set(
        preview_pass_key(hashlib.sha256(unreadable.encode()).hexdigest()), '{"user_id": 1}'
    )

    assert await read_pass(readable) is not None
    assert await read_pass(unreadable) is None
