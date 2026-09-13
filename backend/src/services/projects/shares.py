"""Sharing business logic (#198 R1-R11): create/revoke a share, search colleagues to share
with, and list shares from either side of the relationship.

Every function here takes an already-resolved `Project` the caller is known to own — the
router's own `owned_project_or_404` establishes that before any of these run, so nothing in
this module re-checks ownership. That keeps the "who may call this" question in exactly one
place (the router's dependency chain), matching `resolve.py`'s own stated design.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.errors import AppApiError
from src.db.models.app_registry import AppRegistry
from src.db.models.project import Project
from src.db.models.project_share import ProjectShare
from src.db.models.user import User
from src.services.audit.log import append_audit

#: R4 — below this, the search is refused rather than run (a 1-2 character query against an
#: anchored match is either near-useless or, for very short strings, still wide enough to
#: annoy). No maximum is declared here beyond the schema's own paste-backstop character cap
#: (`ColleagueSearchQuery` in `schemas/shares.py`) — an over-long query simply matches nothing.
MIN_COLLEAGUE_QUERY_CHARS = 3

#: R4 — at most ten results, so the picker stays a picker rather than a second roster view.
MAX_COLLEAGUE_RESULTS = 10


async def _owners_app_id(db: AsyncSession, project: Project) -> uuid.UUID | None:
    """The project's one app, owner-scoped. `None` for a project that has never built —
    which is also, definitionally, a project with nothing saved to share (R10)."""
    app_id: uuid.UUID | None = await db.scalar(
        sa.select(AppRegistry.id).where(
            AppRegistry.project_id == project.id, AppRegistry.user_id == project.user_id
        )
    )
    return app_id


async def create_share(
    db: AsyncSession, *, project: Project, actor_id: uuid.UUID, colleague_id: uuid.UUID
) -> ProjectShare:
    """Share `project` — which the caller must already own; enforced by the router's
    `owned_project_or_404` before this runs, not re-checked here — with `colleague_id`.

    IDEMPOTENT (R3): re-sharing with the same colleague returns the existing row rather than
    erroring, duplicating, or writing a second audit entry for a grant that already stood.

    REFUSES SELF-SHARE (R2) and REFUSES A PROJECT WITH NOTHING SAVED (R10) — EXPLICITLY the
    saved snapshot via `snapshot_presence`, never `restorable_presence`'s broader "saved OR
    autosaved" answer: the shared runtime only ever restores from the saved version (R21), so
    a share backed only by an autosave would hand a recipient nothing launchable. An UNKNOWN
    presence (`None` — the object store could not be reached) refuses too: this is a CREATE
    gate, not Launch's own missing-snapshot disable (R10's second sentence), and the safer
    direction when the platform cannot tell is closed, not open.
    """
    # Lazy import: `build_sessions` itself imports `owned_project_or_404` from this package
    # at module level (`appdata.py`), so a module-level import here would deadlock the two
    # packages against each other on load order.
    from src.services.build_sessions.manager import snapshot_presence

    if actor_id == colleague_id:
        raise AppApiError(400, "You can't share a project with yourself.")

    app_id = await _owners_app_id(db, project)
    saved = await snapshot_presence(app_id) if app_id is not None else False
    if saved is not True:
        raise AppApiError(
            400,
            "Save a version of this app before sharing it — there's nothing to share yet.",
            code="no_saved_snapshot",
        )

    insert_result = cast(
        "sa.CursorResult[Any]",
        await db.execute(
            pg_insert(ProjectShare)
            .values(project_id=project.id, shared_with_user_id=colleague_id)
            .on_conflict_do_nothing(
                index_elements=[ProjectShare.project_id, ProjectShare.shared_with_user_id]
            )
        ),
    )
    is_new = insert_result.rowcount > 0

    share = await db.scalar(
        sa.select(ProjectShare).where(
            ProjectShare.project_id == project.id,
            ProjectShare.shared_with_user_id == colleague_id,
        )
    )
    # The row above was either just inserted or already existed — either way it is there now;
    # a `None` here would mean the unique constraint's own guarantee failed to hold.
    assert share is not None

    if is_new:
        app_status = await db.scalar(sa.select(AppRegistry.status).where(AppRegistry.id == app_id))
        # Ids and an enum value only — never the colleague's name/email (`append_audit`'s own
        # "names go in the operator report, this row gets ints" rule).
        await append_audit(
            db,
            actor_id=actor_id,
            action="project:share_create",
            resource_type="project",
            resource_id=str(project.id),
            detail={
                "sharedWithUserId": str(colleague_id),
                "appStatus": app_status.value if app_status is not None else None,
            },
        )
    return share


async def revoke_share(
    db: AsyncSession, *, project: Project, actor_id: uuid.UUID, colleague_id: uuid.UUID
) -> bool:
    """Revoke `project`'s share with `colleague_id`. Returns whether a row actually existed —
    revoking an already-revoked (or never-existing) share is a normal double-click/retry, not
    an error, mirroring `release_project_sandbox`'s "released: false is a success" posture.
    Tearing down the recipient's live container is the caller's job once the shared runtime
    exists (R25) — this function only ever owns the membership row and its audit trail."""
    result = cast(
        "sa.CursorResult[Any]",
        await db.execute(
            sa.delete(ProjectShare).where(
                ProjectShare.project_id == project.id,
                ProjectShare.shared_with_user_id == colleague_id,
            )
        ),
    )
    revoked = result.rowcount > 0
    if revoked:
        await append_audit(
            db,
            actor_id=actor_id,
            action="project:share_revoke",
            resource_type="project",
            resource_id=str(project.id),
            detail={"sharedWithUserId": str(colleague_id)},
        )
    return revoked


def _escape_for_ilike(value: str) -> str:
    """Escape `%`/`_` (and the escape character itself, first) so a HAND-BUILT `ILIKE` pattern
    treats `value` as literal text rather than live wildcards — the backslash-order matters:
    escaping `\\` before `%`/`_` is what stops a literal backslash in `value` from re-arming
    one of them. `.istartswith(value, autoescape=True)` already does this for a value that IS
    the whole pattern; this is for the one shape below that is not — a match anchored after a
    literal space, which needs the delimiter concatenated onto an already-escaped core."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def search_colleagues(
    db: AsyncSession, *, requester_id: uuid.UUID, query: str
) -> list[User]:
    """Anchored colleague search (R4): matches the START of a display-name TOKEN (any word in
    the name, not just the first) or the START of the email local part — NEVER a substring
    match, which on a tenant where every user shares one email domain would match every user
    in it against any three characters of that shared domain. Excludes the requester (R2 —
    "the user search excludes the requester so they never appear in their own results").

    ONE MECHANISM, ESCAPED ONCE — both arms are `ILIKE` prefix patterns now, not a Postgres
    POSIX regex for the name and an unescaped `ILIKE` for the email. The email arm used to
    interpolate `query` straight into a pattern (`f"{query}%"`): `%`/`_` in `query` were live
    wildcards, so `q=%%%` matched every local part and `q=___` matched every one of three-plus
    characters, defeating this exact anchoring rule in one keystroke. `.istartswith(...,
    autoescape=True)` closes that on the prefix arms; `_escape_for_ilike` covers the one
    pattern SQLAlchemy's convenience methods cannot build for you — "starts right after a
    literal space", not "starts the whole value".

    `query` has already cleared `MIN_COLLEAGUE_QUERY_CHARS` and the character-cap paste
    backstop at the schema boundary (`ColleagueSearchQuery`) — this function trusts both.
    """
    later_token_pattern = f"% {_escape_for_ilike(query)}%"
    rows = await db.scalars(
        sa.select(User)
        .where(
            User.id != requester_id,
            sa.or_(
                User.display_name.istartswith(query, autoescape=True),  # the first token
                User.display_name.ilike(later_token_pattern, escape="\\"),  # any later token
                sa.func.split_part(User.email, "@", 1).istartswith(query, autoescape=True),
            ),
        )
        .order_by(User.display_name, User.email)
        .limit(MAX_COLLEAGUE_RESULTS)
    )
    return list(rows.all())


