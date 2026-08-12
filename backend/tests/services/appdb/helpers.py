"""Out-of-band connection helpers for the per-project-database suite.

Every engine built here is `NullPool` + disposed in a `finally` — pytest-asyncio runs a
per-function event loop and an asyncpg connection is loop-bound, so a pooled engine that
outlives its test blows up in the next one.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from src.config import settings
from src.db.models.project_database import ProjectDatabase
from src.services.appdb.provision import control_plane_dsn


async def scalar_on(dsn: str, sql: str, **params: Any) -> Any:
    """Open `dsn`, run one statement, return the first column of the first row."""
    engine = create_async_engine(dsn, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            return await conn.scalar(text(sql), params)
    finally:
        await engine.dispose()


async def execute_on(dsn: str, sql: str, **params: Any) -> None:
    """Open `dsn` and run one statement for its effect (AUTOCOMMIT, so DDL sticks)."""
    engine = create_async_engine(dsn, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(text(sql), params)
    finally:
        await engine.dispose()


def app_dsn(record: ProjectDatabase) -> str:
    """The app role's own DSN, in the driver form the control plane can connect with."""
    return control_plane_dsn(record)


def control_plane_identity_dsn(db_name: str) -> str:
    """The CONTROL PLANE's own credentials pointed at `db_name`.

    Used to prove the cross-app wall from the outside: this identity is neither the app
    role nor a member of it, so `REVOKE CONNECT ... FROM PUBLIC` must refuse it.
    """
    return (
        make_url(settings.DATABASE_URL.get_secret_value())
        .set(database=db_name)
        .render_as_string(hide_password=False)
    )


def app_role_pointed_at(record: ProjectDatabase, db_name: str) -> str:
    """The APP role's credentials pointed at some OTHER database.

    The inverse of `control_plane_identity_dsn`: that one proves an outsider cannot get
    into an app's box, this one proves the app cannot get out of it — including into the
    control plane's own database.
    """
    return (
        make_url(control_plane_dsn(record))
        .set(database=db_name)
        .render_as_string(hide_password=False)
    )


def control_plane_database_name() -> str:
    """The control plane's own database (`citizen_one_test` under `.env.test`)."""
    return make_url(settings.DATABASE_URL.get_secret_value()).database or ""


def unpersisted_record(
    *, project_id: uuid.UUID, db_name: str, role_name: str, password_encrypted: str
) -> ProjectDatabase:
    """A ProjectDatabase instance that is NEVER added to a session — enough for the pure
    DSN-assembly functions, which read only the four columns."""
    return ProjectDatabase(
        project_id=project_id,
        db_name=db_name,
        role_name=role_name,
        password_encrypted=password_encrypted,
    )
