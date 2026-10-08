"""The portal edge's two routes: which container a preview's public address stands for, and the
entry that turns a hand-over ticket into a preview pass (ADR-0033).

OUTSIDE `/v1`, `/api` AND `/apps`, so the portal site never proxies them, and out of the OpenAPI
document. They answer only a caller presenting `INTERNAL_ROUTE_TOKEN`; the apps site of the edge
is the one place that does, and it never forwards the value to a browser or an app.
"""

from __future__ import annotations

import hmac
import uuid
from typing import Annotated, Final

import structlog
from fastapi import APIRouter, Header, Response
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError

from src.api.deps import DbSession
from src.config import settings
from src.db.models.user import User
from src.services.auth.preview_pass import (
    GONE_PATH,
    HANDOFF_COOKIE,
    PASS_COOKIE,
    apps_site_redirect,
    mint_pass,
    new_binding,
    pass_ttl_seconds,
    read_pass,
    redeem_ticket,
)
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

DENIED_HEADER: Final = "X-Route-Denied"
"""Present only when the alias is live and the pass is not its holder's: a fresh one-time value the
edge binds the hand-over to."""

SECRET_REFUSED_EVENT: Final = "internal_route_secret_refused"
LOOKUP_FAILED_EVENT: Final = "internal_route_lookup_failed"
TICKET_REFUSED_EVENT: Final = "preview_ticket_refused"
ENTRY_FAILED_EVENT: Final = "preview_entry_failed"


def _the_edge_is_asking(presented: str) -> bool:
    """Constant time over bytes: `compare_digest` raises on a non-ASCII `str`, which would turn a
    malformed header into a 500 instead of a refusal. An empty one never equals the secret."""
    expected = settings.INTERNAL_ROUTE_TOKEN.get_secret_value()
    return hmac.compare_digest(presented.encode(), expected.encode())


async def _holder_of(alias: str) -> tuple[uuid.UUID, str] | None:
    """The user whose registry holds this alias and the container it names, or `None` for anything
    the registry no longer backs. Every Redis call is single-key: production Redis is sharded."""
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
    return owner, str(app_name)


async def _pass_is_holders(raw_pass: str, holder: uuid.UUID, db: DbSession) -> bool:
    record = await read_pass(raw_pass)
    if record is None or record.user_id != holder:
        return False
    user = await db.get(User, holder)
    return (
        user is not None
        and user.suspended_at is None
        and user.token_version == record.token_version
    )


@router.get("/app-routes/{alias}")
async def resolve_app_route(
    alias: str,
    db: DbSession,
    x_internal_route_token: Annotated[str, Header()] = "",
    x_preview_pass: Annotated[str, Header()] = "",
) -> Response:
    """Always 200: the container name when the alias is current and the pass is its holder's, a
    denial when the alias is current and the pass is anyone else's or missing, and no header for an
    unknown or retired alias, a wrong or missing secret, or a store that cannot answer.

    The pass is the user scope the edge cannot apply (ADR-0004): the alias must still be held by
    the registry of the user it was minted for, and the pass must name that same user."""
    if not _the_edge_is_asking(x_internal_route_token):
        _log.warning(SECRET_REFUSED_EVENT)
        return Response()
    if not IS_ALIAS.fullmatch(alias):
        return Response()
    try:
        held = await _holder_of(alias)
        if held is None:
            return Response()
        holder, app_name = held
        if await _pass_is_holders(x_preview_pass, holder, db):
            return Response(headers={CONTAINER_HEADER: app_name})
    except RedisError, RedisNotConfiguredError, SQLAlchemyError:
        _log.error(LOOKUP_FAILED_EVENT, exc_info=True)
        return Response()
    return Response(headers={DENIED_HEADER: new_binding()})


@router.get("/preview-pass")
async def enter_preview(
    x_internal_route_token: Annotated[str, Header()] = "",
    x_preview_ticket: Annotated[str, Header()] = "",
    x_preview_binding: Annotated[str, Header()] = "",
) -> Response:
    """Redeems a hand-over ticket in the browser that asked for it, sets the pass and continues to
    the address first asked for. Every outcome is a redirect: a page rendered here would carry the
    control plane's frame refusal and blank the preview pane."""
    if not _the_edge_is_asking(x_internal_route_token):
        _log.warning(SECRET_REFUSED_EVENT, route="preview-pass")
        return apps_site_redirect(GONE_PATH)
    try:
        ticket = await redeem_ticket(x_preview_ticket, x_preview_binding)
        if ticket is None:
            _log.info(TICKET_REFUSED_EVENT)
            return apps_site_redirect(GONE_PATH)
        raw_pass = await mint_pass(user_id=ticket.user_id, token_version=ticket.token_version)
    except RedisError, RedisNotConfiguredError:
        _log.error(ENTRY_FAILED_EVENT, exc_info=True)
        return apps_site_redirect(GONE_PATH)
    response = apps_site_redirect(ticket.return_path)
    response.set_cookie(
        PASS_COOKIE,
        raw_pass,
        max_age=pass_ttl_seconds(),
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )
    response.delete_cookie(HANDOFF_COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    return response
