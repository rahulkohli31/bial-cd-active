"""The live classification configuration: the active classes and the policy, read together.

Classes come back in key order, never kind or weight order, so anything rendered from them stays
byte-identical until a class is added, reworded or switched on or off.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.classification_config import (
    ClassificationClass,
    ClassificationKind,
    ClassificationPolicy,
)


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
        # Byte order, so the order does not depend on the server's collation.
        .order_by(ClassificationClass.key.collate("C"))
    )
    return LiveConfig(
        threshold=policy.threshold,
        owners_can_change_answers=policy.owners_can_change_answers,
        classes=tuple(
            LiveClass(key=key, title=title, description=description, kind=kind, weight=weight)
            for key, title, description, kind, weight in rows
        ),
    )
