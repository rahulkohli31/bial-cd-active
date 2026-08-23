"""The outcomes this plan's success criteria name, counted where an operator can read them (U25).

R32. There is no metrics system in this deployment — `api/v1/admin/schemas.py` says so outright —
so the established shape is a pinned, greppable structlog event an external log rule keys on, plus
(where the outcome needs COUNTING rather than merely noticing) a relational record. This is the
second half.

WHY A NAME/VALUE ROW AND NOT A COLUMN PER COUNTER. The companion plan emits three adoption counters
of its own at the tool boundary and ships no migration; the moment a counter needs a schema change
to exist, the counter does not get added. A name as a column means a new counter is an INSERT.

NOT USER-SCOPED, like `worker_passes` and for the same reason: these are properties of the
DEPLOYMENT, not of a citizen. `app_id` is here for diagnosis — "which app was this about" — and it
is nullable, because some of these counters are about the platform rather than about any one app.

THE PER-BUILD TOKEN COUNTER IS A SINGLE VALUE ON PURPOSE. R32 asks for "a counter to watch", and a
number that takes a join and a judgement call to read is not one: it will not be watched. One row,
one `value`, keyed by the build it describes.
"""

from __future__ import annotations

import datetime as dt
import enum
import uuid

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base
from src.db.mixins import TimestampMixin, UUIDv7PrimaryKeyMixin


class HarnessCounter(enum.StrEnum):
    """The counters this plan's success criteria are answered from.

    A NATIVE PG ENUM would be exactly the wrong choice here and it is worth saying why, since
    ADR-0008 makes native enums the house default: every other enum in this schema is a closed set
    the platform controls, and adding a member is a deliberate schema change. This set is
    open BY DESIGN — the companion plan adds three of its own without shipping a migration — so
    the column is a plain string and this enum is the vocabulary THIS plan writes with.

    Names are stable strings. Renaming one loses the history it names."""

    #: A completion claim the health verdict refused (U6/U7). The headline number: how often the
    #: platform would have told a citizen their app was finished when it was not.
    CLAIM_BLOCKED = "claim_blocked"
    #: The preview cover was shown, and for how long (`value` is milliseconds). A holding state
    #: nobody complains about is one that resolved fast; this is how that stops being a guess.
    HOLDING_SHOWN_MS = "holding_shown_ms"
    #: A workspace was restored after a confirmed reversion (U2).
    RESTORE_PERFORMED = "restore_performed"
    #: A turn's work did not reach the recovery slot (U3). THE ONE THAT SETTLES 2026-08-18: it is
    #: the difference between "the platform failed to CHECK the workspace" and "the platform
    #: failed to make it DURABLE", which nobody could answer on the day.
    RECOVERY_WRITE_MISSED = "recovery_write_missed"
    #: Words in a completed build's agent-facing traffic, and tokens for the same build. R32's
    #: baseline is the pair captured in Prerequisite 3; the measurement after this plan lands is
    #: the companion plan's starting line.
    BUILD_WORDS = "build_words"
    BUILD_TOKENS = "build_tokens"


class HarnessCount(Base, UUIDv7PrimaryKeyMixin, TimestampMixin):
    """One counted outcome."""

    __tablename__ = "harness_counts"

    #: The counter's name. A plain string, not an enum — see `HarnessCounter`.
    name: Mapped[str] = mapped_column(sa.String(64), index=True)
    #: What is being counted. Milliseconds for a duration, tokens for a token count, and 1 for a
    #: plain occurrence — the unit is the counter's business and is documented on its member.
    value: Mapped[int] = mapped_column(sa.BigInteger, default=1)
    #: Which app this was about, when it was about one. Nullable: some counters are about the
    #: platform. NO foreign key, deliberately — a count is a historical fact and must survive the
    #: app being deleted, which is exactly when an operator most wants to read it back.
    app_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid, nullable=True, index=True)
    #: Which build, for the per-build counters. Same reasoning as `app_id`, and it is what makes
    #: the token counter readable as a single value for one build.
    build_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid, nullable=True, index=True)
    #: When the counted thing happened, as distinct from when the row was written.
    occurred_at: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), index=True)
    #: U6's RAW SERVED-HTML HEAD, carried on the row that recorded the verdict it produced.
    #:
    #: FOLDED INTO THIS TABLE rather than given one of its own, because it is only ever read
    #: beside the verdict it explains: an operator asking "why did this claim get blocked" wants
    #: the page the platform actually loaded, and a second table would make that a join for the
    #: sake of tidiness. Already scrubbed and capped at the boundary (`sandbox/client.py`) — the
    #: raw bytes never reach here, because a served page can carry a credential in a query string.
    served_head: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
