"""The version list: which entries are offered, which markers each carries, and what drops next.

THE MARKERS ARE THE POINT OF THIS FILE. Whether the live version is one of the two most recent is
a question about the deployment record and the stored rows together, and getting it wrong lists
the same content twice under two headings — the failure the rule "markers are a set, not a choice"
exists to prevent. Each case below is one shape that question can take.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.deployment import Deployment, DeploymentStatus
from src.services.build_sessions.versions import (
    LIVE_NOT_IN_HISTORY,
    WORKSPACE_NOT_RUNNING,
    Marker,
    live_head_sha,
    offered,
    record,
    what_the_next_save_evicts,
)
from src.services.storage.keys import version_key
from tests.factories import AppRegistryFactory, ProjectFactory, UserFactory


async def _app(db: AsyncSession, email: str):
    user = await UserFactory.create(db, email=email)
    project = await ProjectFactory.create(db, user.id)
    app = await AppRegistryFactory.create(db, user_id=user.id, project_id=project.id)
    return user, app


async def _save(db: AsyncSession, user, app_id: uuid.UUID, *, at: datetime, sha: str, note=None):
    return await record(
        db,
        user_id=user.id,
        app_id=app_id,
        saved_at=at,
        head_sha=sha,
        blob_key=version_key(app_id, at),
        description=note,
    )


async def _deployed(
    db: AsyncSession, user, app_id: uuid.UUID, *, sha: str, succeeded=True
) -> None:
    db.add(
        Deployment(
            user_id=user.id,
            app_id=app_id,
            status=DeploymentStatus.SUCCEEDED if succeeded else DeploymentStatus.FAILED,
            head_sha=sha,
            url="https://live.example/" if succeeded else None,
        )
    )
    await db.flush()


async def test_two_saves_and_no_deployment_offer_both(db_session: AsyncSession) -> None:
    """The ordinary shape: what the citizen saved, newest first, and nothing is live."""
    user, app = await _app(db_session, "v-two@rvaiglobal.com")
    now = datetime.now(UTC)
    await _save(db_session, user, app.id, at=now - timedelta(hours=2), sha="a" * 40)
    await _save(db_session, user, app.id, at=now, sha="b" * 40, note="Added approval")

    entries = await offered(db_session, user_id=user.id, app_id=app.id, workspace_running=True)

    assert [e.markers for e in entries] == [(Marker.CURRENT,), (Marker.PREVIOUS,)]
    assert entries[0].description == "Added approval"
    # The current row is inert: it is what the workspace already holds.
    assert [e.available for e in entries] == [False, True]


async def test_a_third_save_leaves_the_newest_two(db_session: AsyncSession) -> None:
    """★ NOTHING IS DELETED, AND THAT IS THE RULE THIS PINS. A third save pushes the oldest out
    of the LIST; its row and its bundle both remain, because a version that falls off stops being
    offered rather than stopping existing.

    Mutation receipt: raise the slot count and three entries come back.
    """
    user, app = await _app(db_session, "v-three@rvaiglobal.com")
    now = datetime.now(UTC)
    oldest = await _save(db_session, user, app.id, at=now - timedelta(hours=3), sha="a" * 40)
    await _save(db_session, user, app.id, at=now - timedelta(hours=2), sha="b" * 40)
    await _save(db_session, user, app.id, at=now, sha="c" * 40)

    entries = await offered(db_session, user_id=user.id, app_id=app.id, workspace_running=True)

    assert len(entries) == 2
    assert oldest.id not in {e.id for e in entries}
    # Still stored: the row is reachable by id even though the list no longer offers it.
    from src.services.build_sessions.versions import by_id

    assert (
        await by_id(db_session, user_id=user.id, app_id=app.id, version_id=oldest.id) is not None
    )


async def test_live_older_than_both_slots_is_listed_third(db_session: AsyncSession) -> None:
    """The case the retention rule exists for: BIAL staff are running something older than
    anything the two slots hold, so the list carries a third entry to reach it by."""
    user, app = await _app(db_session, "v-livethird@rvaiglobal.com")
    now = datetime.now(UTC)
    await _save(db_session, user, app.id, at=now - timedelta(days=3), sha="a" * 40)
    await _save(db_session, user, app.id, at=now - timedelta(hours=2), sha="b" * 40)
    await _save(db_session, user, app.id, at=now, sha="c" * 40)
    await _deployed(db_session, user, app.id, sha="a" * 40)

    entries = await offered(db_session, user_id=user.id, app_id=app.id, workspace_running=True)

    assert [e.markers for e in entries] == [
        (Marker.CURRENT,),
        (Marker.PREVIOUS,),
        (Marker.LIVE,),
    ]
    assert entries[2].available is True


async def test_live_is_also_the_newest_save_carries_both_markers(db_session: AsyncSession) -> None:
    """★ MARKERS ARE A SET, NOT A CHOICE, and this is the combination that proves it. When the
    live version is also the newest save, ONE row carries CURRENT and LIVE together — two entries,
    not three, because the same content is never listed twice.

    Mutation receipt: let the live marker replace the current one and the top row stops saying
    what the workspace holds; append a second entry instead and the same save is listed twice.
    """
    user, app = await _app(db_session, "v-liveboth@rvaiglobal.com")
    now = datetime.now(UTC)
    await _save(db_session, user, app.id, at=now - timedelta(hours=2), sha="a" * 40)
    await _save(db_session, user, app.id, at=now, sha="b" * 40)
    await _deployed(db_session, user, app.id, sha="b" * 40)

    entries = await offered(db_session, user_id=user.id, app_id=app.id, workspace_running=True)

    assert len(entries) == 2
    assert set(entries[0].markers) == {Marker.CURRENT, Marker.LIVE}


async def test_a_failed_deploy_never_becomes_the_live_one(db_session: AsyncSession) -> None:
    """A failed deploy leaves the previous success serving, so the marker stays where it was.
    Reading the newest attempt rather than the newest SUCCESS would move it onto code nobody is
    running."""
    user, app = await _app(db_session, "v-failed@rvaiglobal.com")
    now = datetime.now(UTC)
    await _save(db_session, user, app.id, at=now - timedelta(days=2), sha="a" * 40)
    await _save(db_session, user, app.id, at=now - timedelta(hours=1), sha="b" * 40)
    await _save(db_session, user, app.id, at=now, sha="c" * 40)
    await _deployed(db_session, user, app.id, sha="a" * 40)
    await _deployed(db_session, user, app.id, sha="c" * 40, succeeded=False)

    live = await live_head_sha(db_session, user_id=user.id, app_id=app.id)
    entries = await offered(db_session, user_id=user.id, app_id=app.id, workspace_running=True)

    # The success is what serves, so the marker names it — not the newer failed attempt.
    assert live == "a" * 40
    assert entries[-1].markers == (Marker.LIVE,)
    assert entries[-1].saved_at is not None


async def test_a_live_commit_with_no_stored_copy_is_listed_and_explains_itself(
    db_session: AsyncSession,
) -> None:
    """★ THE ORDINARY PATH FOR MONTHS, NOT AN EDGE CASE. Apps already live when this shipped need
    no migration, so their live commit names a tree the platform never stored a version of.

    The entry is still listed and still marked, with its rollback refused and the reason given
    before the press: an entry that silently vanishes tells the citizen less about what BIAL staff
    are running than one that explains itself.
    """
    user, app = await _app(db_session, "v-unreachable@rvaiglobal.com")
    now = datetime.now(UTC)
    await _save(db_session, user, app.id, at=now, sha="b" * 40)
    await _deployed(db_session, user, app.id, sha="f" * 40)

    entries = await offered(db_session, user_id=user.id, app_id=app.id, workspace_running=True)

    live = entries[-1]
    assert live.markers == (Marker.LIVE,)
    assert live.id is None and live.saved_at is None
    assert live.available is False
    assert live.unavailable_reason == LIVE_NOT_IN_HISTORY


async def test_a_stopped_workspace_still_lists_with_the_reason(db_session: AsyncSession) -> None:
    """Entries list; actions dim. Rollback needs a workspace to restore INTO, and the reason is
    stated before the press rather than discovered by it."""
    user, app = await _app(db_session, "v-stopped@rvaiglobal.com")
    now = datetime.now(UTC)
    await _save(db_session, user, app.id, at=now - timedelta(hours=2), sha="a" * 40)
    await _save(db_session, user, app.id, at=now, sha="b" * 40)

    entries = await offered(db_session, user_id=user.id, app_id=app.id, workspace_running=False)

    assert len(entries) == 2
    assert all(e.available is False for e in entries)
    assert entries[1].unavailable_reason == WORKSPACE_NOT_RUNNING


async def test_nothing_drops_until_there_are_two_versions(db_session: AsyncSession) -> None:
    """The second slot is named before it exists, so the dialog stays silent while it is empty."""
    user, app = await _app(db_session, "v-evict-none@rvaiglobal.com")
    await _save(db_session, user, app.id, at=datetime.now(UTC), sha="a" * 40)

    assert (
        await what_the_next_save_evicts(
            db_session, user_id=user.id, app_id=app.id, live_head_sha=None
        )
        is None
    )


async def test_the_next_save_names_what_it_drops(db_session: AsyncSession) -> None:
    """★ TOLD, NOT DISCOVERED. The two-slot limit is only a rule if the citizen meets it at the
    moment it applies."""
    user, app = await _app(db_session, "v-evict@rvaiglobal.com")
    now = datetime.now(UTC)
    older = await _save(db_session, user, app.id, at=now - timedelta(hours=2), sha="a" * 40)
    await _save(db_session, user, app.id, at=now, sha="b" * 40)

    dropping = await what_the_next_save_evicts(
        db_session, user_id=user.id, app_id=app.id, live_head_sha=None
    )

    assert dropping is not None and dropping.id == older.id


async def test_nothing_drops_when_the_one_falling_out_is_live(db_session: AsyncSession) -> None:
    """The list keeps the live version as a third entry, so the Save costs the citizen nothing —
    and a warning about something that will not happen trains people to dismiss the dialog."""
    user, app = await _app(db_session, "v-evict-live@rvaiglobal.com")
    now = datetime.now(UTC)
    await _save(db_session, user, app.id, at=now - timedelta(hours=2), sha="a" * 40)
    await _save(db_session, user, app.id, at=now, sha="b" * 40)

    assert (
        await what_the_next_save_evicts(
            db_session, user_id=user.id, app_id=app.id, live_head_sha="a" * 40
        )
        is None
    )
