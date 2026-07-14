"""Dev tooling: mint a valid local session without a real Entra ID round-trip.

DEV/LOCAL ONLY — never run this against a production database. It mirrors the
real callback handler's own token issuance (mint_session_jwt / issue_csrf_token
/ issue_new_family) so the session it produces is indistinguishable from a
real login, but it skips the Microsoft OAuth exchange entirely: point it at a
local/dev DATABASE_URL, hand it any email, and it finds-or-creates that user
row directly. No product code is touched or bypassed — this only substitutes
for the identity provider round-trip during local testing.

Writes a Playwright storageState JSON (cookies only) to the given output
path — feed it to `E2E_STORAGE_STATE` (see portal/e2e/auth.setup.ts) to run
Playwright specs against a real session, or load it into a browser context
(e.g. `browser.newContext({ storageState: <path> })`) to drive the app
manually while authenticated, without a live tenant.

Usage:
    uv run python mint_dev_session.py --email dev-e2e@bial.example --out <path>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid

from sqlalchemy import select

from src.config import settings
from src.db.base import async_session_factory
from src.db.models.user import User
from src.services.auth.cookies import cookie_secure, csrf_cookie_name, refresh_cookie_name, session_cookie_name, REFRESH_COOKIE_PATH
from src.services.auth.csrf import issue_csrf_token
from src.services.auth.refresh import issue_new_family
from src.services.auth.session_jwt import mint_session_jwt


async def main(email: str, out_path: str) -> None:
    # The docstring's "DEV/LOCAL ONLY" warning is only real if something enforces
    # it — matching this codebase's own fail-first convention (config.py gates
    # object_store/cookie_secure the same way) rather than trusting a comment.
    if settings.is_production:
        raise SystemExit(
            "Refusing to mint a dev session: ENVIRONMENT=production. "
            "This script bypasses Entra ID and must never run against a production database."
        )

    async with async_session_factory() as db:
        result = await db.execute(select(User).where(User.email == email))
        user = result.scalar_one_or_none()
        if user is None:
            user = User(
                azure_oid=f"e2e-dev-{uuid.uuid4()}",
                email=email,
                upn=email,
                display_name="E2E Dev User",
            )
            db.add(user)
            await db.flush()

        session_jwt = mint_session_jwt(user.id, user.token_version, settings.auth.access_ttl_seconds)
        refresh_token = await issue_new_family(db, user.id)
        csrf_token = issue_csrf_token(user.id, user.token_version)
        await db.commit()

    secure = cookie_secure()
    now = int(time.time())
    cookies = [
        {
            "name": session_cookie_name(),
            "value": session_jwt,
            "domain": "localhost",
            "path": "/",
            "expires": now + settings.auth.access_ttl_seconds,
            "httpOnly": True,
            "secure": secure,
            "sameSite": "Lax",
        },
        {
            "name": refresh_cookie_name(),
            "value": refresh_token,
            "domain": "localhost",
            "path": REFRESH_COOKIE_PATH,
            "expires": now + settings.auth.refresh_ttl_seconds,
            "httpOnly": True,
            "secure": secure,
            "sameSite": "Lax",
        },
        {
            "name": csrf_cookie_name(),
            "value": csrf_token,
            "domain": "localhost",
            "path": "/",
            "expires": now + settings.auth.refresh_ttl_seconds,
            "httpOnly": False,
            "secure": secure,
            "sameSite": "Lax",
        },
    ]

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"cookies": cookies, "origins": []}, f, indent=2)

    print(f"Minted session for {email} (user_id={user.id}) -> {out_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    asyncio.run(main(args.email, args.out))
