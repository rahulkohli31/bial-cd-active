"""The edge's alias lookup: alias in, container name out, and only for the edge.

The answer is a header, never a body, and EVERY refusal is a 200 with no header: the edge reads
a 200 from its subrequest as data and anything else as an error it cannot place. So each
refusal below is paired with a successful lookup for the same alias, because a route that
answered nothing at all would satisfy "no header" for every case.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import httpx
import pytest
import redis.asyncio as aioredis
import structlog.testing
from fastapi import FastAPI
from pydantic import SecretStr
from redis.exceptions import RedisError

from src.api.internal.router import CONTAINER_HEADER, LOOKUP_FAILED_EVENT, SECRET_REFUSED_EVENT
from src.config import settings
from src.main import create_app
from src.services.redis import registry_key
from src.services.redis.keys import (
    ALIAS_TTL_SECONDS,
    REGISTRY_FIELD_ALIAS,
    alias_key,
)
from src.services.sandbox.base import new_alias
from src.services.sandbox.client import AcaSandboxClient
from src.services.sandbox.config import SandboxConfig
from tests.fakes import a_sandbox_name

USER = uuid.uuid4()
CONTAINER = a_sandbox_name("lookup")
EDGE = {"X-Internal-Route-Token": settings.INTERNAL_ROUTE_TOKEN.get_secret_value()}


def _path(alias: str) -> str:
    return f"/internal/app-routes/{alias}"


async def _a_live_alias(redis: aioredis.Redis) -> str:
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
        USER,
        app_name=CONTAINER,
        app_id=uuid.uuid4(),
        alias=alias,
        fqdn=f"{CONTAINER}.example",
        token_ref="ref",
    )
    return alias


async def test_the_edge_is_told_which_container_a_current_alias_stands_for(
    client: httpx.AsyncClient, fake_redis: aioredis.Redis
) -> None:
    alias = await _a_live_alias(fake_redis)

    resp = await client.get(_path(alias), headers=EDGE)

    assert resp.status_code == 200
    assert resp.headers[CONTAINER_HEADER] == CONTAINER
    assert CONTAINER not in resp.text


async def test_a_hit_renews_the_aliass_lifetime(
    client: httpx.AsyncClient, fake_redis: aioredis.Redis
) -> None:
    alias = await _a_live_alias(fake_redis)
    await fake_redis.expire(alias_key(alias), 60)

    await client.get(_path(alias), headers=EDGE)

    assert await fake_redis.ttl(alias_key(alias)) > ALIAS_TTL_SECONDS - 60


async def _retire(redis: aioredis.Redis, alias: str) -> None:
    """The container is replaced: the user's registry now holds a newer alias."""
    await redis.hset(registry_key(USER), REGISTRY_FIELD_ALIAS, new_alias())


async def _forget_the_container(redis: aioredis.Redis, alias: str) -> None:
    await redis.delete(registry_key(USER))


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
    client: httpx.AsyncClient, fake_redis: aioredis.Redis, case: str
) -> None:
    alias = await _a_live_alias(fake_redis)
    headers: dict[str, str] | dict[bytes, bytes] = dict(EDGE)
    asked = alias
    if case == "no_secret":
        headers = {}
    elif case == "wrong_secret":
        headers = {"X-Internal-Route-Token": "x" * 40}
    elif case == "a_secret_that_is_not_ascii":
        # Bytes, because httpx will not put a non-ASCII `str` on the wire; a client that is not
        # httpx can.
        headers = {b"X-Internal-Route-Token": "é".encode() * 40}
    elif case == "an_alias_nobody_minted":
        asked = new_alias()
    elif case == "an_alias_the_registry_no_longer_holds":
        await _retire(fake_redis, alias)
    elif case == "an_alias_whose_registry_is_gone":
        await _forget_the_container(fake_redis, alias)
    elif case == "a_container_name_in_the_alias_place":
        asked = CONTAINER

    resp = await client.get(_path(asked), headers=headers)

    assert resp.status_code == 200
    assert CONTAINER_HEADER not in resp.headers
    assert CONTAINER not in resp.text


async def test_the_refusals_are_not_a_route_that_never_answers(
    client: httpx.AsyncClient, fake_redis: aioredis.Redis
) -> None:
    """Liveness for the cases above: the alias that every one of them was refused for answers
    when asked properly."""
    alias = await _a_live_alias(fake_redis)
    refused = await client.get(_path(alias), headers={"X-Internal-Route-Token": "x" * 40})
    answered = await client.get(_path(alias), headers=EDGE)
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
