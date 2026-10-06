"""Async SQLAlchemy engine, session factory, and declarative Base.

One process-wide async engine (asyncpg driver) + `async_sessionmaker`. Sessions
are opened per-request via `get_db` (`db/session.py`) — never a module-global
session. `pool_pre_ping` validates a pooled connection before handing it out so a
stale connection surfaces as a clean reconnect, not a mid-query failure.
"""

from typing import Any, Final

from sqlalchemy import event
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from src.config import settings

#: What a statement raises when the database did not answer it, for a caller that carries on
#: without the database rather than failing.
DB_UNREACHABLE: Final = (SQLAlchemyError, OSError)

# Entra token audience for Azure Database for PostgreSQL (verified against MS Learn —
# the SDK "/.default" form of `az account get-access-token --resource
# https://ossrdbms-aad.database.windows.net`). Used only in DB_AUTH_MODE=entra.
_OSSRDBMS_SCOPE = "https://ossrdbms-aad.database.windows.net/.default"


def attach_entra_token(async_engine: AsyncEngine) -> None:
    """Wire Microsoft Entra managed-identity auth onto an async engine.

    Entra Postgres auth has no static password: the app presents a short-lived access token as the
    password, rotated on every NEW physical connection — an open one stays valid past token expiry,
    since Postgres validates only at connect. `do_connect` is that seam, and it pins verify-full
    TLS, since Entra never rides plaintext. Credential and SSL context are built once and reused;
    re-instantiating per call defeats the token cache."""
    import ssl

    from azure.identity import DefaultAzureCredential

    credential = DefaultAzureCredential(managed_identity_client_id=settings.DB_ENTRA_CLIENT_ID)
    ssl_context = ssl.create_default_context()

    @event.listens_for(async_engine.sync_engine, "do_connect")
    def _provide_entra_token(
        dialect: object, conn_rec: object, cargs: object, cparams: dict[str, Any]
    ) -> None:
        # asyncpg gets a fresh token as its password + a verify-full TLS context.
        # STATIC: never log the token or cparams.
        cparams["password"] = credential.get_token(_OSSRDBMS_SCOPE).token
        cparams["ssl"] = ssl_context


engine = create_async_engine(
    settings.DATABASE_URL.get_secret_value(),
    pool_size=20,
    pool_pre_ping=True,
    # NO BIND PARAMETERS IN LOGS, EVER. SQLAlchemy renders a failing statement's
    # bound values into `StatementError.__str__` as a `[parameters: ...]` appendix, and
    # `unhandled_exception_handler` logs `exc_info` for every uncaught exception — so a
    # single 500 on any write path puts whatever the citizen typed into an operator-readable
    # log line. This was measured exactly on `DELETE /v1/projects/{id}`: the actor's email,
    # their display name and their free-text deletion reason. This is the fix at source
    # rather than at one route, because the exposure was never route-local: the flag
    # replaces the appendix with a fixed "parameters hidden" marker for EVERY logger and
    # every statement. The SQL text itself still logs, which is what an operator actually
    # needs to locate the fault.
    hide_parameters=True,
)
if settings.DB_AUTH_MODE == "entra":
    attach_entra_token(engine)

async_session_factory = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    """Declarative base for every ORM model. New models compose the mixins in
    `db/mixins.py` rather than re-declaring id/timestamp/ownership columns."""
