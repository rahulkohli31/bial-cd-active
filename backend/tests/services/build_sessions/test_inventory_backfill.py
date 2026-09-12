"""`_app_names_to_owners` / `backfill_sandbox_tags` (#198) — the Azure-inventory half of the
same fail-closed-name-guard requirement `test_reaper.py`/`test_reclaim.py` cover for their own
guards. Before this, the map only ever derived `sbx-` names: an untagged `shr-` container found
by the backfill matched nothing here and was stamped a plain, ownerless `KIND_BUILD_SANDBOX` —
misclassifying a colleague's shared view as an orphaned build sandbox, which the reclaimer would
then judge under the wrong kind entirely.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.project_share import ProjectShare
from src.services.build_sessions.appdata import resolve_app_for_project
from src.services.build_sessions.inventory import _app_names_to_owners, backfill_sandbox_tags
from src.services.build_sessions.manager import app_name_for, shr_name_for
from src.services.sandbox.base import (
    KIND_BUILD_SANDBOX,
    KIND_SHARED_SANDBOX,
    TAG_KIND,
    TAG_USER_ID,
    FleetMember,
)
from tests.factories import ProjectFactory, UserFactory
from tests.fakes import a_fleet_member


class _Tagger:
    """A `FleetTagger` that lists a fixed fleet and records every `stamp_tags` call — the write
    half `test_inventory.py`'s own `_Fleet` (read-only) does not need."""

    def __init__(self, names: list[str]) -> None:
        self.names = names
        self.stamped: dict[str, dict[str, str]] = {}

    async def list_sandbox_fleet(self) -> list[FleetMember]:
        return [a_fleet_member(n) for n in self.names]

    async def stamp_tags(self, *, name: str, tags: dict[str, str]) -> None:
        self.stamped[name] = tags


async def _owner_and_recipient_with_a_share(
    db: AsyncSession,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """An owner's project, shared with one colleague. Returns `(app_id, owner_id, recipient_id)`.
    Inserted directly rather than through `services.projects.shares.create_share` — that
    function's own business rule (refuse a share with nothing saved, R10) is irrelevant to what
    this module reads, which is the `project_shares` row alone."""
    owner = await UserFactory.create(db, email=f"owner-{uuid.uuid4().hex[:8]}@example.com")
    recipient = await UserFactory.create(db, email=f"colleague-{uuid.uuid4().hex[:8]}@example.com")
    project = await ProjectFactory.create(db, owner.id, description="Shared for inventory")
    app_id = await resolve_app_for_project(db, owner.id, project.id)
    db.add(ProjectShare(project_id=project.id, shared_with_user_id=recipient.id))
    await db.commit()
    return app_id, owner.id, recipient.id


async def test_a_names_to_owners_includes_both_the_build_and_the_shared_name(
    db_session: AsyncSession,
) -> None:
    app_id, owner_id, recipient_id = await _owner_and_recipient_with_a_share(db_session)

    known = await _app_names_to_owners(db_session)

    build_name = app_name_for(app_id)
    shared_name = shr_name_for(app_id, recipient_id)
    assert known[build_name].app_id == app_id
    assert known[build_name].user_id == owner_id
    assert known[build_name].kind == KIND_BUILD_SANDBOX
    assert known[shared_name].app_id == app_id
    assert known[shared_name].user_id == recipient_id  # the RECIPIENT, never the owner
    assert known[shared_name].kind == KIND_SHARED_SANDBOX


async def test_backfill_stamps_an_untagged_shared_view_as_shared_not_build(
    db_session: AsyncSession,
) -> None:
    """The regression this file exists to pin: before `_app_names_to_owners` learned the `shr-`
    shape, this exact scenario stamped `KIND_BUILD_SANDBOX` with the RECIPIENT misread as an
    ordinary sandbox owner — the reclaimer would then judge a colleague's shared view under the
    wrong kind, with no escalate-only protection at all."""
    app_id, _owner_id, recipient_id = await _owner_and_recipient_with_a_share(db_session)
    shared_name = shr_name_for(app_id, recipient_id)
    tagger = _Tagger([shared_name])

    report = await backfill_sandbox_tags(db_session, tagger)

    assert report.stamped == 1
    assert report.skipped_no_row == 0
    assert report.unowned == 0
    stamped = tagger.stamped[shared_name]
    assert stamped[TAG_KIND] == KIND_SHARED_SANDBOX
    assert stamped[TAG_USER_ID] == str(recipient_id)


async def test_backfill_leaves_an_unmatched_name_ownerless_and_build_kind(
    db_session: AsyncSession,
) -> None:
    """The existing escalate-forever contract, unchanged: a name that resolves through NEITHER
    map still gets `KIND_BUILD_SANDBOX` with no owner — never `KIND_SHARED_SANDBOX`, which this
    change could have started guessing for any name it failed to place."""
    tagger = _Tagger(["sbx-" + "0" * 28])

    report = await backfill_sandbox_tags(db_session, tagger)

    assert report.stamped == 0
    assert report.skipped_no_row == 1
    stamped = tagger.stamped["sbx-" + "0" * 28]
    assert stamped[TAG_KIND] == KIND_BUILD_SANDBOX
    assert TAG_USER_ID not in stamped
