"""GET /auth/jwks + POST /auth/app-assertion/preview (issue #92, R3, R6-R8).

Mirrors `test_me.py`'s cookie-auth pattern, plus the CSRF double-submit `auth_headers`
helper `build_sessions/conftest.py` uses for its own mutating POSTs. The preview-mint
endpoint's authorization (owner or superadmin) is exercised directly here rather than
relying on the router's generic AUTH_401 carve-out, since a cross-user id must 404, not
403 (no ownership leak).
"""

from __future__ import annotations

import uuid

from joserfc import jwt
from joserfc.jwk import RSAKey

from src.config import settings
from src.db.models.user import User
from src.services.auth.csrf import issue_csrf_token
from src.services.auth.session_jwt import mint_session_jwt
from tests.factories import AppRegistryFactory, UserFactory

_TTL = settings.auth.access_ttl_seconds


def _auth_headers(user: User, *, with_csrf: bool = True) -> dict[str, str]:
    session_jwt = mint_session_jwt(user.id, user.token_version, _TTL)
    if not with_csrf:
        return {"Cookie": f"session={session_jwt}"}
    csrf = issue_csrf_token(user.id, user.token_version)
    return {"Cookie": f"session={session_jwt}; csrf={csrf}", "X-CSRF-Token": csrf}


async def test_jwks_is_public_and_needs_no_auth(client) -> None:
    resp = await client.get("/v1/auth/jwks")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["keys"]) == 1
    key = body["keys"][0]
    assert key["kty"] == "RSA"
    assert set(key) <= {"kty", "n", "e", "kid", "use", "alg"}  # no private material


async def test_mint_preview_requires_a_session(client, db_session) -> None:
    user = await UserFactory.create(db_session)
    app = await AppRegistryFactory.create(db_session, user_id=user.id)
    resp = await client.post("/v1/auth/app-assertion/preview", json={"app_id": str(app.id)})
    assert resp.status_code == 401


async def test_mint_preview_requires_csrf(client, db_session) -> None:
    user = await UserFactory.create(db_session)
    app = await AppRegistryFactory.create(db_session, user_id=user.id)
    resp = await client.post(
        "/v1/auth/app-assertion/preview",
        json={"app_id": str(app.id)},
        headers=_auth_headers(user, with_csrf=False),
    )
    assert resp.status_code == 403


async def test_owner_mints_a_verifiable_preview_assertion(client, db_session) -> None:
    user = await UserFactory.create(db_session, email="owner@rvaiglobal.com")
    app = await AppRegistryFactory.create(db_session, user_id=user.id)

    resp = await client.post(
        "/v1/auth/app-assertion/preview",
        json={"app_id": str(app.id)},
        headers=_auth_headers(user),
    )
    assert resp.status_code == 200
    assertion = resp.json()["assertion"]

    jwks = (await client.get("/v1/auth/jwks")).json()
    public_key = RSAKey.import_key(jwks["keys"][0])
    claims = jwt.decode(assertion, public_key, algorithms=["RS256"]).claims
    assert claims["aud"] == str(app.id)
    assert claims["plane"] == "preview"
    assert claims["email"] == "owner@rvaiglobal.com"
    assert claims["sub"] == user.azure_oid


async def test_a_non_owner_gets_404_not_someone_elses_assertion(client, db_session) -> None:
    owner = await UserFactory.create(db_session, email="owner2@rvaiglobal.com")
    stranger = await UserFactory.create(db_session, email="stranger@rvaiglobal.com")
    app = await AppRegistryFactory.create(db_session, user_id=owner.id)

    resp = await client.post(
        "/v1/auth/app-assertion/preview",
        json={"app_id": str(app.id)},
        headers=_auth_headers(stranger),
    )
    assert resp.status_code == 404


async def test_a_superadmin_may_mint_for_an_app_they_do_not_own(client, db_session) -> None:
    owner = await UserFactory.create(db_session, email="owner3@rvaiglobal.com")
    admin_email = next(iter(settings.superadmin_emails))
    admin = await UserFactory.create(db_session, email=admin_email)
    app = await AppRegistryFactory.create(db_session, user_id=owner.id)

    resp = await client.post(
        "/v1/auth/app-assertion/preview",
        json={"app_id": str(app.id)},
        headers=_auth_headers(admin),
    )
    assert resp.status_code == 200


async def test_a_missing_app_is_404(client, db_session) -> None:
    user = await UserFactory.create(db_session)

    resp = await client.post(
        "/v1/auth/app-assertion/preview",
        json={"app_id": str(uuid.uuid4())},
        headers=_auth_headers(user),
    )
    assert resp.status_code == 404
