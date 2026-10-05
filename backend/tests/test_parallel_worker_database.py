"""Under pytest-xdist, every worker talks to its own copy of the test database, and the `app_db`
tests share one worker.

Losing either would bring back interference between workers, and it would show up only as
intermittent failures elsewhere in the suite.
"""

from __future__ import annotations

import os

import pytest
import sqlalchemy as sa

import src.db.base as db_base
from src.services.appdb.reconcile import _database_denylist

_WORKER = os.environ.get("PYTEST_XDIST_WORKER")

pytestmark = pytest.mark.skipif(_WORKER is None, reason="runs only under pytest-xdist")


async def test_a_parallel_worker_talks_to_its_own_copy_of_the_test_database() -> None:
    async with db_base.async_session_factory() as db:
        name: str = (await db.execute(sa.text("SELECT current_database()"))).scalar_one()

    assert name.endswith(f"_{_WORKER}")
    assert name in _database_denylist()


@pytest.mark.app_db
def test_xdist_itself_puts_an_app_db_test_in_its_group(request: pytest.FixtureRequest) -> None:
    # xdist appends the group to a test's id only when its own collection hook saw the marker.
    assert request.node.nodeid.endswith("@app_db")
