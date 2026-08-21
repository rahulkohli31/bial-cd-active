"""GUARDS: the citizen submit route is GONE (U8: R15a, ASM18).

`POST /apps/{app_id}/submit` was the only backend writer of the pending status.
Retiring it — rather than hiding its button — is what makes "exactly one route into
the queue" true: reachable, it would let a queue item arrive with no declaration
attached. Per the repo's retire-a-behaviour convention
(`docs/solutions/conventions/cleanly-removing-dead-ui-controls-2026-06-23.md`, the
same flip `test_lifecycle.py` carries for `POST /apps/provision`), the route's tests
become guards that it stays gone: if any of these fails, someone reinstated the
route, and the R15a invariant fails with it.

The BEHAVIOUR the route carried is not gone — it lives in
`services/approvals/submit.py` and is proved at
`tests/services/approvals/test_submit.py`.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.db.models.app_registry import AppRegistry, AppStatus
from src.main import create_app
from src.services.auth.session_jwt import mint_session_jwt
from src.services.build_sessions.appdata import resolve_app_for_project
from src.services.storage import snapshot_key
from tests.factories import ProjectFactory, UserFactory

_TTL = settings.auth.access_ttl_seconds

_SHA = "ab" * 20
_BUNDLE = b"# v2 git bundle\n" + _SHA.encode() + b" HEAD\n\nPACK-fake-bytes"


def _cookie(jwt: str) -> dict[str, str]:
    return {"Cookie": f"session={jwt}"}


async def _auth_user(db: AsyncSession, **overrides: object):
    user = await UserFactory.create(db, **overrides)
    return user, _cookie(mint_session_jwt(user.id, user.token_version, _TTL))


async def _provision_app(db_session, user) -> str:
    project = await ProjectFactory.create(db_session, user.id)
    app_id = await resolve_app_for_project(db_session, user.id, project.id)
    await db_session.commit()
    return str(app_id)


async def test_the_submit_route_is_gone_even_for_the_owner_with_a_valid_bundle(
    client, db_session, fake_storage
) -> None:
    # The strongest reinstatement probe: everything the retired route needed to
    # succeed is in place — the owner, the app, a valid staged bundle — and the
    # answer is still "no such route", never a submission.
    user, headers = await _auth_user(db_session)
    app_id = await _provision_app(db_session, user)
    fake_storage.objects[snapshot_key(uuid.UUID(app_id))] = _BUNDLE

    resp = await client.post(f"/v1/apps/{app_id}/submit", headers=headers)

    assert resp.status_code in (404, 405)
    # And it did NOTHING: no immutable copy, no row change — the row would move to
    # pending if a half-removed handler were still reachable.
    assert list(fake_storage.objects) == [snapshot_key(uuid.UUID(app_id))]
    row = await db_session.get(AppRegistry, uuid.UUID(app_id))
    await db_session.refresh(row)
    assert row.status is AppStatus.DRAFT
    assert row.source_submission_id is None
    assert row.declaration is None
    assert row.approval_route is None


async def test_the_submit_route_is_gone_unauthenticated_too(client) -> None:
    # No auth-shaped answer either (a 401 would mean a handler still guards it).
    resp = await client.post(f"/v1/apps/{uuid.uuid4()}/submit")
    assert resp.status_code in (404, 405)


def test_the_submit_route_is_gone_from_the_openapi_schema() -> None:
    # Retired means UNDOCUMENTED: the schema advertises no submit path, so no
    # client is invited to call one (the same flip U6 applied to provision/source).
    paths = create_app().openapi()["paths"]
    assert "/v1/apps/{app_id}/submit" not in paths
    submit_shaped = [p for p in paths if p.startswith("/v1/apps") and "submit" in p]
    assert submit_shaped == []
