"""The `app_files` table — per-app file metadata (R25; ADR-0004, ADR-0008).

App-scoped (FK `app_registry.id`): the twin of `data_records`, one metadata row per
stored blob. The bytes live in the object store at `blob_key = apps/{app_id}/{id}`;
this row is the composite-scoped handle every read/delete goes through
(`WHERE id = :id AND app_id = :app_id`). The file `id` is preserved at backfill
(AE3); new files get a UUIDv7 PK.

`status` is a native PG enum (ADR-0008) driving the two-store choreography: a row is
inserted `pending`, the blob is written, then the row flips to `ready`; app-facing
reads only see `ready` rows. A crash between insert and ready leaves an invisible
`pending` row reclaimed by the admin recompute sweep. Quota counters live on the app
row (`file_count` / `file_bytes`), reserved atomically before the blob write.
"""

from __future__ import annotations

import uuid
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base
from src.db.mixins import TimestampMixin, UUIDv7PrimaryKeyMixin


class AppFileStatus(StrEnum):
    """The two-store lifecycle (Express `pending`/`ready`, verbatim)."""

    PENDING = "pending"
    READY = "ready"


app_file_status_enum = sa.Enum(
    AppFileStatus,
    name="app_file_status",
    values_callable=lambda enum: [member.value for member in enum],
    create_type=False,
)

# Ported filename charset (Express): blocks traversal + Content-Disposition/CRLF
# injection. Applied at the write boundary and re-checked before disposition.
MAX_FILENAME_LEN = 200
MAX_COLLECTION_LEN = 64


class AppFile(UUIDv7PrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "app_files"

    __table_args__ = (
        sa.Index("ix_app_files_app_collection", "app_id", "collection"),
        # The ready-only, newest-first list path.
        sa.Index("ix_app_files_app_status", "app_id", "status"),
    )

    app_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid,
        sa.ForeignKey("app_registry.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    collection: Mapped[str] = mapped_column(
        sa.String(MAX_COLLECTION_LEN), server_default="default", nullable=False
    )
    filename: Mapped[str] = mapped_column(sa.String(MAX_FILENAME_LEN), nullable=False)
    content_type: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    # Decoded byte length — the server-computed size, NEVER a client-claimed one.
    size: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    # The object-store key `apps/{app_id}/{id}` — server-minted, app-prefixed.
    blob_key: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    status: Mapped[AppFileStatus] = mapped_column(
        app_file_status_enum, server_default=AppFileStatus.PENDING.value, nullable=False
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid, nullable=True)
    created_in_draft: Mapped[bool] = mapped_column(
        sa.Boolean, server_default=sa.text("false"), nullable=False
    )