@dataclass(frozen=True)
class ShareWithRecipient:
    """One row of a project's OWN share panel — who it is shared with, and when (R12)."""

    share: ProjectShare
    recipient: User


async def list_shares_for_project(
    db: AsyncSession, project_id: uuid.UUID
) -> list[ShareWithRecipient]:
    """Every current share for a project the caller already owns — feeds the owner's own
    share panel, which is what makes `revoke_share` usable (you have to see who holds access
    before you can pick who to take it from)."""
    rows = await db.execute(
        sa.select(ProjectShare, User)
        .join(User, User.id == ProjectShare.shared_with_user_id)
        .where(ProjectShare.project_id == project_id)
        .order_by(ProjectShare.created_at.desc())
    )
    return [ShareWithRecipient(share=share, recipient=user) for share, user in rows.all()]


@dataclass(frozen=True)
class SharedProjectEntry:
    """One row of the recipient's "Shared with me" list — the project, who shared it, and
    when (R12). `share_id` is the keyset cursor `list_shared_with_me` paginates on — the
    SHARE's own id, not the project's, since ordering is by grant recency and one project can
    (in principle) recur if it were ever unshared and re-shared."""

    share_id: uuid.UUID
    project: Project
    shared_by: User
    shared_at: datetime


async def list_shared_with_me(
    db: AsyncSession, user_id: uuid.UUID, *, limit: int, cursor: uuid.UUID | None
) -> list[SharedProjectEntry]:
    """Projects shared with the caller, newest grant first — KEYSET paginated (the platform's
    default, `pagination.py`), deliberately NOT the numbered-offset shape the owner's own
    project list uses. That list's offset choice is justified specifically because it is
    single-writer (only the viewer themself creates/deletes their own rows); a shared list is
    written to by every colleague who shares or revokes with this recipient, so the skew
    offset pagination cannot promise is a real one here, not the bounded, self-inflicted
    window the owner's list accepts.

    Joins through `Project.user_id` for the sharer's identity — there is no `shared_by_user_id`
    column (`db/models/project_share.py` explains why): the project's owner IS who shared it,
    always, since only an owner can create a share.
    """
    query = (
        sa.select(ProjectShare, Project, User)
        .join(Project, Project.id == ProjectShare.project_id)
        .join(User, User.id == Project.user_id)
        .where(ProjectShare.shared_with_user_id == user_id)
    )
    if cursor is not None:
        query = query.where(ProjectShare.id < cursor)
    rows = await db.execute(query.order_by(ProjectShare.id.desc()).limit(limit))
    return [
        SharedProjectEntry(
            share_id=share.id, project=project, shared_by=user, shared_at=share.created_at
        )
        for share, project, user in rows.all()
    ]
