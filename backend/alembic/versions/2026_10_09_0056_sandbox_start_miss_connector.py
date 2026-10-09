"""sandbox_start_miss gains `connector`: a connector project's start never claims a ready container

Revision ID: 0056_start_miss_connector
Revises: 0055_sandbox_pool
Create Date: 2026-10-09

The type is rebuilt rather than extended, as 0044 rebuilds `chat_kind`: `ALTER TYPE … ADD VALUE`
has no inverse. The swap rewrites `sandbox_starts` under ACCESS EXCLUSIVE; only the start recorder
writes that table, so the lock wait is bounded and the revision fails fast rather than queueing.

The downgrade clears `connector` to NULL, since the older type has no word for it.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0056_start_miss_connector"
down_revision: str | None = "0055_sandbox_pool"
branch_labels: str | None = None
depends_on: str | None = None

# Labels spelled out, never imported. `create_type=False` so THIS migration owns each lifecycle.
_PREVIOUS = ("no_ready", "unhealthy", "claim_failed", "size_zero")
miss_next = postgresql.ENUM(*_PREVIOUS, "connector", name="sandbox_start_miss", create_type=False)
miss_prev = postgresql.ENUM(*_PREVIOUS, name="sandbox_start_miss", create_type=False)


def _rebuild(target: postgresql.ENUM) -> None:
    op.execute(sa.text("ALTER TYPE sandbox_start_miss RENAME TO sandbox_start_miss_old"))
    target.create(op.get_bind(), checkfirst=False)
    op.execute(
        sa.text(
            "ALTER TABLE sandbox_starts ALTER COLUMN miss_reason TYPE sandbox_start_miss "
            "USING miss_reason::text::sandbox_start_miss"
        )
    )
    op.execute(sa.text("DROP TYPE sandbox_start_miss_old"))


def upgrade() -> None:
    op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    _rebuild(miss_next)
    op.execute(sa.text("SET LOCAL lock_timeout = DEFAULT"))


def downgrade() -> None:
    op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    op.execute(
        sa.text("UPDATE sandbox_starts SET miss_reason = NULL WHERE miss_reason = 'connector'")
    )
    _rebuild(miss_prev)
    op.execute(sa.text("SET LOCAL lock_timeout = DEFAULT"))
