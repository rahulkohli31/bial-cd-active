"""A TOMBSTONE for a deleted project — a record ABOUT the deletion, never the data.

#158 §13.2 requires the person deleting a project to say why, in 5–50 words, and §13.3 left
where that remark lives as a decision. The ask was `is_deleted` + `remark` columns on
`projects`. This is deliberately not that, and the reasoning matters more than the schema:

THE TWO HALVES OF A SOFT DELETE CONTRADICT THIS PRODUCT. The dialog tells the citizen that
nothing is recoverable, and it is telling the truth: `delete_project` calls
`salt_the_earth`, which severs and force-drops the project's own PostgreSQL database and
its role, sweeps its blobs, and deletes its container. `is_deleted` is the shape of a
reversible delete, which is the opposite claim. A row that survives while its database,
files, deployment and chats are gone is not a soft-deleted project — it is a TOMBSTONE, and
naming it as one stops someone later assuming a project can be restored from it.

A FLAG ON `projects` WOULD COST EVERY QUERY, FOREVER. Each read would need
`WHERE is_deleted = false` — the list, the search, the counts, the dashboard tiles, the
admin panel, every join. Missing one resurrects a deleted project, which is the single most
common soft-delete bug, and this repo already treats a dropped predicate as a defect rather
than a style nit (ADR-0004's ownership clause is enforced the same way). The hot path stays
untouched here because deleted projects are not in `projects` at all.

WHAT IT KEEPS, AND WHY EACH FIELD. The row must stay readable after everything it refers to
is gone, so it stores VALUES rather than foreign keys to rows that no longer exist:
`project_id` is not an FK for that reason, and the name and owner are copied rather than
joined. The counts record what went with it, which is the part an administrator reviewing a
deletion actually wants and could never reconstruct afterwards.

WHO WRITES IT AND WHO READS IT. The remark is written by whoever deletes — usually the
project's OWNER, not an administrator — and read by administrators. §13.2 requires the
dialog to say so, because a person writing a private-feeling note deserves to know it is not
private.
"""

from __future__ import annotations

import datetime
import uuid

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base

# The remark bounds (#158 §13.2), in WORDS. `src/core/words.py` owns the splitting rule and
# `portal/src/utils/words.ts` mirrors it, because a reason accepted by the counter the user
# is watching must not be refused by the API.
MIN_DELETE_REMARK_WORDS = 5
MAX_DELETE_REMARK_WORDS = 50
# A character ceiling as a paste backstop only. 50 words of ordinary English is far under
# this; a user should never meet it, and the word rule is the one they are told about.
MAX_DELETE_REMARK_CHARS = 2000


class DeletedProject(Base):
    """One deletion, recorded. Never joined to `projects` — that row is gone."""

    __tablename__ = "deleted_projects"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # NOT a foreign key, deliberately: the project row does not exist any more, so an FK
    # would be unsatisfiable. It is kept so an administrator can correlate this with audit
    # entries and deployment history that still name the id.
    project_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, index=True)
    # COPIED, not joined. The name is what makes this row legible to a human a month later.
    project_name: Mapped[str] = mapped_column(sa.String(120), nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, index=True)
    owner_email: Mapped[str] = mapped_column(sa.String(320), nullable=False)
    # Usually the same person as the owner — a citizen deleting their own project — but
    # stored separately because it does not have to be.
    deleted_by: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    deleted_at: Mapped[datetime.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    remark: Mapped[str] = mapped_column(sa.Text, nullable=False)
    # WHAT WENT WITH IT. Unreconstructable after the fact, which is the whole reason to
    # capture it at the moment of deletion rather than derive it later.
    chats_deleted: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    had_app: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    had_database: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
