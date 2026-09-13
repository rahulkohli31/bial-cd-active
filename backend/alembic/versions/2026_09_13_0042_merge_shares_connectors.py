"""merge the project-shares head with the connector head

Revision ID: 0042_merge_shares_connectors
Revises: 0041_merge_connector_heads, 0041_project_shares
Create Date: 2026-09-13

Reconciles two tips that both descend from ``0040_description_embedding``: the connector line's
``0041_merge_connector_heads`` and ``0041_project_shares``. A merge rather than re-parenting
either, because a database that already ran one of them keeps a revision id alembic still knows:
``alembic upgrade head`` applies the other line and this merge, with no hand stamping. Pure merge
revision, no schema change; the single-head invariant is guarded by
``tests/test_alembic_single_head.py``.
"""

from __future__ import annotations

revision: str = "0042_merge_shares_connectors"
down_revision: tuple[str, str] = ("0041_merge_connector_heads", "0041_project_shares")
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """No-op: this revision only reconciles two divergent heads into one."""


def downgrade() -> None:
    """No-op: reverting re-exposes the two independent heads."""
