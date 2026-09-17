"""The `app_versions` table — one row per version the citizen chose to keep.

WHY THIS EXISTS

Save used to overwrite the app's one stored copy, so a citizen who saved a broken state could not
reach the good one. A version is what a Save leaves behind: a frozen bundle, plus the two facts
that cannot live in the bundle with it.

NEITHER FACT SURVIVES ANYWHERE ELSE. A description is the citizen's own words about what changed,
and a git commit has nowhere honest to carry it. The save time was previously read off the object
store's `last_modified`, which works only while there is exactly ONE saved copy — older entries
have either no object of their own or one that was overwritten. So both live here, and the row is
what the list is built from.

ONLY A SAVE AND A ROLLBACK MINT ONE. Chat turns and the platform's own autosave do not: the
`recovery_key` docstring draws that line and this table stays on the same side of it, or "try
something and walk away" stops being possible.

NOTHING HERE IS EVER DELETED BY THE LIST. Two rows are offered — the most recent pair — plus
whatever is live. A third stops being OFFERED; its row and its bundle both remain.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base
from src.db.mixins import OwnedByUserMixin, TimestampMixin, UUIDv7PrimaryKeyMixin

#: The longest description a version may carry. Matches the project name's ceiling rather than
#: inventing a second number — one line either way, and the list truncates it long before this.
MAX_DESCRIPTION = 120


class AppVersion(OwnedByUserMixin, TimestampMixin, UUIDv7PrimaryKeyMixin, Base):
    """One saved version of an app's source tree."""

    __tablename__ = "app_versions"

    app_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("app_registry.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # SEPARATE FROM `created_at`, AND NOT A DUPLICATE OF IT. A rollback mints a row now while
    # carrying forward the restored version's save date, because what the citizen recognises in the
    # list is when they saved that work — not when the platform re-recorded it. Conflating the two
    # either loses that or makes `created_at` lie about the row's age.
    saved_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)

    description: Mapped[str | None] = mapped_column(sa.String(MAX_DESCRIPTION), nullable=True)

    # What went live is matched against THIS, so the list can say which entry BIAL staff are
    # running without storing a second copy of the deployment record.
    head_sha: Mapped[str] = mapped_column(sa.String(40), nullable=False)

    # ★ SHARED BY A ROLLBACK'S NEW ROW, deliberately. A rollback restores byte-identical content,
    # the store has no server-side copy, and a version's bundle is never deleted while the app
    # exists — so two rows may name one key, and a sweeper must ask whether ANY row names it
    # rather than assuming the row that wrote it is the only claimant.
    blob_key: Mapped[str] = mapped_column(sa.Text, nullable=False)

    # The version this one was restored from, when it was. Makes the append-only lineage
    # inspectable: a rollback adds a row, it never rewrites the one it came from.
    restored_from_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("app_versions.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        # Every read is "the most recent few for this app", so the index carries the sort.
        sa.Index("ix_app_versions_app_saved_at", "app_id", sa.text("saved_at DESC")),
    )
