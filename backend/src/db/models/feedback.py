"""The `feedback` table — one row per user-submitted feedback message (R30).

Mirrors the Express `feedback` collection (`server/feedback-repo.js`): the author is the
authenticated user (never a body-supplied name), plus a short free-text `message` and an
advisory `page` path. No rating, no other fields — feedback is message + page only.

`OwnedByUserMixin` scopes the row to its author (the admin read in Plan B lists across
users, but a citizen only ever writes their own). `page` is a sanitized same-origin path
(or empty) — never trusted raw input (sanitization happens at the API boundary).
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base
from src.db.mixins import OwnedByUserMixin, TimestampMixin, UUIDv7PrimaryKeyMixin


class Feedback(UUIDv7PrimaryKeyMixin, TimestampMixin, OwnedByUserMixin, Base):
    __tablename__ = "feedback"

    # The trimmed message. TEXT (unbounded at the DB) — the API boundary enforces the
    # 4000-UTF-8-byte cap (parity with Express `MAX_FEEDBACK_CHARS`), so the column need
    # not re-encode that bound.
    message: Mapped[str] = mapped_column(sa.Text, nullable=False)
    # A sanitized same-origin path (`/chat`, `/admin/feedback`, …) or empty. Bounded to
    # 256 chars (Express `MAX_PAGE_CHARS`, applied as a code-point truncation upstream);
    # varchar(256) holds any value the sanitizer can produce.
    page: Mapped[str] = mapped_column(sa.String(256), nullable=False, server_default="")
