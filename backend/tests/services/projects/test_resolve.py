"""`resolve_project_access`'s tri-state logic (#198 R11) — the platform's first legitimate
exception to ADR-0004's "every list/lookup is scoped by a single user_id" rule.

`owned_project_or_404` is a one-line wrapper around this resolver (`resolve.py`'s own
docstring), so a passing test here also pins that every one of its ~20 existing mutating call
sites still sees a binary owner-or-404 — a share never satisfies it.
"""

from __future__ import annotations

import uuid

import pytest

from src.core.errors import AppApiError
from src.services.build_sessions.appdata import resolve_app_for_project
from src.services.projects.resolve import (
    ProjectAccess,
    owned_project_or_404,
    resolve_project_access,
)
from src.services.projects.shares import create_share
from tests.factories import ProjectFactory, UserFactory


async def _share_with(db_session, monkeypatch, *, owner, project, recipient) -> None:
    """Share `project` with `recipient` — bypassing the snapshot-presence gate (R10) via a
    monkeypatched `snapshot_presence`, since these tests are about access resolution, not
    about `create_share`'s own preconditions (covered in `test_shares.py`)."""
    from src.services.build_sessions import manager as build_manager

    async def _fake_snapshot_presence(_app_id):
        return True

    monkeypatch.setattr(build_manager, "snapshot_presence", _fake_snapshot_presence)
    await resolve_app_for_project(db_session, owner.id, project.id)
    await db_session.flush()
    await create_share(db_session, project=project, actor_id=owner.id, colleague_id=recipient.id)
    await db_session.flush()


async def test_the_owner_resolves_as_owner(db_session) -> None:
    owner = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, owner.id)

    resolved = await resolve_project_access(db_session, owner.id, project.id)
    assert resolved.access is ProjectAccess.OWNER
    assert resolved.project.id == project.id


async def test_a_stranger_gets_a_404_not_a_403(db_session) -> None:
    # Non-leaking: a project that exists but isn't visible to this caller reads identically
    # to one that doesn't exist at all.
    owner = await UserFactory.create(db_session)
    stranger = await UserFactory.create(db_session, email="stranger@example.com")
    project = await ProjectFactory.create(db_session, owner.id)

    with pytest.raises(AppApiError) as exc_info:
        await resolve_project_access(db_session, stranger.id, project.id)
    assert exc_info.value.status_code == 404


async def test_a_nonexistent_project_id_gets_the_same_404(db_session) -> None:
    someone = await UserFactory.create(db_session)
    with pytest.raises(AppApiError) as exc_info:
        await resolve_project_access(db_session, someone.id, uuid.uuid4())
    assert exc_info.value.status_code == 404


async def test_a_share_recipient_resolves_as_shared(db_session, monkeypatch) -> None:
    owner = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, owner.id)
    recipient = await UserFactory.create(db_session, email="recipient@example.com")
    await _share_with(db_session, monkeypatch, owner=owner, project=project, recipient=recipient)

    resolved = await resolve_project_access(db_session, recipient.id, project.id)
    assert resolved.access is ProjectAccess.SHARED
    assert resolved.project.id == project.id


async def test_owner_takes_precedence_over_a_share_row_on_their_own_project(db_session) -> None:
    # R11's own stated invariant: a builder must never be handed the restricted recipient
    # view of a project they themselves own. `create_share` itself refuses a self-share (R2),
    # so the only way this data shape could ever exist is a bug elsewhere — the resolver
    # checks ownership FIRST regardless, and this pins that it stays that way even against a
    # stray share row naming the owner as their own recipient.
    from src.db.models.project_share import ProjectShare

    owner = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, owner.id)
    db_session.add(ProjectShare(project_id=project.id, shared_with_user_id=owner.id))
    await db_session.flush()

    resolved = await resolve_project_access(db_session, owner.id, project.id)
    assert resolved.access is ProjectAccess.OWNER


async def test_owned_project_or_404_still_refuses_a_share_recipient(
    db_session, monkeypatch
) -> None:
    # The binding guarantee for every one of `owned_project_or_404`'s ~20 existing mutating
    # call sites: a share widens READS only, never this.
    owner = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, owner.id)
    recipient = await UserFactory.create(db_session, email="recipient@example.com")
    await _share_with(db_session, monkeypatch, owner=owner, project=project, recipient=recipient)

    with pytest.raises(AppApiError) as exc_info:
        await owned_project_or_404(db_session, recipient.id, project.id)
    assert exc_info.value.status_code == 404


async def test_owned_project_or_404_still_works_for_the_owner(db_session) -> None:
    owner = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, owner.id)

    resolved = await owned_project_or_404(db_session, owner.id, project.id)
    assert resolved.id == project.id
