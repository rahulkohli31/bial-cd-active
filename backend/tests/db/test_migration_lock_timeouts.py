"""0047 and 0048 take ACCESS EXCLUSIVE locks on tables nearly every request reads: 0048 alters
`app_registry`, and 0047's drop locks `users` through its foreign keys. Each half must bound its
lock wait and hand the setting back, or its DDL queues behind an open reader and every request
for that table queues behind the DDL.

Source-level, for the reason `test_migration_0034_marketplace_indexes.py` gives: `SET LOCAL` is
scoped to the transaction, so a test that runs the migration cannot see a missing reset. The
round-trip tests for these two migrations sit outside the default lane; this one does not.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_VERSIONS = Path(__file__).resolve().parent.parent.parent / "alembic" / "versions"


@pytest.mark.parametrize(
    "migration",
    ["2026_09_26_0047_drop_connector_access.py", "2026_09_26_0048_drop_manual_go_live.py"],
)
def test_both_halves_bound_their_lock_wait_and_reset_it(migration: str) -> None:
    source = (_VERSIONS / migration).read_text(encoding="utf-8")
    upgrade_src = source[source.index("def upgrade()") : source.index("def downgrade()")]
    downgrade_src = source[source.index("def downgrade()") :]

    for name, body in (("upgrade", upgrade_src), ("downgrade", downgrade_src)):
        assert "SET LOCAL lock_timeout = '5s'" in body, f"{name}() no longer bounds its lock wait"
        assert "SET LOCAL lock_timeout = DEFAULT" in body, f"{name}() no longer resets it"
        assert body.index("lock_timeout = '5s'") < body.index("lock_timeout = DEFAULT")
