"""The edge's alias lookup: alias and pass in, container name out, only for the edge and only for
the person whose workspace runs the preview.

The answer is a header, never a body, and EVERY refusal is a 200: the edge reads a 200 from its
subrequest as data and anything else as an error it cannot place. So each refusal below is paired
with a successful lookup for the same alias, because a route that answered nothing at all would
satisfy "no header" for every case.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import redis.asyncio as aioredis
import structlog.testing
from fastapi import FastAPI
from pydantic import SecretStr
from redis.exceptions import RedisError

from src.api.internal.router import (
    CONTAINER_HEADER,
    DENIED_HEADER,
    LOOKUP_FAILED_EVENT,
    SECRET_REFUSED_EVENT,
)
from src.config import settings
from src.main import create_app
from src.services.auth.preview_pass import mint_pass
from src.services.auth.refresh import revoke_all_sessions
from src.services.redis import registry_key
from src.services.redis.keys import (
    ALIAS_TTL_SECONDS,
    REGISTRY_FIELD_ALIAS,
    alias_key,
)
from src.services.sandbox.base import new_alias
from src.services.sandbox.client import AcaSandboxClient
from src.services.sandbox.config import SandboxConfig
from tests.factories import UserFactory
from tests.fakes import a_sandbox_name

USER = uuid.uuid4()
CONTAINER = a_sandbox_name("lookup")
EDGE = {"X-Internal-Route-Token": settings.INTERNAL_ROUTE_TOKEN.get_secret_value()}


def _path(alias: str) -> str:
    return f"/internal/app-routes/{alias}"


async def _a_live_alias(
    redis: aioredis.Redis,
    holder: uuid.UUID = USER,
    *,
    shared_owner_id: uuid.UUID | None = None,
) -> str:
    """An alias exactly as the create step leaves it, written through the real client rather than
    typed key by key, so a drift between what is written and what is read is caught here."""

    class _NoArm:
        pass

    client = AcaSandboxClient(
        SandboxConfig(
            subscription_id="sub",
            resource_group="rg",
            region="westeurope",
            managed_environment_name="aca-env",
            acr_server="acr.azurecr.io",
            acr_username="acr-user",
            acr_password=SecretStr("acr-pass"),
            image_ref="acr.azurecr.io/sandbox:latest",
        ),
        aca=_NoArm(),  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]  # pyright: ignore[reportArgumentType]
    )
    alias = new_alias()
    await client._write_registry(
        holder,
        app_name=CONTAINER,
        app_id=uuid.uuid4(),
        alias=alias,
        fqdn=f"{CONTAINER}.example",
        token_ref="ref",
        shared_project_id=None if shared_owner_id is None else uuid.uuid4(),
        shared_owner_id=shared_owner_id,
    )
    return alias


async def _holding(db_session, redis: aioredis.Redis) -> tuple[str, dict[str, str]]:
    """A live alias and the edge headers its holder's browser produces."""
    holder = await UserFactory.create(db_session)
    alias = await _a_live_alias(redis, holder.id)
    pass_ = await mint_pass(user_id=holder.id, token_version=holder.token_version)
    return alias, {**EDGE, "X-Preview-Pass": pass_}


async def test_the_edge_is_told_which_container_a_current_alias_stands_for(
    client: httpx.AsyncClient, fake_redis: aioredis.Redis, db_session
) -> None:
    alias, holders_browser = await _holding(db_session, fake_redis)

    resp = await client.get(_path(alias), headers=holders_browser)

    assert resp.status_code == 200
    assert resp.headers[CONTAINER_HEADER] == CONTAINER
    assert DENIED_HEADER not in resp.headers
    assert CONTAINER not in resp.text


async def test_a_browser_without_the_holders_pass_is_denied_with_a_fresh_value(
    client: httpx.AsyncClient, fake_redis: aioredis.Redis, db_session
) -> None:
    alias, holders_browser = await _holding(db_session, fake_redis)
    stranger = await UserFactory.create(db_session)
    strangers_pass = await mint_pass(user_id=stranger.id, token_version=stranger.token_version)

    no_pass = await client.get(_path(alias), headers=EDGE)
    again = await client.get(_path(alias), headers=EDGE)
    someone_else = await client.get(
        _path(alias), headers={**EDGE, "X-Preview-Pass": strangers_pass}
    )
    holder = await client.get(_path(alias), headers=holders_browser)

    for refused in (no_pass, again, someone_else):
        assert CONTAINER_HEADER not in refused.headers
        assert len(refused.headers[DENIED_HEADER]) == 32
    assert no_pass.headers[DENIED_HEADER] != again.headers[DENIED_HEADER]
    assert holder.headers[CONTAINER_HEADER] == CONTAINER


