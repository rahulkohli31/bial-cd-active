"""POST /v1/feedback — validation, author-from-token, page sanitization, rate limit (U7).
Byte-stable with the Express `/api/feedback` contract.
"""

from __future__ import annotations

from sqlalchemy import select

from src.config import settings
from src.db.models.feedback import Feedback
from src.db.models.user import User
from src.services.auth.session_jwt import mint_session_jwt
from tests.factories import UserFactory

_TTL = settings.auth.access_ttl_seconds


def _cookie(jwt: str) -> dict[str, str]:
    return {"Cookie": f"session={jwt}"}


async def _auth(db_session) -> tuple[dict[str, str], User]:
    user = await UserFactory.create(db_session)
    return _cookie(mint_session_jwt(user.id, user.token_version, _TTL)), user


async def _stored(db_session, user) -> list[Feedback]:
    rows = await db_session.execute(select(Feedback).where(Feedback.user_id == user.id))
    return list(rows.scalars())


# --- happy path ---------------------------------------------------------------


async def test_submit_persists_author_scoped(client, db_session) -> None:
    headers, user = await _auth(db_session)
    resp = await client.post(
        "/v1/feedback", headers=headers, json={"message": "  great app  ", "page": "/chat"}
    )
    assert resp.status_code == 201
    assert resp.json() == {"ok": True}
    rows = await _stored(db_session, user)
    assert len(rows) == 1
    assert rows[0].message == "great app"  # trimmed
    assert rows[0].page == "/chat"


async def test_author_is_caller_not_body(client, db_session) -> None:
    # A body-supplied username must be ignored — author is the verified caller.
    headers, user = await _auth(db_session)
    resp = await client.post(
        "/v1/feedback",
        headers=headers,
        json={"message": "hi", "username": "attacker@evil.test"},
    )
    assert resp.status_code == 201
    rows = await _stored(db_session, user)
    assert len(rows) == 1
    assert rows[0].user_id == user.id


# --- validation (exact Express 400 messages) ----------------------------------


async def test_missing_message_400(client, db_session) -> None:
    headers, _ = await _auth(db_session)
    resp = await client.post("/v1/feedback", headers=headers, json={})
    assert resp.status_code == 400
    assert resp.json() == {"error": {"message": "Feedback message is required."}}


async def test_non_string_message_400(client, db_session) -> None:
    headers, _ = await _auth(db_session)
    resp = await client.post("/v1/feedback", headers=headers, json={"message": 123})
    assert resp.status_code == 400
    assert resp.json() == {"error": {"message": "Feedback message is required."}}


async def test_empty_after_trim_400(client, db_session) -> None:
    headers, _ = await _auth(db_session)
    resp = await client.post("/v1/feedback", headers=headers, json={"message": "   "})
    assert resp.status_code == 400
    assert resp.json() == {"error": {"message": "Feedback message cannot be empty."}}


async def test_message_byte_cap(client, db_session) -> None:
    headers, _ = await _auth(db_session)
    # 'é' is 2 UTF-8 bytes: 2000 → 4000 bytes passes, 2001 → 4002 bytes fails.
    ok = await client.post("/v1/feedback", headers=headers, json={"message": "é" * 2000})
    assert ok.status_code == 201
    too_long = await client.post("/v1/feedback", headers=headers, json={"message": "é" * 2001})
    assert too_long.status_code == 400
    assert too_long.json() == {
        "error": {"message": "Feedback message is too long (max 4000 characters)."}
    }


# --- page sanitization --------------------------------------------------------


async def test_page_sanitization(client, db_session) -> None:
    headers, user = await _auth(db_session)
    cases = [
        ("/admin/feedback", "/admin/feedback"),  # valid same-origin path kept
        ("javascript:alert(1)", ""),  # not a path
        ("//evil.example/x", ""),  # protocol-relative dropped
        ("chat", ""),  # no leading slash
        (12345, ""),  # non-string
    ]
    for i, (sent, _expected) in enumerate(cases):
        resp = await client.post(
            "/v1/feedback", headers=headers, json={"message": f"m{i}", "page": sent}
        )
        assert resp.status_code == 201, sent
    rows = sorted(await _stored(db_session, user), key=lambda r: r.message)
    assert [r.page for r in rows] == [c[1] for c in cases]


async def test_page_defaults_empty_when_absent(client, db_session) -> None:
    headers, user = await _auth(db_session)
    resp = await client.post("/v1/feedback", headers=headers, json={"message": "no page"})
    assert resp.status_code == 201
    rows = await _stored(db_session, user)
    assert rows[0].page == ""


# --- rate limit + auth --------------------------------------------------------


async def test_rate_limit_enforced(client, db_session) -> None:
    from src.api.v1.feedback.router import FEEDBACK_RATE_LIMIT

    headers, _ = await _auth(db_session)
    for _ in range(FEEDBACK_RATE_LIMIT):
        resp = await client.post("/v1/feedback", headers=headers, json={"message": "spam"})
        assert resp.status_code == 201
    blocked = await client.post("/v1/feedback", headers=headers, json={"message": "spam"})
    assert blocked.status_code == 429
    assert blocked.json() == {
        "error": {"message": "Too many feedback submissions. Please try again later."}
    }


async def test_requires_auth(client) -> None:
    resp = await client.post("/v1/feedback", json={"message": "hi"})
    assert resp.status_code == 401


def test_feedback_openapi_documents_codes_and_request_body() -> None:
    from src.main import create_app

    op = create_app().openapi()["paths"]["/v1/feedback"]["post"]
    assert {"400", "401", "429", "500"} <= set(op["responses"])
    assert "201" in op["responses"]  # success body documented (documented-only model)
    # The raw-parse body is documented from the model via openapi_extra.
    props = op["requestBody"]["content"]["application/json"]["schema"]["properties"]
    assert "message" in props
