"""Regression coverage for tests/tooling/mint_dev_session.py's fresh-user branch.

A PR review flagged that `token_version` (server_default-only, no client-side
default — see `src/db/models/user.py`) could be unloaded right after
`db.add(user)` + `flush()`, and that reading it in an async context without a
`db.refresh()` first can raise `MissingGreenlet`. That specific crash does NOT
reproduce against this stack (Postgres's implicit RETURNING backfills
server-defaulted columns during flush, and `async_session_factory` is built
with `expire_on_commit=False` — see `src/db/base.py` — so nothing here ever
gets expired-and-needs-a-lazy-reload). The `db.refresh(user)` call was still
added, matching the same flush/refresh convention already used by
`UserFactory.create()` (tests/factories.py) and several api/v1/ routers, as a
defensive guard against a future backend/config change silently reintroducing
the crash. These tests guard the invariant the fix relies on, not a crash.
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import delete, inspect, select

from src.db.base import async_session_factory
from src.db.models.user import User
from tests.tooling import mint_dev_session


@pytest.mark.asyncio
async def test_fresh_user_mint_succeeds_and_writes_a_valid_session(tmp_path) -> None:
    # A genuinely novel email — never created before — exercises exactly the
    # `if user is None:` branch the review comment was concerned about.
    email = f"mint-dev-session-fresh-{uuid.uuid4()}@bial.example"
    out_path = tmp_path / "session.json"

    try:
        await mint_dev_session.main(email, str(out_path))

        written = json.loads(out_path.read_text(encoding="utf-8"))
        cookies = written["cookies"]
        assert len(cookies) == 3
        assert all(c["value"] for c in cookies)

        # main() uses its own session/commit (not the rollback-scoped db_session
        # fixture), so verify server-defaulted state landed correctly in the DB.
        async with async_session_factory() as db:
            user = (await db.execute(select(User).where(User.email == email))).scalar_one()
            assert user.token_version == 0
            assert user.suspended_at is None
    finally:
        async with async_session_factory() as db:
            await db.execute(delete(User).where(User.email == email))
            await db.commit()


@pytest.mark.asyncio
async def test_get_or_create_user_loads_server_defaulted_columns_for_a_new_user(
    db_session,
) -> None:
    # Calls the actual helper mint_dev_session.py's fresh-user branch uses (not a
    # standalone re-implementation) — fails if `db.refresh(user)` is ever removed
    # from `_get_or_create_user`, even though removing it doesn't currently crash
    # on this stack (see module docstring above): without the refresh, `unloaded`
    # below would still contain "suspended_at".
    email = f"unloaded-guard-{uuid.uuid4()}@bial.example"

    user = await mint_dev_session._get_or_create_user(db_session, email)

    state = inspect(user)
    assert "token_version" not in state.unloaded
    assert "suspended_at" not in state.unloaded
    assert user.token_version == 0
    assert user.suspended_at is None
