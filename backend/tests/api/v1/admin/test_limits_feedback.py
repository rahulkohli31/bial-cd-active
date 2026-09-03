"""Admin per-user limits + feedback read (U9, R28): super-admin-only; overrides take
effect on the LIVE daily gate; clearing falls back to the default; feedback is capped
with a true total; limit edits are audited."""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.db.models.audit import AuditLog
from src.db.models.feedback import Feedback
from src.db.models.user_limit import UserLimit
from src.main import create_app
from src.services.auth.session_jwt import mint_session_jwt
from src.services.usage.context_window import occupied_window
from src.services.usage.gate import effective_daily_limit
from src.services.usage.limits import CONTEXT_HARD_FLOOR, effective_context
from tests.factories import UserFactory

_TTL = settings.auth.access_ttl_seconds


def _cookie(jwt: str) -> dict[str, str]:
    return {"Cookie": f"session={jwt}"}


async def _admin(db: AsyncSession) -> dict[str, str]:
    user = await UserFactory.create(db, email="admin@bial.com")
    return _cookie(mint_session_jwt(user.id, user.token_version, _TTL))


async def _citizen(db: AsyncSession) -> dict[str, str]:
    user = await UserFactory.create(db, email="plain@rvaiglobal.com")
    return _cookie(mint_session_jwt(user.id, user.token_version, _TTL))


# --- openapi documentation -----------------------------------------------------


def test_admin_users_routes_document_error_codes_in_openapi() -> None:
    # The `/admin` users/limits/feedback sub-router: each route lists the inherited
    # dependency 401/403 (DetailBody) + the v1-router 500 default; the limits PATCH also
    # documents its explicit 400/404.
    paths = create_app().openapi()["paths"]
    patch = set(paths["/v1/admin/users/{user_id}/limits"]["patch"]["responses"])
    assert {"400", "401", "403", "404", "500"} <= patch
    assert {"401", "403", "500"} <= set(paths["/v1/admin/users"]["get"]["responses"])
    assert {"401", "403", "500"} <= set(paths["/v1/admin/feedback"]["get"]["responses"])


# --- gating --------------------------------------------------------------------


async def test_non_admin_forbidden(client, db_session) -> None:
    headers = await _citizen(db_session)
    assert (await client.get("/v1/admin/users", headers=headers)).status_code == 403
    assert (await client.get("/v1/admin/feedback", headers=headers)).status_code == 403


# --- list + resolution ---------------------------------------------------------


