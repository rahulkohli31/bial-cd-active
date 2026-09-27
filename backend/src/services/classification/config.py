"""The live classification configuration: the active classes and the policy, read together, and
the fingerprint of the class definitions a review reads.

Classes come back in the order they were created, which is how every screen breaks weight ties.
The reviewer's prompt sorts them by key itself, so the fingerprint does not depend on this order.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.classification_config import (
    ClassificationClass,
    ClassificationKind,
    ClassificationPolicy,
)
from src.services.classification.prompts import review_instructions


@dataclass(frozen=True, slots=True)
class LiveClass:
    key: str
    title: str
    description: str
    kind: ClassificationKind
    weight: int | None


@dataclass(frozen=True, slots=True)
class LiveConfig:
    threshold: int
    owners_can_change_answers: bool
    classes: tuple[LiveClass, ...]

    @property
    def fingerprint(self) -> str:
        """The sha256 of the reviewer's static instruction block for these classes. A review is
        current only while its fingerprint matches this; a weight, kind or policy edit leaves it
        unchanged."""
        return hashlib.sha256(review_instructions(self.classes).encode()).hexdigest()


async def load_live_config(db: AsyncSession) -> LiveConfig:
    """The configuration a review, a score or a gate decision uses right now."""
    policy = (
        await db.execute(
            sa.select(
                ClassificationPolicy.threshold, ClassificationPolicy.owners_can_change_answers
            )
        )
    ).one()
    rows = await db.execute(
        sa.select(
            ClassificationClass.key,
            ClassificationClass.title,
            ClassificationClass.description,
            ClassificationClass.kind,
            ClassificationClass.weight,
        )
        .where(ClassificationClass.active.is_(True))
        # UUIDv7: creation order.
        .order_by(ClassificationClass.id)
    )
    return LiveConfig(
        threshold=policy.threshold,
        owners_can_change_answers=policy.owners_can_change_answers,
        classes=tuple(
            LiveClass(key=key, title=title, description=description, kind=kind, weight=weight)
            for key, title, description, kind, weight in rows
        ),
    )
