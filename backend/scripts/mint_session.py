"""Mint a real, working browser session for an existing (or newly created) user,
without touching the Entra OIDC round-trip — for local E2E testing only.

Writes a Playwright storageState-shaped JSON (`{"cookies": [...]}`) to --out,
suitable for `E2E_STORAGE_STATE` (see `portal/e2e/auth.setup.ts`'s docstring,
which documents this exact script's contract). Uses the SAME functions the real
login callback uses (`mint_session_jwt`, `issue_csrf_token`, `issue_new_family`)
against the REAL, configured `DATABASE_URL` — no product code is touched, only
the Microsoft round-trip is skipped.

Usage:
    uv run python scripts/mint_session.py --email admin@bial.example --out /tmp/admin.json
    uv run python scripts/mint_session.py --email citizen@rvaiglobal.com \
        --out /tmp/citizen.json --ttl-seconds 10800

A long --ttl-seconds is deliberate here (unlike the ~15m production default):
a real, unstubbed E2E run (a live Foundry interview + a real ACA sandbox boot +
submit + deploy-reconcile) can run well past 15 minutes end to end, and this
script's whole job is to hand a test a session that outlives the run, not to
model production's short-lived security posture.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool  # noqa: E402

from src.config import settings  # noqa: E402
from src.db.models.user import User  # noqa: E402
from src.services.auth.csrf import issue_csrf_token  # noqa: E402
from src.services.auth.refresh import issue_new_family  # noqa: E402
from src.services.auth.session_jwt import mint_session_jwt  # noqa: E402


async def _get_or_create_user(db: AsyncSession, email: str) -> User:
    row = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if row is not None:
        return row
    row = User(azure_oid=f"e2e-{uuid.uuid4()}", email=email, display_name=email.split("@")[0])
    db.add(row)
    await db.flush()
    return row


def _cookie_domain() -> str:
    # Playwright storageState cookies need a bare host, no scheme/port — matches
    # whatever origin the SPA's baseURL resolves to (localhost in dev).
    return urlparse(settings.FRONTEND_URL).hostname or "localhost"


async def _main(email: str, out_path: Path, ttl_seconds: int) -> None:
    # Code-enforced dev-only gate, matching the repo's other committed dev script
    # (`merge_duplicate_user_rows.py`): this mints a REAL, working session — a full
    # superadmin one if `--email` matches `SUPERADMIN_EMAILS` — for any email against
    # whatever `DATABASE_URL` is configured, with no Entra round-trip at all. A
    # misconfigured `DATABASE_URL` (or a `.env` left over from a prod debugging
    # session) must not be able to silently mint a superadmin session against it.
    if settings.is_production:
        raise SystemExit("FATAL: settings.is_production is True — this script refuses to run.")

    engine = create_async_engine(settings.DATABASE_URL.get_secret_value(), poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as db:
        user = await _get_or_create_user(db, email)
        session_jwt = mint_session_jwt(user.id, user.token_version, ttl_seconds)
        csrf_token = issue_csrf_token(user.id, user.token_version)
        refresh_token = await issue_new_family(db, user.id)
        await db.commit()
    await engine.dispose()

    domain = _cookie_domain()
    expires = time.time() + ttl_seconds
    refresh_expires = time.time() + settings.auth.refresh_ttl_seconds
    cookies = [
        {
            "name": "session",  # dev = bare name (cookie_secure() is False off-production)
            "value": session_jwt,
            "domain": domain,
            "path": "/",
            "httpOnly": True,
            "secure": False,
            "sameSite": "Lax",
            "expires": expires,
        },
        {
            "name": "csrf",
            "value": csrf_token,
            "domain": domain,
            "path": "/",
            "httpOnly": False,
            "secure": False,
            "sameSite": "Lax",
            "expires": refresh_expires,
        },
        {
            "name": "refresh",
            "value": refresh_token,
            "domain": domain,
            "path": "/api/v1/auth/refresh",
            "httpOnly": True,
            "secure": False,
            "sameSite": "Lax",
            "expires": refresh_expires,
        },
    ]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"cookies": cookies}, indent=2))
    print(f"minted session for {email!r} (user_id={user.id}) -> {out_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--ttl-seconds", type=int, default=3 * 3600)
    args = parser.parse_args()
    asyncio.run(_main(args.email, args.out, args.ttl_seconds))