async def test_list_users_with_effective_limits(client, db_session) -> None:
    target = await UserFactory.create(db_session, email="dev@rvaiglobal.com", display_name="Dev")
    headers = await _admin(db_session)
    resp = await client.get("/v1/admin/users", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["defaults"]["dailyTokenLimit"] == settings.DAILY_TOKEN_LIMIT
    row = next(u for u in body["users"] if u["userId"] == str(target.id))
    assert row["role"] == "citizen"
    # No override → raw limits are null; effective falls back to the defaults.
    assert row["limits"]["dailyTokenLimit"] is None
    assert row["effectiveLimits"]["dailyTokenLimit"] == settings.DAILY_TOKEN_LIMIT
    assert row["effectiveLimits"]["contextHardLimit"] == 200000


# --- set / clear override, live agreement --------------------------------------


async def test_set_override_takes_effect_on_live_gate(client, db_session) -> None:
    target = await UserFactory.create(db_session, email="capped@rvaiglobal.com")
    headers = await _admin(db_session)
    resp = await client.patch(
        f"/v1/admin/users/{target.id}/limits", json={"dailyTokenLimit": 5000}, headers=headers
    )
    assert resp.status_code == 200
    assert resp.json()["effectiveLimits"]["dailyTokenLimit"] == 5000
    # The LIVE daily gate resolves the same value (admin edit and gate agree).
    assert await effective_daily_limit(db_session, target.id) == 5000


async def test_clear_override_falls_back_to_default(client, db_session) -> None:
    target = await UserFactory.create(db_session, email="reset@rvaiglobal.com")
    headers = await _admin(db_session)
    await client.patch(
        f"/v1/admin/users/{target.id}/limits", json={"dailyTokenLimit": 5000}, headers=headers
    )
    # null clears the override → back to the global default.
    cleared = await client.patch(
        f"/v1/admin/users/{target.id}/limits", json={"dailyTokenLimit": None}, headers=headers
    )
    assert cleared.json()["effectiveLimits"]["dailyTokenLimit"] == settings.DAILY_TOKEN_LIMIT
    override = (
        await db_session.execute(sa.select(UserLimit).where(UserLimit.user_id == target.id))
    ).scalar_one()
    assert override.daily_token_limit is None


async def test_set_limit_is_audited(client, db_session) -> None:
    target = await UserFactory.create(db_session, email="audited@rvaiglobal.com")
    headers = await _admin(db_session)
    await client.patch(
        f"/v1/admin/users/{target.id}/limits", json={"contextSoftLimit": 1000}, headers=headers
    )
    action = (
        await db_session.execute(
            sa.select(AuditLog.action).where(
                AuditLog.resource_type == "user", AuditLog.resource_id == str(target.id)
            )
        )
    ).scalar_one()
    assert action == "limits:set"


async def test_limit_validation(client, db_session) -> None:
    target = await UserFactory.create(db_session, email="bad@rvaiglobal.com")
    headers = await _admin(db_session)
    over_window = await client.patch(
        f"/v1/admin/users/{target.id}/limits",
        json={"contextHardLimit": 999999},
        headers=headers,
    )
    assert over_window.status_code == 400
    soft_ge_hard = await client.patch(
        f"/v1/admin/users/{target.id}/limits",
        json={"contextSoftLimit": 100, "contextHardLimit": 100},
        headers=headers,
    )
    assert soft_ge_hard.status_code == 400
    empty = await client.patch(f"/v1/admin/users/{target.id}/limits", json={}, headers=headers)
    assert empty.status_code == 400


async def test_a_chat_length_below_the_floor_is_refused_and_the_floor_is_named(
    client, db_session
) -> None:
    """An administrator cannot store a number that locks a citizen out of the product.

    Below the floor the context gate refuses EVERY conversation that person opens — the gate
    charges the system-prompt reserve before it counts a word — and the sentence they read
    tells them to start a new chat, which also fails. The refusal names the lowest usable
    number because an administrator who is stopped without one has no way to pick a good value.

    Mutation check: drop the `hard < CONTEXT_HARD_FLOOR` arm from the PATCH validator and this
    goes red on the status code."""
    target = await UserFactory.create(db_session, email="floored@rvaiglobal.com")
    headers = await _admin(db_session)

    refused = await client.patch(
        f"/v1/admin/users/{target.id}/limits",
        json={"contextHardLimit": CONTEXT_HARD_FLOOR - 1},
        headers=headers,
    )
    assert refused.status_code == 400, refused.text
    assert str(CONTEXT_HARD_FLOOR) in refused.text
    # NOTHING WAS STORED. A refusal that still wrote the row would leave the citizen locked out
    # and the administrator believing they had been stopped.
    assert (
        await db_session.scalar(sa.select(UserLimit).where(UserLimit.user_id == target.id)) is None
    )

    # …and the floor itself is a value an administrator may genuinely want.
    accepted = await client.patch(
        f"/v1/admin/users/{target.id}/limits",
        json={"contextHardLimit": CONTEXT_HARD_FLOOR},
        headers=headers,
    )
    assert accepted.status_code == 200, accepted.text


async def test_a_limit_already_stored_below_the_floor_still_opens_a_chat(db_session) -> None:
    """The other half of the floor, for the people the defect already reached.

    A row written before the validator existed still holds its number, and validation cannot
    reach back. Without the read-time clamp those citizens stay locked out until an
    administrator notices and edits them by hand — and nothing in the product tells anyone that
    is what happened.

    Mutation check: drop `CONTEXT_HARD_FLOOR` from the `max(...)` in `effective_context` and
    this goes red.
    """
    stored = UserLimit(user_id=uuid.uuid7(), context_hard_limit=1)
    _soft, hard = effective_context(stored)
    assert hard == CONTEXT_HARD_FLOOR
    # AND THE GATE THEN LETS AN EMPTY CONVERSATION THROUGH, which is the thing the citizen
    # actually cares about — the clamp is only worth anything if it clears the reserve the gate
    # charges before it has counted a word.
    assert occupied_window([], "hello") < hard
    # The advisory warn threshold stays strictly under the ceiling it warns about, so a clamped
    # user is not warned at the same instant they are refused.
    assert _soft < hard


async def test_unknown_user_is_404(client, db_session) -> None:
    from uuid import uuid4

    headers = await _admin(db_session)
    resp = await client.patch(
        f"/v1/admin/users/{uuid4()}/limits", json={"dailyTokenLimit": 5000}, headers=headers
    )
    assert resp.status_code == 404


# --- feedback read -------------------------------------------------------------


async def test_feedback_read_with_true_total(client, db_session) -> None:
    author = await UserFactory.create(db_session, email="voice@rvaiglobal.com")
    for i in range(3):
        db_session.add(Feedback(user_id=author.id, message=f"msg {i}", page="/chat"))
    await db_session.flush()
    headers = await _admin(db_session)
    resp = await client.get("/v1/admin/feedback", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 3
    mine = [f for f in body["feedback"] if f["email"] == "voice@rvaiglobal.com"]
    assert len(mine) == 3
    assert set(mine[0]) == {"userId", "email", "message", "page", "createdAt"}
