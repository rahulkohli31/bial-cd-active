"""Guard the single-Alembic-head invariant.

The migration chain is authored across two parallel plan sessions (portal-plane
and app-data-plane), which is exactly how divergent heads sneak in: each branch
extends the chain off a shared ancestor, and ``alembic upgrade head`` then fails
with "Multiple head revisions are present" (see
``alembic/versions/2026_07_06_0014_merge_plan_ab_heads.py``). This test fails the
moment a new head appears without a reconciling merge revision, so the drift is
caught in CI rather than at deploy time.
"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

_BACKEND_ROOT = Path(__file__).resolve().parent.parent


def test_migrations_have_exactly_one_head() -> None:
    config = Config(str(_BACKEND_ROOT / "alembic.ini"))
    script = ScriptDirectory.from_config(config)
    heads = script.get_heads()
    assert len(heads) == 1, (
        f"expected exactly one Alembic head, found {len(heads)}: {heads}. "
        "Reconcile divergent branches with `alembic merge heads`."
    )
