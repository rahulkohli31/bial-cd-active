"""The app's saved versions: what the list offers, and what a Save leaves behind.

WHY THIS EXISTS

A Save used to overwrite one stored copy, so the only version a citizen could reach was the latest.
A version is what a Save now leaves behind — a frozen bundle plus the row naming it — and this
module owns the two questions the rest of the system asks about them: which versions are OFFERED,
and which one a Save is about to push off that list.

THE LIST IS A VIEW, NOT THE TRUTH. Two rows are offered — the most recent pair — plus whatever is
live. A third stops being offered; its row and its bundle both remain, because a citizen who saved
something is owed the ability to find it again if the rule ever widens.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.app_version import AppVersion

#: How many saves the list offers, before the live version is considered. Two, and the number is
#: the feature rather than a tuning knob: the second slot is named in the Save dialog before it
#: exists, so a citizen is TOLD the limit rather than discovering it as a loss.
OFFERED_SLOTS = 2


@dataclass(frozen=True)
class Version:
    """One row as the rest of the system reads it."""

    id: uuid.UUID
    saved_at: datetime
    description: str | None
    head_sha: str
    blob_key: str


def _as_version(row: AppVersion) -> Version:
    return Version(
        id=row.id,
        saved_at=row.saved_at,
        description=row.description,
        head_sha=row.head_sha,
        blob_key=row.blob_key,
    )


async def most_recent(
    db: AsyncSession, *, user_id: uuid.UUID, app_id: uuid.UUID, limit: int = OFFERED_SLOTS
) -> list[Version]:
    """The newest versions first, owner-scoped.

    OWNER-SCOPED EVEN THOUGH `app_id` IMPLIES THE OWNER, like every other read on this platform:
    the id is client-supplied on the paths that reach here, and a dropped predicate is a
    cross-user leak rather than a wrong answer.
    """
    rows = (
        (
            await db.execute(
                sa.select(AppVersion)
                .where(AppVersion.user_id == user_id, AppVersion.app_id == app_id)
                .order_by(AppVersion.saved_at.desc(), AppVersion.id.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [_as_version(row) for row in rows]


async def what_the_next_save_evicts(
    db: AsyncSession, *, user_id: uuid.UUID, app_id: uuid.UUID, live_head_sha: str | None
) -> Version | None:
    """The version a Save would push off the list, or None when nothing drops.

    ★ NAMED BEFORE IT HAPPENS, which is the whole point of R5. The two-slot limit is only ever a
    rule if the citizen is told it at the moment it applies; discovered afterwards it is a loss.

    Nothing drops when the app has fewer than two versions — there is no third to displace. And
    nothing drops when the version that would fall out is the LIVE one: the list keeps it as its
    third entry, so the Save costs the citizen nothing and saying otherwise would be a warning
    about something that is not going to happen.
    """
    offered = await most_recent(db, user_id=user_id, app_id=app_id, limit=OFFERED_SLOTS)
    if len(offered) < OFFERED_SLOTS:
        return None
    falling_out = offered[-1]
    if live_head_sha is not None and falling_out.head_sha == live_head_sha:
        return None
    return falling_out


async def record(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    app_id: uuid.UUID,
    saved_at: datetime,
    head_sha: str,
    blob_key: str,
    description: str | None,
    restored_from: uuid.UUID | None = None,
) -> Version:
    """Add one version. Flushes rather than commits: the caller owns the transaction.

    A description that is empty or only whitespace is stored as None rather than as an empty
    string, so "no description" is one state in the database instead of two the list would have
    to treat alike.
    """
    described = (description or "").strip() or None
    row = AppVersion(
        user_id=user_id,
        app_id=app_id,
        saved_at=saved_at,
        description=described,
        head_sha=head_sha,
        blob_key=blob_key,
        restored_from_id=restored_from,
    )
    db.add(row)
    await db.flush()
    return _as_version(row)
