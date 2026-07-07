"""merge Plan A + Plan B migration heads

Revision ID: 0014_merge_plan_ab_heads
Revises: 0007_attachments, 0013_clear_data_token
Create Date: 2026-07-06

Reconciles the two Alembic tips produced by the parallel migration sessions
(the Cross-Plan Execution Guide's documented "merge heads" hazard). Plan A's
portal-plane chain ends at ``0007_attachments`` (0003→0004→0005→0006→0007) and
Plan B's app-data chain ends at ``0013_clear_data_token`` (0003→0010→0011→0012
→0013); both branch off ``0003_audit_logs``. Without this merge, ``alembic
upgrade head`` fails with "Multiple head revisions are present".

This is a pure merge revision: no schema change (empty upgrade/downgrade). The
single-head invariant is guarded by ``tests/test_alembic_single_head.py``.
Hand-finalized (ADR-0013).
"""

from __future__ import annotations

revision: str = "0014_merge_plan_ab_heads"
down_revision: tuple[str, str] = ("0007_attachments", "0013_clear_data_token")
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """No-op: this revision only reconciles two divergent heads into one."""


def downgrade() -> None:
    """No-op: reverting re-exposes the two independent heads."""