async def test_a_pass_from_before_a_sign_out_is_denied(
    client: httpx.AsyncClient, fake_redis: aioredis.Redis, db_session
) -> None:
    holder = await UserFactory.create(db_session)
    alias = await _a_live_alias(fake_redis, holder.id)
    pass_ = await mint_pass(user_id=holder.id, token_version=holder.token_version)
    before = await client.get(_path(alias), headers={**EDGE, "X-Preview-Pass": pass_})

    await revoke_all_sessions(db_session, holder.id)
    await db_session.refresh(holder)
    after = await client.get(_path(alias), headers={**EDGE, "X-Preview-Pass": pass_})

    assert before.headers[CONTAINER_HEADER] == CONTAINER
    assert CONTAINER_HEADER not in after.headers
    assert DENIED_HEADER in after.headers


async def test_a_suspended_holders_pass_is_denied(
    client: httpx.AsyncClient, fake_redis: aioredis.Redis, db_session
) -> None:
    holder = await UserFactory.create(db_session)
    alias = await _a_live_alias(fake_redis, holder.id)
    pass_ = await mint_pass(user_id=holder.id, token_version=holder.token_version)
    before = await client.get(_path(alias), headers={**EDGE, "X-Preview-Pass": pass_})

    holder.suspended_at = datetime.now(UTC)
    await db_session.flush()
    after = await client.get(_path(alias), headers={**EDGE, "X-Preview-Pass": pass_})

    assert before.headers[CONTAINER_HEADER] == CONTAINER
    assert CONTAINER_HEADER not in after.headers
    assert DENIED_HEADER in after.headers


async def test_a_shared_view_opens_for_the_colleague_and_not_the_projects_owner(
    client: httpx.AsyncClient, fake_redis: aioredis.Redis, db_session
) -> None:
    owner = await UserFactory.create(db_session)
    colleague = await UserFactory.create(db_session)
    alias = await _a_live_alias(fake_redis, colleague.id, shared_owner_id=owner.id)
    colleagues = await mint_pass(user_id=colleague.id, token_version=colleague.token_version)
    owners = await mint_pass(user_id=owner.id, token_version=owner.token_version)

    as_colleague = await client.get(_path(alias), headers={**EDGE, "X-Preview-Pass": colleagues})
    as_owner = await client.get(_path(alias), headers={**EDGE, "X-Preview-Pass": owners})

    assert as_colleague.headers[CONTAINER_HEADER] == CONTAINER
    assert CONTAINER_HEADER not in as_owner.headers
    assert DENIED_HEADER in as_owner.headers


async def test_a_dead_alias_is_never_a_denial_even_with_a_good_pass(
    client: httpx.AsyncClient, fake_redis: aioredis.Redis, db_session
) -> None:
    """Only a live alias may send a browser to the hand-over; a dead one answers nothing."""
    alias, holders_browser = await _holding(db_session, fake_redis)
    live = await client.get(_path(alias), headers=holders_browser)
    dead = await client.get(_path(new_alias()), headers=holders_browser)

    assert live.headers[CONTAINER_HEADER] == CONTAINER
    assert CONTAINER_HEADER not in dead.headers
    assert DENIED_HEADER not in dead.headers


async def test_a_hit_renews_the_aliass_lifetime(
    client: httpx.AsyncClient, fake_redis: aioredis.Redis, db_session
) -> None:
    alias, holders_browser = await _holding(db_session, fake_redis)
    await fake_redis.expire(alias_key(alias), 60)

    resp = await client.get(_path(alias), headers=holders_browser)

    assert resp.headers[CONTAINER_HEADER] == CONTAINER
    assert await fake_redis.ttl(alias_key(alias)) > ALIAS_TTL_SECONDS - 60


async def _retire(redis: aioredis.Redis, holder: uuid.UUID) -> None:
    """The container is replaced: the user's registry now holds a newer alias."""
    await redis.hset(registry_key(holder), REGISTRY_FIELD_ALIAS, new_alias())


async def _forget_the_container(redis: aioredis.Redis, holder: uuid.UUID) -> None:
    await redis.delete(registry_key(holder))


