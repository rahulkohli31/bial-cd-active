"""GET /v1/usage/today — the SPA daily-cap badge read (U6). Cookie auth; byte-stable
{used, limit, remaining, resetsAt} shape.
"""

from __future__ import annotations

from src.config import settings
from src.db.models.user_limit import UserLimit
from src.main import create_app
from src.services.auth.session_jwt import mint_session_jwt
from src.services.usage.gate import record_usage
from tests.factories import UserFactory

_TTL = settings.auth.access_ttl_seconds


def _cookie(jwt: str) -> dict[str, str]:
    return {"Cookie": f"session={jwt}"}


async def test_usage_today_default_limit_no_usage(client, db_session) -> None:
    user = await UserFactory.create(db_session)
    jwt = mint_session_jwt(user.id, user.token_version, _TTL)

    resp = await client.get("/v1/usage/today", headers=_cookie(jwt))
    assert resp.status_code == 200
    body = resp.json()
    # Exactly the four contract fields the SPA reads.
    assert set(body) == {"used", "limit", "remaining", "resetsAt"}
    assert body["used"] == 0
    assert body["limit"] == settings.DAILY_TOKEN_LIMIT
    assert body["remaining"] == settings.DAILY_TOKEN_LIMIT
    assert body["resetsAt"].endswith("Z")


async def test_usage_today_reflects_recorded_usage_and_override(client, db_session) -> None:
    user = await UserFactory.create(db_session)
    db_session.add(UserLimit(user_id=user.id, daily_token_limit=1000))
    # Cache tokens fold into `used`, so 100+50+30+20 = 200.
    await record_usage(
        db_session,
        user.id,
        input_tokens=100,
        output_tokens=50,
        cache_read_tokens=30,
        cache_write_tokens=20,
    )
    jwt = mint_session_jwt(user.id, user.token_version, _TTL)

    resp = await client.get("/v1/usage/today", headers=_cookie(jwt))
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "used": 200,
        "limit": 1000,
        "remaining": 800,
        "resetsAt": body["resetsAt"],  # value asserted in the gate unit test
    }


async def test_usage_today_requires_auth(client) -> None:
    resp = await client.get("/v1/usage/today")
    assert resp.status_code == 401


def test_usage_today_documents_401_in_openapi() -> None:
    # Covers AE4: the inherited `current_user` 401 is documented in the route's
    # OpenAPI `responses=` even though the raise originates in the dependency.
    responses = create_app().openapi()["paths"]["/v1/usage/today"]["get"]["responses"]
    assert "401" in responses
