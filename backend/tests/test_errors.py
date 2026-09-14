"""Boundary exception handlers: no input echo on 422, no internal detail on 500, and no
bound parameters in the line the 500 handler logs."""

from __future__ import annotations

import json
import traceback

import httpx
import structlog.testing
from fastapi import Request
from fastapi.exceptions import RequestValidationError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from src.config import settings
from src.core.errors import unhandled_exception_handler, validation_exception_handler
from src.db.session import get_db
from src.main import create_app
from src.services.auth.session_jwt import mint_session_jwt
from tests.db.test_engine_hides_parameters import engine_as_written_in_db_base
from tests.factories import ProjectFactory, UserFactory

# The three values below were read out of a live operator log line, restated as test data.
ACTOR_EMAIL = "actor.under.test@nobody.invalid"
ACTOR_DISPLAY_NAME = "Actor Under Test"
REMARK_WORDS = "the verification run is finished"
# The reproducing input: a NUL byte inside an otherwise valid 5-50 word reason. Postgres
# refuses it in a text parameter, so the tombstone INSERT fails and the request 500s.
NUL_BYTE_REMARK = f"Deleting because\x00 {REMARK_WORDS}"


def _request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/", "headers": []})


def test_validation_handler_drops_submitted_input() -> None:
    # A validation error whose raw form carries the submitted value (a plaintext
    # password) must NOT reflect that value back to the client.
    exc = RequestValidationError(
        [
            {
                "type": "missing",
                "loc": ("body", "password"),
                "msg": "Field required",
                "input": "hunter2-secret",
            }
        ]
    )
    response = validation_exception_handler(_request(), exc)
    assert response.status_code == 422
    body = json.loads(bytes(response.body))
    assert body == {
        "detail": [{"type": "missing", "loc": ["body", "password"], "msg": "Field required"}]
    }
    assert b"hunter2-secret" not in bytes(response.body)


def test_unhandled_handler_returns_generic_500() -> None:
    # An unhandled exception must return a generic message — never the internal
    # detail, which is logged server-side instead.
    response = unhandled_exception_handler(_request(), RuntimeError("secret stack detail"))
    assert response.status_code == 500
    body = json.loads(bytes(response.body))
    assert body == {"detail": "Internal server error"}
    assert b"secret stack detail" not in bytes(response.body)


# --- a 500 on a real route must not log what the citizen typed -----------------


def _rendered(exc: BaseException) -> str:
    """Every rendering of `exc_info` a logger could produce, joined.

    The configured chain (`core/log_config.py`) logs an exception as its signature, but anything
    else handed the exception — a formatted traceback, which embeds `str(exc)` for every exception
    in the chain, or its `repr` — must not leak either, so both are searched at once.
    """
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)) + repr(exc)


async def _delete_with_a_nul_byte(engine: AsyncEngine):
    """Drive the REAL `DELETE /v1/projects/{id}` into the NUL-byte 500, on `engine`, and return
    the response plus everything the loggers were handed while it happened.

    Everything this writes lives inside one transaction that is rolled back, so the 500 leaves
    the database exactly as it found it.
    """
    async with engine.connect() as conn:
        outer = await conn.begin()
        session = AsyncSession(bind=conn, expire_on_commit=False)
        try:
            user = await UserFactory.create(
                session, email=ACTOR_EMAIL, display_name=ACTOR_DISPLAY_NAME
            )
            project = await ProjectFactory.create(session, user.id, name="NUL Byte Probe")
            token = mint_session_jwt(user.id, user.token_version, settings.auth.access_ttl_seconds)

            app = create_app()

            async def _override_get_db():
                yield session

            app.dependency_overrides[get_db] = _override_get_db
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                with structlog.testing.capture_logs() as captured:
                    response = await client.request(
                        "DELETE",
                        f"/v1/projects/{project.id}",
                        headers={"Cookie": f"session={token}"},
                        json={"remark": NUL_BYTE_REMARK},
                    )
            return response, captured, project.id
        finally:
            await session.close()
            await outer.rollback()


async def test_a_500_on_the_delete_route_logs_no_bound_parameters(fake_storage) -> None:
    """The reproduction, end to end on the real route and the real engine. A NUL byte in the
    deletion reason still 500s — rejecting it on the way in is a separate change's job, not this
    one's — but the line the operator gets must no longer carry the actor's email, display name,
    or reason typed, all three bound to the tombstone INSERT that fails.

    THE ENGINE IS THE POINT: the session runs on the application engine as `src/db/base.py`
    writes it, not `conftest`'s `test_engine` (a fixture's own `create_async_engine` call,
    answering for itself, not the source). `fake_storage` only satisfies the route's
    object-store dependency, resolved before the body runs; nothing is written to it here.
    """
    engine = engine_as_written_in_db_base()
    try:
        response, captured, project_id = await _delete_with_a_nul_byte(engine)
    finally:
        await engine.dispose()

    # The crash half is unchanged (the input fix belongs to that separate change), and the body
    # a citizen sees is still the generic one, with no internal detail in it.
    assert response.status_code == 500, response.text
    assert response.json() == {"detail": "Internal server error"}

    # The handler really ran, and really logged the exception: without this the absence
    # assertions below would pass just as happily on a request that never reached it.
    entries = [e for e in captured if e["event"] == "unhandled_exception"]
    assert len(entries) == 1, captured
    assert entries[0]["path"] == f"/v1/projects/{project_id}"
    rendered = _rendered(entries[0]["exc_info"])
    assert "[SQL parameters hidden due to hide_parameters=True]" in rendered
    assert "[parameters:" not in rendered

    # The three values read out of a live log line.
    assert ACTOR_EMAIL not in rendered
    assert ACTOR_DISPLAY_NAME not in rendered
    assert REMARK_WORDS not in rendered
