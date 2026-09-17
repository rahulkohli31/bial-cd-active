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
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.app_version import AppVersion
from src.db.models.deployment import Deployment, DeploymentStatus
from src.services.deploy.liveness import live_app_ids

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


class Marker(StrEnum):
    """What an entry IS, and a row may carry more than one.

    ★ A SET, NOT A CHOICE. When the live version is also the newest save, one row carries both
    CURRENT and LIVE rather than one marker replacing the other or the same content being listed
    twice under two headings.
    """

    CURRENT = "current"
    PREVIOUS = "previous"
    LIVE = "live"


@dataclass(frozen=True)
class Entry:
    """One row of the list as the citizen reads it."""

    #: None for a live version with no stored row — see `offered`.
    id: uuid.UUID | None
    saved_at: datetime | None
    description: str | None
    markers: tuple[Marker, ...]
    #: Whether this row can be rolled back to, and why not when it cannot. The reason is
    #: carried so the list can state it BEFORE the press rather than failing after it.
    available: bool
    unavailable_reason: str | None


async def live_head_sha(db: AsyncSession, *, user_id: uuid.UUID, app_id: uuid.UUID) -> str | None:
    """The commit BIAL staff are running, or None when nothing of this app is live.

    Resolved from the newest SUCCESSFUL deployment rather than anything pinned, so an app that
    stops being live — replaced, unpublished, disabled or rejected — simply stops having a live
    entry, with no state of ours to keep in step. Nothing needs migrating for apps already live:
    the answer comes from the deployment record they already have.
    """
    return await db.scalar(
        sa.select(Deployment.head_sha)
        .where(
            Deployment.app_id == app_id,
            # ★ THE NEWEST SUCCESS, NOT THE NEWEST ATTEMPT. A failed deploy leaves the
            # previous one serving, so reading the newest row of any status would move the
            # marker onto code nobody is running — and offer a rollback to it.
            Deployment.status
            == sa.bindparam("live_succeeded", DeploymentStatus.SUCCEEDED, literal_execute=True),
            Deployment.url.is_not(None),
            # `live_app_ids` answers WHICH apps are live at all, which is the half this
            # query cannot see: an app can be disabled or rejected in the registry without
            # anything being stamped on its deployment rows.
            Deployment.app_id.in_(live_app_ids(owner_user_id=user_id)),
        )
        .order_by(Deployment.id.desc())
        .limit(1)
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


#: Why a row cannot be rolled back to right now. Stated before the press, never discovered after.
WORKSPACE_NOT_RUNNING = "Your workspace is not running"
LIVE_NOT_IN_HISTORY = "This version was deployed before the platform kept a copy of it"


async def offered(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    app_id: uuid.UUID,
    workspace_running: bool,
) -> list[Entry]:
    """The list: the two most recent saves, plus the live version when it is neither of them.

    ★ THE MARKERS ARE RESOLVED HERE, NOT IN THE CLIENT. Whether the live version is one of the
    two most recent is a question about the newest successful deployment and the stored rows
    together; asking it once means the same content is never listed twice, and the browser is
    never in a position to answer it differently.

    THE THIRD ENTRY IS ORDINARY, NOT AN EDGE CASE. An app that was live before this feature
    shipped has a deployment record and no version row — no migration was needed, and none was
    written — so for months the common shape is a live entry that names a commit nothing stored.
    It is still listed, marked, and refused with its reason, because an entry that silently
    vanishes tells the citizen less than one that explains itself.
    """
    live_sha = await live_head_sha(db, user_id=user_id, app_id=app_id)
    recent = await most_recent(db, user_id=user_id, app_id=app_id, limit=OFFERED_SLOTS)

    entries: list[Entry] = []
    for index, version in enumerate(recent):
        markers = [Marker.CURRENT if index == 0 else Marker.PREVIOUS]
        if live_sha is not None and version.head_sha == live_sha:
            markers.append(Marker.LIVE)
        entries.append(
            Entry(
                id=version.id,
                saved_at=version.saved_at,
                description=version.description,
                markers=tuple(markers),
                # The current row is inert: it is what the workspace already holds, so rolling
                # back to it is a press with nothing to do.
                available=workspace_running and index != 0,
                unavailable_reason=None if workspace_running else WORKSPACE_NOT_RUNNING,
            )
        )

    if live_sha is None or any(Marker.LIVE in entry.markers for entry in entries):
        return entries

    # Live, and neither of the two most recent. It may still be a version we hold — an app
    # deployed from four saves back — or one from before any of this existed.
    stored = await _version_with_head(db, user_id=user_id, app_id=app_id, head_sha=live_sha)
    if stored is None:
        entries.append(
            Entry(
                id=None,
                saved_at=None,
                description=None,
                markers=(Marker.LIVE,),
                available=False,
                unavailable_reason=LIVE_NOT_IN_HISTORY,
            )
        )
        return entries

    entries.append(
        Entry(
            id=stored.id,
            saved_at=stored.saved_at,
            description=stored.description,
            markers=(Marker.LIVE,),
            available=workspace_running,
            unavailable_reason=None if workspace_running else WORKSPACE_NOT_RUNNING,
        )
    )
    return entries


async def _version_with_head(
    db: AsyncSession, *, user_id: uuid.UUID, app_id: uuid.UUID, head_sha: str
) -> Version | None:
    """The newest stored version holding this commit, or None when none does."""
    row = (
        (
            await db.execute(
                sa.select(AppVersion)
                .where(
                    AppVersion.user_id == user_id,
                    AppVersion.app_id == app_id,
                    AppVersion.head_sha == head_sha,
                )
                .order_by(AppVersion.saved_at.desc(), AppVersion.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    return _as_version(row) if row is not None else None


async def by_id(
    db: AsyncSession, *, user_id: uuid.UUID, app_id: uuid.UUID, version_id: uuid.UUID
) -> Version | None:
    """One version, owner- and app-scoped. None when it is not this citizen's to reach."""
    row = (
        (
            await db.execute(
                sa.select(AppVersion).where(
                    AppVersion.id == version_id,
                    AppVersion.user_id == user_id,
                    AppVersion.app_id == app_id,
                )
            )
        )
        .scalars()
        .first()
    )
    return _as_version(row) if row is not None else None
