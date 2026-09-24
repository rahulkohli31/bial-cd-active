"""WHY THIS EXISTS: NO DATABASE ENGINE HERE MAY RENDER BOUND VALUES INTO AN ERROR.

SQLAlchemy renders a failing statement's bind parameters into `StatementError.__str__` as a
`[parameters: ...]` appendix, and `unhandled_exception_handler` logs `exc_info` for every
uncaught exception — so before `hide_parameters=True`, one 500 on any write put whatever the
citizen typed into an operator log. A measured incident on `DELETE /v1/projects/{id}` put the
actor's email, display name and free-text deletion reason all in one line.

THE FIX IS PER ENGINE, SO THE TESTS ARE PER ENGINE: three `create_async_engine` call sites, each
passing the flag at its own construction, so one shared assertion would pass on two of three even
if the third lost it. Each test builds ONE engine through the source's own construction path:
- `src/db/base.py`, the app pool — `conftest` REBINDS `src.db.base.engine` to its own `NullPool`
  engine, so reading the module global would test the fixture, not the source;
  `engine_as_written_in_db_base()` loads a fresh, unregistered copy to see what the deployed
  process actually gets.
- `src/services/appdb/engine.py`'s AUTOCOMMIT maintenance engine, via `_new_maintenance_engine`:
  the module's sole `create_async_engine` call and the shared constructor behind both public entry
  points, so covering it covers both. Pointed at the TEST database rather than the maintenance DSN,
  since the flag is set in the constructor and borrowing the app substrate keeps this test
  independent of whether an `APP_DB__*` cluster is reachable.
- `src/services/build_sessions/destroy.py`'s advisory-lock engine, shared by every scheduled
  destructive pass, via `_the_lock_engine()` with its module cache reset so the call really
  constructs one.

Each test asserts the ABSENCE of the marker value AND the PRESENCE of the "parameters hidden" line
SQLAlchemy substitutes; absence alone would pass on an exception that never rendered at all.
"""

from __future__ import annotations

import importlib.util
import pathlib
from collections.abc import AsyncIterator, Iterator

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import StatementError
from sqlalchemy.ext.asyncio import AsyncEngine

from src.services.appdb.engine import _new_maintenance_engine
from tests.conftest import TEST_DATABASE_URL

# A value that reads like the PII this test guards against, and is unique enough that finding
# it anywhere in a rendered exception is unambiguous.
MARKER = "anant.gupta@nobody.invalid"

_DB_BASE_SOURCE = pathlib.Path(__file__).resolve().parents[2] / "src" / "db" / "base.py"


def engine_as_written_in_db_base() -> AsyncEngine:
    """The application engine AS `src/db/base.py` CONSTRUCTS IT, bypassing `conftest`'s rebind.

    Loaded from the file under a private name and deliberately NOT registered in `sys.modules`,
    so `src.db.base` — and the `Base` every model is already mapped against — is untouched.
    The module makes no connection at import, so this costs a parse and nothing else.
    """
    spec = importlib.util.spec_from_file_location("_db_base_as_written", _DB_BASE_SOURCE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine: AsyncEngine = module.engine
    return engine


async def _failing_statement(engine: AsyncEngine) -> StatementError:
    """Run one statement that fails at the database with `MARKER` bound to it, and return the
    rendered `StatementError`. An undefined table is used because every engine here can reach
    it regardless of isolation level, and it needs no schema of its own."""
    with pytest.raises(StatementError) as caught:
        async with engine.connect() as conn:
            await conn.execute(
                sa.text("SELECT * FROM no_such_table_for_the_parameters_test WHERE email = :m"),
                {"m": MARKER},
            )
    return caught.value


def _assert_hidden(rendered: str) -> None:
    # Liveness first: an exception that never rendered its statement would satisfy every
    # absence assertion below for the wrong reason.
    assert "[SQL: SELECT * FROM no_such_table_for_the_parameters_test" in rendered
    assert "[SQL parameters hidden due to hide_parameters=True]" in rendered
    assert "[parameters:" not in rendered
    assert MARKER not in rendered


@pytest.fixture
async def disposing() -> AsyncIterator[list[AsyncEngine]]:
    """Dispose every engine a test built, on the loop that built it (an asyncpg connection is
    bound to its creating loop, and pytest-asyncio gives each test a new one)."""
    engines: list[AsyncEngine] = []
    yield engines
    for engine in engines:
        await engine.dispose()


async def test_the_application_engine_hides_bound_parameters(disposing) -> None:
    engine = engine_as_written_in_db_base()
    disposing.append(engine)

    _assert_hidden(str(await _failing_statement(engine)))


async def test_the_appdb_maintenance_engine_hides_bound_parameters(disposing) -> None:
    # The maintenance engine is the one closest to credentials — its statements carry database
    # names, role names and a generated role PASSWORD as bound values.
    engine = _new_maintenance_engine(TEST_DATABASE_URL)
    disposing.append(engine)

    _assert_hidden(str(await _failing_statement(engine)))


@pytest.fixture
def unbuilt_lock_engine() -> Iterator[None]:
    """Force `_the_lock_engine()` to really construct an engine, and put the module cache back.

    The engine is a module global built on first use, so a pass that ran earlier in the session
    would otherwise hand this test a cached engine and the construction under test would never
    run.
    """
    from src.services.build_sessions import destroy

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(destroy, "_lock_engine", None)
        yield


async def test_the_scheduled_pass_lock_engine_hides_bound_parameters(
    unbuilt_lock_engine, disposing
) -> None:
    from src.services.build_sessions.destroy import _the_lock_engine

    engine = _the_lock_engine()
    disposing.append(engine)

    _assert_hidden(str(await _failing_statement(engine)))
