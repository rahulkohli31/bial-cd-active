"""merge the connector-access head with the description-embedding head

Revision ID: 0041_merge_connector_heads
Revises: 0039_connector_access, 0040_description_embedding
Create Date: 2026-09-11

Reconciles two tips that both branched off ``0038_app_previous_status``: main's
``0039_drop_current_code`` -> ``0040_description_embedding`` (#191), and the connector line's
``0039_connector_access``. A merge rather than re-parenting ``0039_connector_access`` onto 0040,
because a database that already ran it keeps a revision id alembic still knows: ``alembic upgrade
head`` applies the other line and this merge, with no hand stamping. Pure merge revision, no schema
change; the single-head invariant is guarded by ``tests/test_alembic_single_head.py``.
"""

from __future__ import annotations

revision: str = "0041_merge_connector_heads"
down_revision: tuple[str, str] = ("0039_connector_access", "0040_description_embedding")
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """No-op: this revision only reconciles two divergent heads into one."""


def downgrade() -> None:
    """No-op: reverting re-exposes the two independent heads."""
