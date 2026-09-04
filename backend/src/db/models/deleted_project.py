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
private. The dialog names them too, but it does not ASK: `deleted_by_name` is stamped from
the session next to `deleted_by`, so the readable label and the durable key are the same
person by construction rather than by trust — see those columns.
"""

from __future__ import annotations

import datetime
import uuid

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base
from src.db.mixins import UUIDv7PrimaryKeyMixin

# The remark bounds (#158 §13.2), in WORDS. `src/core/words.py` owns the splitting rule and
# `portal/src/utils/words.ts` mirrors it, because a reason accepted by the counter the user
# is watching must not be refused by the API.
MIN_DELETE_REMARK_WORDS = 5
MAX_DELETE_REMARK_WORDS = 50
# A character ceiling as a paste backstop only. 50 words of ordinary English is far under
# this; a user should never meet it, and the word rule is the one they are told about.
MAX_DELETE_REMARK_CHARS = 2000
# Wide enough for either source the server stamps `deleted_by_name` from, so the value never
# needs truncating on the way in: `users.display_name` is 256 and `users.email` is 320. A
# truncation here would quietly corrupt the one field an administrator reads to identify a
# person, so the column is sized to make it impossible rather than handled.
DELETED_BY_NAME_WIDTH = 320


class DeletedProject(UUIDv7PrimaryKeyMixin, Base):
    """One deletion, recorded. Never joined to `projects` — that row is gone."""

    __tablename__ = "deleted_projects"

    # `id` comes from UUIDv7PrimaryKeyMixin — time-sortable, with a `uuidv7()` server default,
    # like every other table (ADR-0006). It was briefly a hand-rolled uuid4, which made this
    # the one table in the schema whose primary keys do not order by creation time; on an
    # audit table read newest-first, that is the ordering you most want.
    #
    # NOT a foreign key, deliberately: the project row does not exist any more, so an FK
    # would be unsatisfiable. It is kept so an administrator can correlate this with audit
    # entries and deployment history that still name the id.
    #
    # UNIQUE, and that is a correctness guard rather than an optimisation. `owned_project_or_404`
    # takes no row lock and the cascade deletes through Core `sa.delete()`, so no ORM staleness
    # check fires: a double-click or a proxy retry ran the whole delete twice and wrote TWO
    # tombstones for one physical deletion, plus duplicate audit rows, both returning 200. On the
    # table whose entire job is to be an accurate record of an irreversible action, an
    # administrator who cannot tell one deletion from two is the failure. The loser of the race
    # now fails closed on this constraint instead.
    project_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False, unique=True, index=True
    )
    # COPIED, not joined. The name is what makes this row legible to a human a month later.
    project_name: Mapped[str] = mapped_column(sa.String(120), nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, index=True)
    owner_email: Mapped[str] = mapped_column(sa.String(320), nullable=False)
    # WHO DELETED IT, authoritatively. Taken from the authenticated session, never from the
    # request body, and this is the column to trust: see `deleted_by_name` below for the one
    # that cannot be. Usually the same person as the owner — a citizen deleting their own
    # project — but stored separately because it does not have to be.
    deleted_by: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    # `deleted_by` IN WORDS. Stamped by the server from the authenticated session —
    # `display_name`, or the email when Entra gave us no display name — and NEVER read from
    # the request body, which is the whole point: a name the client supplies can disagree
    # with the account that acted, and this field exists to be read by an administrator
    # deciding who deleted something. It cannot disagree, because nothing outside this
    # process can set it.
    #
    # It is stored rather than joined for the same reason as `owner_email`: `users` rows
    # outlive projects, but a tombstone that needs a second lookup to name a person is one
    # an administrator will read wrong at a glance. `deleted_by` stays the durable key —
    # this is the label on it, correct as of the moment of deletion.
    deleted_by_name: Mapped[str] = mapped_column(sa.String(DELETED_BY_NAME_WIDTH), nullable=False)
    deleted_at: Mapped[datetime.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    remark: Mapped[str] = mapped_column(sa.Text, nullable=False)
    # WHAT WENT WITH IT. Unreconstructable after the fact, which is the whole reason to
    # capture it at the moment of deletion rather than derive it later.
    chats_deleted: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    had_app: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    had_database: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
