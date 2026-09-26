"""The publish classification configuration: the classes the reviewer answers, and the one policy
row that decides what their answers mean.

A class's `key` is fixed at creation and never changes. The reviewer answers by key and every
stored decision refers to it, so rewording a title never orphans an answer. Title uniqueness is
case-insensitive, held by a unique index on `lower(title)`. A hard block carries no weight and a
scored class always carries one; the CHECK constraint holds both halves.

The policy table has exactly one row, seeded by the migration and held there by a unique index
on a constant. Nothing inserts another; the admin API only updates it.
"""

from __future__ import annotations

import uuid
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base
from src.db.mixins import TimestampMixin, UUIDv7PrimaryKeyMixin

MAX_CLASS_KEY = 64
MAX_CLASS_TITLE = 60
MAX_CLASS_DESCRIPTION = 1000
MAX_WEIGHT = 100
MAX_THRESHOLD = 100


class ClassificationKind(StrEnum):
    """A hard block routes the app to review on a Yes whatever the score. A scored class adds
    its weight to the score on a Yes."""

    HARD_BLOCK = "hard_block"
    SCORED = "scored"


# `create_type=False`: the migration owns CREATE and DROP TYPE, so a downgrade removes it.
classification_kind_enum = sa.Enum(
    ClassificationKind,
    name="classification_kind",
    values_callable=lambda enum: [member.value for member in enum],
    create_type=False,
)

# A CHECK passes on UNKNOWN, so the scored arm says NOT NULL rather than trusting the range test
# to refuse a missing weight.
_WEIGHT_FOR_KIND = (
    "(kind = 'hard_block' AND weight IS NULL) OR "
    f"(kind = 'scored' AND weight IS NOT NULL AND weight BETWEEN 0 AND {MAX_WEIGHT})"
)


class ClassificationClass(UUIDv7PrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "classification_classes"

    __table_args__ = (
        sa.UniqueConstraint("key", name="uq_classification_classes_key"),
        sa.Index("uq_classification_classes_title", sa.text("lower(title)"), unique=True),
        sa.CheckConstraint(_WEIGHT_FOR_KIND, name="ck_classification_classes_weight_for_kind"),
    )

    key: Mapped[str] = mapped_column(sa.String(MAX_CLASS_KEY), nullable=False)
    title: Mapped[str] = mapped_column(sa.String(MAX_CLASS_TITLE), nullable=False)
    # The reviewer's instruction for this class. Administrators read and edit it; owners never
    # see it.
    description: Mapped[str] = mapped_column(sa.String(MAX_CLASS_DESCRIPTION), nullable=False)
    kind: Mapped[ClassificationKind] = mapped_column(classification_kind_enum, nullable=False)
    weight: Mapped[int | None] = mapped_column(sa.SmallInteger, nullable=True)
    active: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.text("true")
    )
    # NULL on the seeded rows until an administrator first edits them, and after that
    # administrator's account is deleted.
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class ClassificationPolicy(UUIDv7PrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "classification_policy"

    __table_args__ = (
        sa.Index("uq_classification_policy_one_row", sa.text("(true)"), unique=True),
        sa.CheckConstraint(
            f"threshold BETWEEN 0 AND {MAX_THRESHOLD}", name="ck_classification_policy_threshold"
        ),
    )

    # The highest score that still publishes without review.
    threshold: Mapped[int] = mapped_column(sa.SmallInteger, nullable=False)
    owners_can_change_answers: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
