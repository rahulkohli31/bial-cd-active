"""POST /v1/admin/apps/reconcile-sandboxes — the Azure-side fleet reconcile (#83 follow-up).

The mechanics of the diff are pinned service-side in
`tests/services/build_sessions/test_inventory.py`; this file pins the ROUTE: who may call it,
what the wire body looks like, what reaches the audit trail, and which failures are retryable.

It exists because the first cut had none — the endpoint shipped with unit coverage of
`take_sandbox_inventory` and nothing that ever issued the request, so the gate, the envelope
and the audit row were correct only by inspection. Its two siblings
(`test_storage_reconcile.py`, `test_database_reconcile.py`) each carry this set, and
`.claude/rules/testing.md` asks for RBAC to be tested explicitly rather than reasoned about.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.v1.build_sessions.deps import sandbox_or_none_dependency
from src.config import settings
from src.db.models.audit import AuditLog
from src.services.auth.session_jwt import mint_session_jwt
from src.services.build_sessions.manager import app_name_for
from src.services.redis import REGISTRY_STATE_READY, registry_key
from src.services.redis.keys import REGISTRY_FIELD_APP_NAME, REGISTRY_FIELD_STATE
from src.services.sandbox import SandboxError
from src.services.sandbox.base import FleetMember
from tests.factories import UserFactory
from tests.fakes import a_fleet_member

_TTL = settings.auth.access_ttl_seconds
_RECONCILE = "/v1/admin/apps/reconcile-sandboxes"


def _cookie(jwt: str) -> dict[str, str]:
    return {"Cookie": f"session={jwt}"}


async def _admin(db: AsyncSession) -> dict[str, str]:
    # The .env.test allowlist contains admin@bial.com → super-admin.
    user = await UserFactory.create(db, email="admin@bial.com")
    return _cookie(mint_session_jwt(user.id, user.token_version, _TTL))


async def _citizen(db: AsyncSession) -> dict[str, str]:
    user = await UserFactory.create(db, email="nobody@rvaiglobal.com")
    return _cookie(mint_session_jwt(user.id, user.token_version, _TTL))


class _Fleet:
    """A sandbox client that can enumerate — `FleetLister` by shape, which is all the route
    checks. Not a `SandboxClient` subclass on purpose: the route's `isinstance(sandbox,
    FleetLister)` runtime check is exactly what a future substrate has to satisfy, and a test
    that inherited the real ABC would stop exercising it."""

    def __init__(self, names: list[str], *, error: Exception | None = None) -> None:
        self.names = names
        self.error = error

    async def list_sandbox_fleet(self) -> list[FleetMember]:
        if self.error is not None:
            raise self.error
        return [a_fleet_member(n) for n in self.names]


class _CannotEnumerate:
    """A deployment whose sandbox client has no fleet capability at all."""


def _wire(app, sandbox: object) -> None:
    app.dependency_overrides[sandbox_or_none_dependency] = lambda: sandbox


async def _register(fake_redis, user_id: uuid.UUID, app_name: str) -> None:
    await fake_redis.hset(
        registry_key(user_id),
        mapping={
            REGISTRY_FIELD_APP_NAME: app_name,
            REGISTRY_FIELD_STATE: REGISTRY_STATE_READY,
        },
    )


# --- the gate ---------------------------------------------------------------------


async def test_citizen_is_forbidden(client, app, db_session, fake_redis) -> None:
    _wire(app, _Fleet([]))
    assert (await client.post(_RECONCILE, headers=await _citizen(db_session))).status_code == 403


async def test_unauthenticated_is_401(client, app, db_session, fake_redis) -> None:
    _wire(app, _Fleet([]))
    assert (await client.post(_RECONCILE)).status_code == 401


# --- the body ---------------------------------------------------------------------


async def test_the_orphan_is_named_in_the_response(client, app, db_session, fake_redis) -> None:
    """THE LEAK, over HTTP. A container nothing tracks bills forever because `sweep_all` walks
    the registry and never sees it. The operator has to be told its NAME — they are the one who
    deletes it — which is the deliberate divergence from `reconcile-storage`'s counts-only body."""
    admin = await _admin(db_session)
    tracked_user, orphan_app = uuid.uuid7(), uuid.uuid7()
    tracked_name = app_name_for(uuid.uuid7())
    orphan_name = app_name_for(orphan_app)
    _wire(app, _Fleet([tracked_name, orphan_name]))
    await _register(fake_redis, tracked_user, tracked_name)

    body = (await client.post(_RECONCILE, headers=admin)).json()
    assert body["live"] == 2
    assert body["registered"] == 1
    assert body["unregistered"] == [orphan_name]
    assert body["registeredMissing"] == []


async def test_a_registry_entry_whose_container_is_gone(
    client, app, db_session, fake_redis
) -> None:
    # The opposite gap and far less urgent — the next `reconcile_user` clears it — but it is
    # still a true statement about drift, so it is reported rather than swallowed.
    admin = await _admin(db_session)
    ghost = app_name_for(uuid.uuid7())
    _wire(app, _Fleet([]))
    await _register(fake_redis, uuid.uuid7(), ghost)

    body = (await client.post(_RECONCILE, headers=admin)).json()
    assert body["registeredMissing"] == [ghost]
    assert body["unregistered"] == []


async def test_a_clean_fleet_reports_nothing(client, app, db_session, fake_redis) -> None:
    admin = await _admin(db_session)
    user_id = uuid.uuid7()
    name = app_name_for(uuid.uuid7())
    _wire(app, _Fleet([name]))
    await _register(fake_redis, user_id, name)

    body = (await client.post(_RECONCILE, headers=admin)).json()
    assert body == {"live": 1, "registered": 1, "unregistered": [], "registeredMissing": []}


# --- the audit trail --------------------------------------------------------------


async def test_the_audit_row_carries_counts_but_no_names(
    client, app, db_session, fake_redis
) -> None:
    """A sandbox name embeds its app's uuid, so a name list in the audit trail is a durable
    inventory of who was running what. The response carries names because the operator must act
    on them; the audit row carries counts only — the same split `reconcile-storage` makes for
    blob keys (`.claude/rules/security.md`)."""
    admin = await _admin(db_session)
    orphan_name = app_name_for(uuid.uuid7())
    _wire(app, _Fleet([orphan_name]))

    assert (await client.post(_RECONCILE, headers=admin)).status_code == 200
    row = await db_session.scalar(select(AuditLog).where(AuditLog.action == "sandbox:reconcile"))
    assert row is not None
    assert row.resource_type == "sandbox"
    assert row.detail is not None
    assert row.detail == {"live": 1, "registered": 0, "unregistered": 1, "registeredMissing": 0}
    # Counts only — the name must appear nowhere in the row.
    assert orphan_name not in str(row.detail)


# --- the failure surface ----------------------------------------------------------


async def test_a_failed_enumeration_is_503_not_a_partial_answer(
    client, app, db_session, fake_redis
) -> None:
    # A half-read fleet is indistinguishable from a clean one, and "clean" is the answer that
    # gets an orphan forgotten. Refuse rather than under-report.
    admin = await _admin(db_session)
    _wire(app, _Fleet([], error=SandboxError("arm blip")))

    resp = await client.post(_RECONCILE, headers=admin)
    assert resp.status_code == 503
    assert "try again" in resp.json()["error"]["message"].lower()


async def test_an_unconfigured_sandbox_is_503(client, app, db_session, fake_redis) -> None:
    admin = await _admin(db_session)
    _wire(app, None)
    assert (await client.post(_RECONCILE, headers=admin)).status_code == 503


async def test_a_client_that_cannot_enumerate_is_503_not_500(
    client, app, db_session, fake_redis
) -> None:
    # Nothing is wrong with the request — this deployment simply cannot answer it — so the
    # shape is retryable rather than a crash.
    admin = await _admin(db_session)
    _wire(app, _CannotEnumerate())

    resp = await client.post(_RECONCILE, headers=admin)
    assert resp.status_code == 503
    assert resp.status_code != 500


async def test_no_redis_is_the_declared_503_not_a_500(client, app, db_session) -> None:
    """BLOCKER 1 REGRESSION (#83 review). `build_coordination_or_503` SKIPS its body on an
    unconfigured Redis and resumes after it, so a route whose `return` lives inside the block
    falls off the end and returns None against a non-optional response model — FastAPI's
    response validation then raises, and the route that documents a 503 answered 500.

    Deliberately takes no `fake_redis` fixture: with the singleton unset, `get_redis()` raises
    `RedisNotConfiguredError` exactly as it would on a deployment with no Redis configured.
    Every sibling route ends with the trailing `raise coordination_is_gone()`; this one did not."""
    admin = await _admin(db_session)
    _wire(app, _Fleet([app_name_for(uuid.uuid7())]))

    resp = await client.post(_RECONCILE, headers=admin)
    assert resp.status_code == 503, "an unconfigured Redis must not surface as a 500"
    assert "try again" in resp.json()["error"]["message"].lower()


def test_openapi_documents_the_route() -> None:
    from src.main import create_app

    responses = set(create_app().openapi()["paths"][_RECONCILE]["post"]["responses"])
    assert {"200", "503", "401", "403"} <= responses
