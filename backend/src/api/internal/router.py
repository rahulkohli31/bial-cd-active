"""The portal edge's alias lookup: which container a preview's public address stands for.

OUTSIDE `/v1`, `/api` AND `/apps`, so the portal site never proxies it, and out of the OpenAPI
document. It answers only a caller presenting `INTERNAL_ROUTE_TOKEN`; the apps site of the edge
is the one place that does, and it never forwards the value to a browser or an app.
"""

from __future__ import annotations

import hmac
import uuid
from typing import Annotated, Final

import structlog
from fastapi import APIRouter, Header, Response
from redis.exceptions import RedisError

from src.config import settings
from src.services.redis import RedisNotConfiguredError, get_redis, registry_key
from src.services.redis.keys import (
    ALIAS_TTL_SECONDS,
    IS_ALIAS,
    REGISTRY_FIELD_ALIAS,
    REGISTRY_FIELD_APP_NAME,
    alias_key,
)

_log = structlog.get_logger()

router = APIRouter(prefix="/internal", include_in_schema=False)

CONTAINER_HEADER: Final = "X-App-Container"
"""The response header that carries the container name. Absent on every kind of no."""

SECRET_REFUSED_EVENT: Final = "internal_route_secret_refused"
LOOKUP_FAILED_EVENT: Final = "internal_route_lookup_failed"


def _the_edge_is_asking(presented: str) -> bool:
    """Constant time over bytes: `compare_digest` raises on a non-ASCII `str`, which would turn a
    malformed header into a 500 instead of a refusal. An empty one never equals the secret."""
    expected = settings.INTERNAL_ROUTE_TOKEN.get_secret_value()
    return hmac.compare_digest(presented.encode(), expected.encode())


async def _container_behind(alias: str) -> str | None:
    """The container name this alias stands for, or `None` for anything the registry no longer
    backs. Every Redis call is single-key: production Redis is sharded."""
    redis = get_redis()
    reverse_key = alias_key(alias)
    raw_owner = await redis.get(reverse_key)
    if raw_owner is None:
        return None
    try:
        owner = uuid.UUID(str(raw_owner))
    except ValueError:
        return None
    held_alias, app_name = await redis.hmget(
        registry_key(owner), [REGISTRY_FIELD_ALIAS, REGISTRY_FIELD_APP_NAME]
    )
    if held_alias != alias or not app_name:
        return None
    await redis.expire(reverse_key, ALIAS_TTL_SECONDS)
    return str(app_name)


@router.get("/app-routes/{alias}")
async def resolve_app_route(
    alias: str,
    x_internal_route_token: Annotated[str, Header()] = "",
) -> Response:
    """Always 200: the container name in `X-App-Container` when the alias is current, no header
    for an unknown or retired alias, a wrong or missing secret, or a store that cannot answer.

    NO USER SCOPE, BY DESIGN (ADR-0004): the caller is the edge, which holds no user, and the
    alias is the only thing it can name. The registry of the user the alias belongs to must
    still hold it, so a retired alias answers no whatever is left in the reverse key."""
    if not _the_edge_is_asking(x_internal_route_token):
        _log.warning(SECRET_REFUSED_EVENT)
        return Response()
    if not IS_ALIAS.fullmatch(alias):
        return Response()
    try:
        app_name = await _container_behind(alias)
    except RedisError, RedisNotConfiguredError:
        _log.error(LOOKUP_FAILED_EVENT, exc_info=True)
        return Response()
    if app_name is None:
        return Response()
    return Response(headers={CONTAINER_HEADER: app_name})
