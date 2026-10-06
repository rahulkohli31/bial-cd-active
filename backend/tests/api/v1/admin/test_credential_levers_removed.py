"""The admin credential levers are gone, for every caller.

Publishing injects the database and storage credentials itself, so nothing needs an
administrator to read either one back. A route that answered here would hand a live
credential to a person.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.db.models.audit import AuditLog
from src.services.auth.session_jwt import mint_session_jwt
from tests.factories import AppRegistryFactory, UserFactory

_TTL = settings.auth.access_ttl_seconds


@pytest.mark.parametrize("lever", ["database-credential", "deploy-credential"])
@pytest.mark.parametrize(
    ("email", "sibling_status"), [("admin@bial.com", 200), ("nobody@rvaiglobal.com", 403)]
)
async def test_the_lever_does_not_exist(
    client: AsyncClient, db_session: AsyncSession, lever: str, email: str, sibling_status: int
) -> None:
    owner = await UserFactory.create(db_session)
    app = await AppRegistryFactory.create(db_session, user_id=owner.id)
    caller = await UserFactory.create(db_session, email=email)
    headers = {"Cookie": f"session={mint_session_jwt(caller.id, caller.token_version, _TTL)}"}

    resp = await client.post(f"/v1/admin/apps/{app.id}/{lever}", headers=headers)

    assert resp.status_code == 404
    audited = await db_session.scalar(sa.select(sa.func.count()).select_from(AuditLog))
    assert audited == 0
    # The same caller reaches a sibling route on the same app, so the 404 is the lever's alone.
    sibling = await client.get(f"/v1/admin/apps/{app.id}/history", headers=headers)
    assert sibling.status_code == sibling_status
