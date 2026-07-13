"""Async SQLAlchemy engine, session factory, and declarative Base (ADR-0013).

One process-wide async engine (asyncpg driver) + `async_sessionmaker`. Sessions
are opened per-request via `get_db` (`db/session.py`) — never a module-global
session. `pool_pre_ping` validates a pooled connection before handing it out so a
stale connection surfaces as a clean reconnect, not a mid-query failure.
"""

from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from src.config import settings

# Entra token audience for Azure Database for PostgreSQL (verified against MS Learn —
# the SDK "/.default" form of `az account get-access-token --resource
# https://ossrdbms-aad.database.windows.net`). Used only in DB_AUTH_MODE=entra.
_OSSRDBMS_SCOPE = "https://ossrdbms-aad.database.windows.net/.default"


def attach_entra_token(async_engine: AsyncEngine) -> None:
    """Wire Microsoft Entra managed-identity auth onto an async engine (ADR-0027).

    Azure Database for PostgreSQL Flexible Server with Entra auth has no static
    password: the app presents a short-lived Entra access token as the password.
    The token rotates, so it must be set on every NEW physical connection — an
    already-open connection stays valid past token expiry, because Postgres validates
    the token only at connect. A `do_connect` listener is that seam; it also pins a
    verify-full TLS context (system CAs + hostname check), since Entra never rides
    plaintext. One credential + one SSL context are built here and reused across
    connects (re-instantiating per call defeats the token cache). Callable on any
    engine — the app engine and Alembic's migration engine share it.
    """
    import ssl

    from azure.identity import DefaultAzureCredential

    credential = DefaultAzureCredential(managed_identity_client_id=settings.DB_ENTRA_CLIENT_ID)
    ssl_context = ssl.create_default_context()

    @event.listens_for(async_engine.sync_engine, "do_connect")
    def _provide_entra_token(
        dialect: object, conn_rec: object, cargs: object, cparams: dict[str, Any]
    ) -> None:
        # asyncpg gets a fresh token as its password + a verify-full TLS context.
        # STATIC: never log the token or cparams (security.md).
        cparams["password"] = credential.get_token(_OSSRDBMS_SCOPE).token
        cparams["ssl"] = ssl_context


engine = create_async_engine(
    settings.DATABASE_URL.get_secret_value(),
    pool_size=20,
    pool_pre_ping=True,
)
if settings.DB_AUTH_MODE == "entra":
    attach_entra_token(engine)

async_session_factory = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    """Declarative base for every ORM model. New models compose the mixins in
    `db/mixins.py` rather than re-declaring id/timestamp/ownership columns."""
