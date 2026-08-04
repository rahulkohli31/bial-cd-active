"""Mint a REAL local session — access JWT + refresh family — and print it paste-ready.

Not a mock. `mint_session_jwt`, `issue_csrf_token` and `issue_new_family` are the SAME
functions the Entra callback calls; the backend validates all three through its ordinary
path. The only step skipped is the Entra handshake itself, which this tenant's app
registration cannot complete (AADSTS9002326 — the callback is not registered as SPA).

The refresh cookie is the point: without it the 15-minute access token simply died and the
SPA signed you out, because its silent-refresh path had nothing to present. With it, the
session renews itself exactly as it does in production, for the full 8-hour family cap.

httpOnly is OFF here so the values can be pasted from the console. The real login sets it.
"""

from __future__ import annotations

import asyncio
import sys
import uuid

from sqlalchemy import select

from src.config import settings
from src.db.models.user import User
from src.db.session import async_session_factory
from src.services.auth.csrf import issue_csrf_token
from src.services.auth.refresh import issue_new_family
from src.services.auth.session_jwt import mint_session_jwt

EMAIL = sys.argv[1] if len(sys.argv) > 1 else "sheik.javeed@rvaiglobal.com"


async def main() -> None:
    async with async_session_factory() as db:
        user = await db.scalar(select(User).where(User.email == EMAIL))
        if user is None:
            user = User(
                id=uuid.uuid7(),
                azure_oid=f"local-{uuid.uuid4().hex[:16]}",
                email=EMAIL,
                upn=EMAIL,
                display_name=EMAIL.split("@")[0],
            )
            db.add(user)
            await db.commit()
            await db.refresh(user)
        jwt = mint_session_jwt(user.id, user.token_version, settings.auth.access_ttl_seconds)
        csrf = issue_csrf_token(user.id, user.token_version)
        refresh = await issue_new_family(db, user.id)
        await db.commit()
        admin = user.email.lower() in settings.superadmin_emails

    print("\n" + "=" * 74)
    print(f"  signed in as : {user.email}")
    print(f"  super admin  : {admin}")
    print(
        f"  renews itself for up to {settings.auth.absolute_session_seconds // 3600}h "
        f"(access token rotates every {settings.auth.access_ttl_seconds // 60} min)"
    )
    print("=" * 74)
    print("\n1. open  http://localhost:5173")
    print("2. DevTools -> Console -> paste ALL THREE lines -> Enter")
    print("3. reload\n")
    print(f'document.cookie = "session={jwt}; path=/";')
    print(f'document.cookie = "csrf={csrf}; path=/";')
    print(f'document.cookie = "refresh={refresh}; path=/api/v1/auth/refresh";')
    print()


asyncio.run(main())