@pytest.mark.parametrize(
    "case",
    [
        "no_secret",
        "wrong_secret",
        "a_secret_that_is_not_ascii",
        "an_alias_nobody_minted",
        "an_alias_the_registry_no_longer_holds",
        "an_alias_whose_registry_is_gone",
        "a_container_name_in_the_alias_place",
    ],
)
async def test_every_refusal_is_a_200_that_names_nothing(
    client: httpx.AsyncClient, fake_redis: aioredis.Redis, db_session, case: str
) -> None:
    """Every case carries the holder's own pass, so only the case itself can be what refuses."""
    holder = await UserFactory.create(db_session)
    alias = await _a_live_alias(fake_redis, holder.id)
    pass_ = await mint_pass(user_id=holder.id, token_version=holder.token_version)
    headers: dict[str, str] | dict[bytes, bytes] = {**EDGE, "X-Preview-Pass": pass_}
    asked = alias
    if case == "no_secret":
        headers = {"X-Preview-Pass": pass_}
    elif case == "wrong_secret":
        headers = {"X-Internal-Route-Token": "x" * 40, "X-Preview-Pass": pass_}
    elif case == "a_secret_that_is_not_ascii":
        # Bytes, because httpx will not put a non-ASCII `str` on the wire; a client that is not
        # httpx can.
        headers = {b"X-Internal-Route-Token": "é".encode() * 40, b"X-Preview-Pass": pass_.encode()}
    elif case == "an_alias_nobody_minted":
        asked = new_alias()
    elif case == "an_alias_the_registry_no_longer_holds":
        await _retire(fake_redis, holder.id)
    elif case == "an_alias_whose_registry_is_gone":
        await _forget_the_container(fake_redis, holder.id)
    elif case == "a_container_name_in_the_alias_place":
        asked = CONTAINER

    resp = await client.get(_path(asked), headers=headers)

    assert resp.status_code == 200
    assert CONTAINER_HEADER not in resp.headers
    assert DENIED_HEADER not in resp.headers
    assert CONTAINER not in resp.text


async def test_the_refusals_are_not_a_route_that_never_answers(
    client: httpx.AsyncClient, fake_redis: aioredis.Redis, db_session
) -> None:
    """Liveness for the cases above: the alias that every one of them was refused for answers
    when asked properly."""
    alias, holders_browser = await _holding(db_session, fake_redis)
    refused = await client.get(
        _path(alias), headers={**holders_browser, "X-Internal-Route-Token": "x" * 40}
    )
    answered = await client.get(_path(alias), headers=holders_browser)
    assert CONTAINER_HEADER not in refused.headers
    assert answered.headers[CONTAINER_HEADER] == CONTAINER


async def test_a_wrong_secret_is_its_own_event_and_never_prints_either_value(
    client: httpx.AsyncClient, fake_redis: aioredis.Redis
) -> None:
    alias = await _a_live_alias(fake_redis)
    presented = "presented-" + "x" * 30

    with structlog.testing.capture_logs() as logs:
        await client.get(_path(alias), headers={"X-Internal-Route-Token": presented})

    assert [entry["event"] for entry in logs] == [SECRET_REFUSED_EVENT]
    rendered = repr(logs)
    assert presented not in rendered
    assert settings.INTERNAL_ROUTE_TOKEN.get_secret_value() not in rendered


async def test_a_refused_caller_costs_no_redis_call(
    client: httpx.AsyncClient, fake_redis: aioredis.Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    alias = await _a_live_alias(fake_redis)
    calls: list[str] = []
    real_get = fake_redis.get

    async def counting_get(key: str):
        calls.append(key)
        return await real_get(key)

    monkeypatch.setattr(fake_redis, "get", counting_get)

    await client.get(_path(alias), headers={})
    await client.get(_path(CONTAINER), headers=EDGE)
    assert calls == [], "a wrong caller or a name-shaped alias reached the store"

    await client.get(_path(alias), headers=EDGE)
    assert calls == [alias_key(alias)]


async def test_a_store_that_cannot_answer_is_a_logged_no_not_a_500(
    client: httpx.AsyncClient, fake_redis: aioredis.Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    alias = await _a_live_alias(fake_redis)

    async def unreachable(key: str):
        raise RedisError("the shard is gone")

    monkeypatch.setattr(fake_redis, "get", unreachable)

    with structlog.testing.capture_logs() as logs:
        resp = await client.get(_path(alias), headers=EDGE)

    assert resp.status_code == 200
    assert CONTAINER_HEADER not in resp.headers
    assert LOOKUP_FAILED_EVENT in [entry["event"] for entry in logs]


async def test_the_route_is_not_in_the_published_api_document(
    client: httpx.AsyncClient,
) -> None:
    schema = (await client.get("/openapi.json")).json()
    assert not [path for path in schema["paths"] if path.startswith("/internal")]


async def test_a_mistyped_path_under_the_root_is_a_404_not_the_spa_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>SPA shell</title>")
    monkeypatch.setattr(settings, "spa_dist_dir", dist)
    app: FastAPI = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as spa_client:
        resp = await spa_client.get("/internal/nothing-here")
    assert resp.status_code == 404
    assert "SPA shell" not in resp.text
