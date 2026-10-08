"""The preview pass, and the one-time ticket that carries a signed-in person to it (ADR-0033).

A pass tells the apps site which person a browser belongs to; a ticket moves a person from the
portal's site to the apps site once, in the browser that asked. Both are random values stored only
as their sha256, each record is one Redis key with a TTL, and every command is single-key.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import uuid
from collections.abc import Callable
from typing import Final

from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict

from src.config import settings
from src.services.redis import get_redis
from src.services.redis.keys import preview_pass_key, preview_ticket_key

PASS_COOKIE: Final = "__Host-bial_pass"
HANDOFF_COOKIE: Final = "__Host-bial_handoff"

# The apps site's own pages for these hops (`portal/nginx.conf`): neither ever redirects.
ENTRY_PATH: Final = "/__bial_enter"
GONE_PATH: Final = "/__bial_gone"

TICKET_TTL_SECONDS: Final = 60

_TOKEN_BYTES: Final = 32
_BINDING_BYTES: Final = 16
# The shapes those byte counts produce, which the edge checks too: 43 url-safe characters, 32 hex.
IS_TOKEN: Final = re.compile(r"[A-Za-z0-9_-]{43}")
IS_BINDING: Final = re.compile(r"[0-9a-f]{32}")


class PassRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: uuid.UUID
    token_version: int


class TicketRecord(PassRecord):
    binding: str
    return_path: str


def pass_ttl_seconds() -> int:
    """As long as the portal's longest session, never renewed by use."""
    return settings.auth.absolute_session_seconds


def new_binding() -> str:
    return secrets.token_hex(_BINDING_BYTES)


def apps_site_redirect(path: str) -> RedirectResponse:
    return RedirectResponse(f"{settings.APPS_BASE_URL}{path}", status_code=302)


def _digest(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


async def _store(key_for: Callable[[str], str], record: BaseModel, ttl: int) -> str:
    raw = secrets.token_urlsafe(_TOKEN_BYTES)
    await get_redis().set(key_for(_digest(raw)), record.model_dump_json(), ex=ttl)
    return raw


async def mint_ticket(
    *, user_id: uuid.UUID, token_version: int, binding: str, return_path: str
) -> str:
    record = TicketRecord(
        user_id=user_id, token_version=token_version, binding=binding, return_path=return_path
    )
    return await _store(preview_ticket_key, record, TICKET_TTL_SECONDS)


async def redeem_ticket(raw: str, binding: str) -> TicketRecord | None:
    """The ticket's record when it exists and was issued against `binding`; it is spent either way,
    so a ticket carried into another browser cannot be retried with the right value later."""
    if not IS_TOKEN.fullmatch(raw):
        return None
    stored = await get_redis().getdel(preview_ticket_key(_digest(raw)))
    if stored is None:
        return None
    record = TicketRecord.model_validate_json(stored)
    if not hmac.compare_digest(record.binding.encode(), binding.encode()):
        return None
    return record


async def mint_pass(*, user_id: uuid.UUID, token_version: int) -> str:
    record = PassRecord(user_id=user_id, token_version=token_version)
    return await _store(preview_pass_key, record, pass_ttl_seconds())


async def read_pass(raw: str) -> PassRecord | None:
    if not IS_TOKEN.fullmatch(raw):
        return None
    stored = await get_redis().get(preview_pass_key(_digest(raw)))
    return None if stored is None else PassRecord.model_validate_json(stored)
