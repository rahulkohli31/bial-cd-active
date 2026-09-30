"""A test gets the per-project database substrate only when it is marked `app_db`, and then on
the one worker every marked test shares, because some of them scan every database on the cluster.
"""

from __future__ import annotations

import pytest

from src.config import settings
from src.services.appdb.engine import get_maintenance_engine


def test_an_unmarked_test_runs_with_the_substrate_switched_off() -> None:
    assert settings.app_db is None
    assert get_maintenance_engine() is None


@pytest.mark.app_db
def test_a_marked_test_gets_the_substrate_on_the_app_db_worker(
    request: pytest.FixtureRequest,
) -> None:
    assert settings.app_db is not None
    group = request.node.get_closest_marker("xdist_group")
    assert group is not None
    assert group.args == ("app_db",)
