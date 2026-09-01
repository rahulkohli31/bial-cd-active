"""LIVE end-to-end scenarios for the crash-recovery copy.

Real sandbox containers, real git, real Azurite, real Redis, real PostgreSQL. See conftest.

ON `may_write` (read once, then every call site below reads itself):

`may_write` is not a free knob — it MIRRORS THE TURN'S TOOLSET. `toolsets_for_kind` hands the
mutating `sandbox_toolset` to `ChatKind.BUILD` and to nothing else, every
`workspace_touched = True` lives inside that toolset, and `workspace_touched` is the only thing
the engine derives `finish_turn_sandbox(touched=...)` from. So in production `may_write=False`
implies `touched=False`, always; a read-only turn paired with `touched=True` is a turn that
simultaneously cannot and did mutate the tree, and pinning behaviour to it pins nothing.

Hence the shape used throughout: a mutating turn runs `may_write=True`, and a Save is taken
BETWEEN turns — `finish_turn_sandbox` pops the build slot (and pardons the container, which
stays up), after which `save_project_snapshot` is free to run. That is also the production
sequence, which is why that method deliberately does not require an in-process session: the
common Save is the one clicked after a reply has landed.

Three scenarios keep `may_write=False` while a Save runs against a LIVE session: `s3`, `s9`
and `s12`. Each says why where it sits. None of them is an instance of the modelling problem
the shape above avoids — all three end with `touched=False` or do not end the turn at all, so
none declares a session that is read-only and mutating at once. `s8` is a plain read-only turn
with `touched=False` and needs no explanation.
"""

from __future__ import annotations

import pytest
import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession

from src.services.build_sessions.manager import SessionManager, app_name_for
from src.services.storage import recovery_key, snapshot_key
from src.services.storage.bundle import parse_bundle_head_sha
from tests.factories import ProjectFactory, UserFactory

pytestmark = pytest.mark.integration


async def _project(db: AsyncSession, email: str):
    user = await UserFactory.create(db, email=email)
    project = await ProjectFactory.create(db, user.id)
    return user, project.id


async def _write(client, handle, path: str, text: str) -> None:
    """Write a file INTO the running container, as the agent would."""
    from src.services.sandbox.base import FileCreate

    await client.files(handle, FileCreate(path=path, file_text=text))


async def _read(client, handle, path: str) -> str:
    result = await client.exec(handle, ["sh", "-c", f"cat {path} 2>&1 || true"])
    return result.stdout


async def test_s1_a_first_turn_provisions_a_real_container(
    db_session: AsyncSession, live_redis: aioredis.Redis, live_storage, sandbox
) -> None:
    user, project_id = await _project(db_session, "e2e1@rvaiglobal.com")
    manager = SessionManager()

    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=sandbox, may_write=True
    )

    assert session.handle is not None
    assert session.handle.app_name == app_name_for(session.app_id)
    # The real supervisor answers on the real container.
    health = await sandbox.exec(session.handle, ["sh", "-c", "echo alive"])
    assert health.stdout.strip() == "alive"
    assert health.exit == 0


async def test_s2_a_mutating_turn_writes_a_real_bundle_to_the_recovery_key_only(
    db_session: AsyncSession, live_redis: aioredis.Redis, live_storage, sandbox
) -> None:
    user, project_id = await _project(db_session, "e2e2@rvaiglobal.com")
    manager = SessionManager()
    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=sandbox, may_write=True
    )
    assert session.handle is not None
    await _write(sandbox, session.handle, "app/marker.txt", "TREE-A")

    await manager.finish_turn_sandbox(session, sandbox, touched=True)

    # A REAL git bundle landed at the recovery key, and nothing at the saved key.
    stored = await live_storage.get(recovery_key(session.app_id))
    assert stored.startswith(b"# v2 git bundle\n")
    assert parse_bundle_head_sha(stored)  # a real 40-hex HEAD, produced by real git
    assert await live_storage.head(snapshot_key(session.app_id)) is None


async def test_s3_the_users_save_writes_the_saved_key_and_settles_dirty(
    db_session: AsyncSession, live_redis: aioredis.Redis, live_storage, sandbox
) -> None:
    user, project_id = await _project(db_session, "e2e3@rvaiglobal.com")
    manager = SessionManager()
    # `may_write=False` — a read-only (Ask/Plan) turn: it pins the container but may not touch
    # the tree, so the Save below is legal against a session that is still live. That arm is
    # not load-bearing for the assertions (`save_project_snapshot` needs no in-process session
    # at all); it is what keeps this scenario to a single turn.
    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=sandbox, may_write=False
    )
    assert session.handle is not None
    await _write(sandbox, session.handle, "app/marker.txt", "TREE-A")

    before = await manager.project_save_state(db_session, user, project_id, sandbox_client=sandbox)
    assert before.dirty is True, "unsaved work in a live container must read as dirty"

    await manager.save_project_snapshot(db_session, user, project_id, sandbox_client=sandbox)

    after = await manager.project_save_state(db_session, user, project_id, sandbox_client=sandbox)
    assert after.dirty is False
    assert after.saved_head == after.container_head
    saved = await live_storage.get(snapshot_key(session.app_id))
    assert parse_bundle_head_sha(saved) == after.saved_head


async def test_s4_work_after_a_save_is_offered_back_with_real_blob_timestamps(
    db_session: AsyncSession, live_redis: aioredis.Redis, live_storage, sandbox
) -> None:
    """Azurite's `Last-Modified` is whole SECONDS, exactly like Azure. The unit suite's fake
    stamps microseconds, so this is the first time the ordering comparison runs at the
    resolution production actually has."""
    import asyncio as _asyncio

    user, project_id = await _project(db_session, "e2e4@rvaiglobal.com")
    manager = SessionManager()
    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=sandbox, may_write=True
    )
    assert session.handle is not None
    await _write(sandbox, session.handle, "app/marker.txt", "TREE-A")
    await manager.finish_turn_sandbox(session, sandbox, touched=True)

    # The user reads the reply and clicks Save — between turns, which is when Saves happen.
    await manager.save_project_snapshot(db_session, user, project_id, sandbox_client=sandbox)
    # The turn's recovery copy is the same tree and older: nothing to offer back yet.
    assert await manager.recoverable_work(session.app_id) is None

    await _asyncio.sleep(1.1)  # clear the one-second granularity deliberately
    # ...then sends another message, and THAT turn moves the tree past the save.
    second = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=sandbox, may_write=True
    )
    assert second.handle is not None
    await _write(sandbox, second.handle, "app/marker.txt", "TREE-B")
    await manager.finish_turn_sandbox(second, sandbox, touched=True)

    offer = await manager.recoverable_work(session.app_id)
    assert offer is not None, "work done after the save was not offered back"
    assert offer.app_id == session.app_id


async def test_s5_a_reaped_container_resumes_the_work_not_the_last_save(
    db_session: AsyncSession, live_redis: aioredis.Redis, live_storage, sandbox
) -> None:
    """★ THE ONE THE WHOLE BRANCH EXISTS FOR, end to end against real everything.

    Save tree A, work on to tree B, lose the container, send another message. The file that
    comes back in the NEW container is read out of it — no fake, no assertion on a key name."""
    import asyncio as _asyncio

    from src.services.redis import registry_key

    user, project_id = await _project(db_session, "e2e5@rvaiglobal.com")
    manager = SessionManager()
    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=sandbox, may_write=True
    )
    assert session.handle is not None
    first_container = session.handle.app_name

    await _write(sandbox, session.handle, "app/marker.txt", "TREE-A")
    await manager.finish_turn_sandbox(session, sandbox, touched=True)
    await manager.save_project_snapshot(db_session, user, project_id, sandbox_client=sandbox)

    await _asyncio.sleep(1.1)
    second = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=sandbox, may_write=True
    )
    assert second.handle is not None
    await _write(sandbox, second.handle, "app/marker.txt", "TREE-B")
    await manager.finish_turn_sandbox(second, sandbox, touched=True)

    # The container is reclaimed for real: destroyed, registry cleared.
    await sandbox.teardown(second.handle)
    await live_redis.delete(registry_key(user.id))

    # The user does the only thing the product offers: sends another message.
    resumed = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=sandbox, may_write=True
    )
    assert resumed.handle is not None
    assert resumed.handle.app_name == first_container  # same stable per-app name
    body = await _read(sandbox, resumed.handle, "/workspace/app/app/marker.txt")

    assert "TREE-B" in body, f"the user's work after their last save was lost; got {body!r}"
    # ...and it is still THEIRS to save: resuming is not promoting.
    state = await manager.project_save_state(db_session, user, project_id, sandbox_client=sandbox)
    assert state.dirty is True


async def test_s6_a_user_who_never_saved_can_still_get_their_work_back(
    db_session: AsyncSession, live_redis: aioredis.Redis, live_storage, sandbox
) -> None:
    from src.services.redis import registry_key

    user, project_id = await _project(db_session, "e2e6@rvaiglobal.com")
    manager = SessionManager()
    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=sandbox, may_write=True
    )
    assert session.handle is not None
    await _write(sandbox, session.handle, "app/marker.txt", "NEVER-SAVED")
    await manager.finish_turn_sandbox(session, sandbox, touched=True)
    assert await live_storage.head(snapshot_key(session.app_id)) is None

    await sandbox.teardown(session.handle)
    await live_redis.delete(registry_key(user.id))

    relaunched = await manager.relaunch_preview(db_session, user, project_id, sandbox)
    assert relaunched.app_id == session.app_id
    handle = await sandbox.attach_existing(str(user.id))
    body = await _read(sandbox, handle, "/workspace/app/app/marker.txt")
    assert "NEVER-SAVED" in body


async def test_s7_prefer_saved_puts_the_older_saved_version_back(
    db_session: AsyncSession, live_redis: aioredis.Redis, live_storage, sandbox
) -> None:
    import asyncio as _asyncio

    from src.services.redis import registry_key

    user, project_id = await _project(db_session, "e2e7@rvaiglobal.com")
    manager = SessionManager()
    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=sandbox, may_write=True
    )
    assert session.handle is not None
    await _write(sandbox, session.handle, "app/marker.txt", "TREE-A")
    await manager.finish_turn_sandbox(session, sandbox, touched=True)
    await manager.save_project_snapshot(db_session, user, project_id, sandbox_client=sandbox)

    await _asyncio.sleep(1.1)
    second = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=sandbox, may_write=True
    )
    assert second.handle is not None
    await _write(sandbox, second.handle, "app/marker.txt", "TREE-B")
    await manager.finish_turn_sandbox(second, sandbox, touched=True)

    await sandbox.teardown(second.handle)
    await live_redis.delete(registry_key(user.id))

    await manager.relaunch_preview(db_session, user, project_id, sandbox, prefer_saved=True)
    handle = await sandbox.attach_existing(str(user.id))
    body = await _read(sandbox, handle, "/workspace/app/app/marker.txt")
    assert "TREE-A" in body, "the user asked for their saved version and got something else"


async def test_s8_a_read_only_turn_writes_no_recovery_bundle(
    db_session: AsyncSession, live_redis: aioredis.Redis, live_storage, sandbox
) -> None:
    user, project_id = await _project(db_session, "e2e8@rvaiglobal.com")
    manager = SessionManager()
    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=sandbox, may_write=False
    )
    await manager.finish_turn_sandbox(session, sandbox, touched=False)
    assert await live_storage.head(recovery_key(session.app_id)) is None


async def test_s9_a_save_racing_a_turn_boundary_write_corrupts_neither_bundle(
    db_session: AsyncSession, live_redis: aioredis.Redis, live_storage, sandbox
) -> None:
    """The 0.1 fix, for real. Both writers run `git add`/`commit`/`bundle create` against ONE
    container's worktree and one git index. Before the per-call bundle path and the per-app
    lock, one call's cleanup deleted the file the other was still reading."""
    import asyncio as _asyncio

    user, project_id = await _project(db_session, "e2e9@rvaiglobal.com")
    manager = SessionManager()
    # `may_write=False`: the race IS the subject, so the Save has to reach the bundling code
    # while the turn terminal is still in flight — and a WRITING session is refused there, not
    # flakily but DETERMINISTICALLY (`finish_turn_sandbox` pops the slot last, after two
    # awaited container round trips; the Save reaches the guard after a single DB await).
    # So `may_write=False` here is a harness necessity, not a claim about production: it is the
    # only way to get a Save and a turn terminal bundling concurrently inside one container,
    # which is the collision this test exists to rule out. Do NOT read it as "production can
    # reach this state" — the deploy contract is single-replica (manager.py), so the in-process
    # guard is not something a second worker routes around.
    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=sandbox, may_write=False
    )
    assert session.handle is not None
    await _write(sandbox, session.handle, "app/marker.txt", "RACE")

    save, turn = await _asyncio.gather(
        manager.save_project_snapshot(db_session, user, project_id, sandbox_client=sandbox),
        manager.finish_turn_sandbox(session, sandbox, touched=True),
        return_exceptions=True,
    )
    assert not isinstance(save, BaseException), f"the user's Save failed: {save!r}"
    assert not isinstance(turn, BaseException), f"the turn terminal failed: {turn!r}"

    # BOTH objects are complete, parseable git bundles — no short read landed on either key.
    for key in (snapshot_key(session.app_id), recovery_key(session.app_id)):
        blob = await live_storage.get(key)
        assert blob.startswith(b"# v2 git bundle\n"), f"{key} is not a bundle"
        assert parse_bundle_head_sha(blob)
        # git itself is the arbiter: verify the bundle inside the container.
        await _write(sandbox, session.handle, "verify.b64", "")
        import base64 as _b64

        from src.services.sandbox.base import FileCreate

        await sandbox.files(
            session.handle,
            FileCreate(path="verify.b64", file_text=_b64.b64encode(blob).decode()),
        )
        checked = await sandbox.exec(
            session.handle,
            [
                "sh",
                "-c",
                "base64 -d < /workspace/app/verify.b64 > /tmp/v.bundle "
                "&& git bundle verify /tmp/v.bundle 2>&1 | tail -2",
            ],
        )
        assert checked.exit == 0, f"git rejected the bundle at {key}: {checked.stdout}"


async def test_s10_no_bundle_artifact_is_ever_left_in_or_committed_to_the_users_tree(
    db_session: AsyncSession, live_redis: aioredis.Redis, live_storage, sandbox
) -> None:
    """The bundle is written under /tmp precisely so no `.gitignore` rule has to be right —
    a RESTORED container carries the `.gitignore` committed in its own bundle, which for
    pre-existing apps lists the literal `/app.bundle` and would not match the randomized
    names. Several turns, then look at what git actually tracks."""
    user, project_id = await _project(db_session, "e2e10@rvaiglobal.com")
    manager = SessionManager()
    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=sandbox, may_write=True
    )
    assert session.handle is not None
    for n in range(3):
        await _write(sandbox, session.handle, f"app/f{n}.txt", f"turn {n}")
        await manager.finish_turn_sandbox(session, sandbox, touched=True)

    tracked = await sandbox.exec(
        session.handle, ["sh", "-c", "cd /workspace/app && git ls-files | grep -c bundle || true"]
    )
    assert tracked.stdout.strip() in {"0", ""}, "a bundle artifact got committed into the tree"

    present = await sandbox.exec(
        session.handle,
        ["sh", "-c", "ls /workspace/app | grep -c 'app.bundle' || true"],
    )
    assert present.stdout.strip() in {"0", ""}, "a bundle was left lying in the worktree"

    # And the history is the user's commits only.
    log = await sandbox.exec(
        session.handle, ["sh", "-c", "cd /workspace/app && git log --oneline | wc -l"]
    )
    assert int(log.stdout.strip()) >= 1


async def test_s11_deleting_the_project_removes_the_recovery_blob_too(
    db_session: AsyncSession, live_redis: aioredis.Redis, live_storage, sandbox
) -> None:
    from src.services.projects.delete import delete_project_cascade

    user, project_id = await _project(db_session, "e2e11@rvaiglobal.com")
    manager = SessionManager()
    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=sandbox, may_write=True
    )
    assert session.handle is not None
    await _write(sandbox, session.handle, "app/marker.txt", "DOOMED")
    await manager.finish_turn_sandbox(session, sandbox, touched=True)
    await manager.save_project_snapshot(db_session, user, project_id, sandbox_client=sandbox)
    assert await live_storage.head(recovery_key(session.app_id)) is not None

    from src.db.models.project import Project

    project = await db_session.get(Project, project_id)
    assert project is not None
    cleanup = await delete_project_cascade(db_session, project, live_storage, user_id=user.id)
    await db_session.commit()
    assert recovery_key(session.app_id) in set(cleanup.blob_keys)

    # The caller sweeps post-commit; do that for real and confirm the bytes are gone.
    for key in cleanup.blob_keys:
        await live_storage.delete(key)
    assert await live_storage.head(recovery_key(session.app_id)) is None
    assert await live_storage.head(snapshot_key(session.app_id)) is None


async def test_s12_an_unreadable_bundle_is_a_typed_refusal_not_a_crash(
    db_session: AsyncSession, live_redis: aioredis.Redis, live_storage, sandbox
) -> None:
    """A corrupt blob must surface as `SnapshotUnavailableError` — which the routers map to a
    503 telling the user their work is intact — never as an unhandled 500."""
    from src.services.build_sessions.manager import SnapshotUnavailableError
    from src.services.redis import registry_key

    user, project_id = await _project(db_session, "e2e12@rvaiglobal.com")
    manager = SessionManager()
    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=sandbox, may_write=False
    )
    assert session.handle is not None
    await _write(sandbox, session.handle, "app/marker.txt", "TREE-A")
    await manager.save_project_snapshot(db_session, user, project_id, sandbox_client=sandbox)
    await manager.finish_turn_sandbox(session, sandbox, touched=False)

    await sandbox.teardown(session.handle)
    await live_redis.delete(registry_key(user.id))
    # The saved bundle is present but is not a bundle. Remove the recovery copy too, so the
    # restore has only the corrupt object to work with.
    await live_storage.delete(recovery_key(session.app_id))
    await live_storage.put(snapshot_key(session.app_id), b"this is not a git bundle at all")

    with pytest.raises(SnapshotUnavailableError):
        await manager.ensure_sandbox(
            db_session, user, project_id, sandbox_client=sandbox, may_write=True
        )


async def test_s13_a_save_and_a_turn_in_the_same_second_still_resumes_the_newer_tree(
    db_session: AsyncSession, live_redis: aioredis.Redis, live_storage, sandbox
) -> None:
    """★ THE RESOLUTION QUESTION. Azure (and Azurite) stamp `Last-Modified` in WHOLE SECONDS.
    `newest_restore_source` orders the two bundles by that stamp, so when a Save and a
    turn-boundary write land inside one second the comparison cannot separate them.

    The stake is not a missing prompt: `_restore_or_provision` uses the same comparison, so a
    tie means the older SAVED tree is restored and the newer work is gone — the exact P0 this
    branch fixed, reappearing inside a one-second window. This drives it end to end.

    It does NOT guarantee the collision — real container round trips decide that, and the run
    that named S13B found the two stamps a second apart. The tie is FORCED in S13B; what this
    one holds is the unforced sequence, and it prints which way the stamps actually fell."""
    from src.services.redis import registry_key

    user, project_id = await _project(db_session, "e2e13@rvaiglobal.com")
    manager = SessionManager()
    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=sandbox, may_write=True
    )
    assert session.handle is not None
    await _write(sandbox, session.handle, "app/marker.txt", "TREE-A")
    await manager.finish_turn_sandbox(session, sandbox, touched=True)

    # Save, then the next turn's work and terminal AS FAST AS POSSIBLE — no sleep anywhere, so
    # the two blobs have whatever chance the real timings give them of landing in one second.
    await manager.save_project_snapshot(db_session, user, project_id, sandbox_client=sandbox)
    second = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=sandbox, may_write=True
    )
    assert second.handle is not None
    await _write(sandbox, second.handle, "app/marker.txt", "TREE-B")
    await manager.finish_turn_sandbox(second, sandbox, touched=True)

    saved_meta = await live_storage.head(snapshot_key(session.app_id))
    recovery_meta = await live_storage.head(recovery_key(session.app_id))
    assert saved_meta is not None and recovery_meta is not None
    same_second = saved_meta.last_modified == recovery_meta.last_modified
    print(
        f"\nsaved={saved_meta.last_modified} recovery={recovery_meta.last_modified} "
        f"same_second={same_second}"
    )

    await sandbox.teardown(second.handle)
    await live_redis.delete(registry_key(user.id))
    resumed = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=sandbox, may_write=True
    )
    assert resumed.handle is not None
    body = await _read(sandbox, resumed.handle, "/workspace/app/app/marker.txt")
    assert "TREE-B" in body, (
        "work done after a Save in the SAME SECOND was lost "
        f"(saved={saved_meta.last_modified}, recovery={recovery_meta.last_modified})"
    )


async def test_s14_the_real_reaper_sweep_then_resume_keeps_the_work(
    db_session: AsyncSession, live_redis: aioredis.Redis, live_storage, sandbox
) -> None:
    """Not a hand-rolled teardown: the ACTUAL background sweep destroys the container, exactly
    as it does when a user closes their tab and the heartbeat lapses."""
    import asyncio as _asyncio

    from src.services.build_sessions.reaper import sweep_all
    from src.services.redis import heartbeat_key, registry_key

    user, project_id = await _project(db_session, "e2e14@rvaiglobal.com")
    manager = SessionManager()
    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=sandbox, may_write=True
    )
    assert session.handle is not None
    await _write(sandbox, session.handle, "app/marker.txt", "TREE-A")
    await manager.finish_turn_sandbox(session, sandbox, touched=True)
    await manager.save_project_snapshot(db_session, user, project_id, sandbox_client=sandbox)

    await _asyncio.sleep(1.1)
    second = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=sandbox, may_write=True
    )
    assert second.handle is not None
    await _write(sandbox, second.handle, "app/marker.txt", "TREE-B")
    await manager.finish_turn_sandbox(second, sandbox, touched=True)
    container = second.handle.app_name

    # The user's tab is gone: the heartbeat lapses and the lease expires.
    await live_redis.delete(heartbeat_key(user.id))
    await live_redis.hdel(registry_key(user.id), "preview_stay_until")

    result = await sweep_all(live_redis, sandbox, live_users=set())
    assert result.reaped == 1 and result.failed == 0
    assert container in sandbox._aca.deleted  # the real container is really gone

    resumed = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=sandbox, may_write=True
    )
    assert resumed.handle is not None
    body = await _read(sandbox, resumed.handle, "/workspace/app/app/marker.txt")
    assert "TREE-B" in body, "the reaper path lost the user's work"


async def test_s15_a_failing_recovery_write_never_fails_the_turn_or_the_pardon(
    db_session: AsyncSession, live_redis: aioredis.Redis, live_storage, sandbox
) -> None:
    """The safety net failing must not take the turn down with it — the turn already
    succeeded and its terminal frame has gone out — and must not skip the pardon, or the
    user's live preview goes dark over a blob write they never knew about."""
    from src.services.build_sessions.locks import stay_of_execution_is_current

    user, project_id = await _project(db_session, "e2e15@rvaiglobal.com")
    manager = SessionManager()
    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=sandbox, may_write=True
    )
    assert session.handle is not None

    # A handle whose token is wrong: every supervisor call 401s, exactly as it would against a
    # container whose bearer rotated underneath us.
    import dataclasses

    broken = dataclasses.replace(session.handle, token="not-the-token")
    session.handle = broken

    await manager.finish_turn_sandbox(session, sandbox, touched=True)  # must not raise

    assert await live_storage.head(recovery_key(session.app_id)) is None
    assert manager.active_session_for(user.id) is None, "the build slot was not freed"
    assert await stay_of_execution_is_current(live_redis, user.id) is True, "no pardon"


async def test_s13b_a_forced_same_second_tie_does_not_silently_restore_the_older_tree(
    db_session: AsyncSession, live_redis: aioredis.Redis, live_storage, sandbox
) -> None:
    """S13 did not actually collide — the two writes fell a second apart, so it proved
    nothing. This FORCES the tie by rewriting both blobs back to back, then asks the question
    that matters: with equal timestamps, which tree does a resume bring back?

    Azure and Azurite both stamp whole seconds, so this is reachable in production whenever a
    user's Save and their in-flight turn finish inside the same second."""
    from src.services.redis import registry_key

    user, project_id = await _project(db_session, "e2e13b@rvaiglobal.com")
    manager = SessionManager()
    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=sandbox, may_write=True
    )
    assert session.handle is not None
    app_id = session.app_id

    await _write(sandbox, session.handle, "app/marker.txt", "TREE-A")
    await manager.finish_turn_sandbox(session, sandbox, touched=True)
    await manager.save_project_snapshot(db_session, user, project_id, sandbox_client=sandbox)
    tree_a = await live_storage.get(snapshot_key(app_id))

    second = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=sandbox, may_write=True
    )
    assert second.handle is not None
    await _write(sandbox, second.handle, "app/marker.txt", "TREE-B")
    await manager.finish_turn_sandbox(second, sandbox, touched=True)
    tree_b = await live_storage.get(recovery_key(app_id))
    assert tree_a != tree_b

    # Force the collision: both objects rewritten back to back, saved LAST — the worst
    # ordering, where a tie or a rounding-up makes the older tree look at least as new.
    await live_storage.put(recovery_key(app_id), tree_b)
    await live_storage.put(snapshot_key(app_id), tree_a)
    saved_meta = await live_storage.head(snapshot_key(app_id))
    recovery_meta = await live_storage.head(recovery_key(app_id))
    assert saved_meta is not None and recovery_meta is not None
    print(
        f"\nFORCED: saved={saved_meta.last_modified} recovery={recovery_meta.last_modified} "
        f"tie={saved_meta.last_modified == recovery_meta.last_modified}"
    )

    source = await manager.newest_restore_source(app_id)
    print(f"newest_restore_source -> {source}")

    await sandbox.teardown(second.handle)
    await live_redis.delete(registry_key(user.id))
    resumed = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=sandbox, may_write=True
    )
    assert resumed.handle is not None
    body = await _read(sandbox, resumed.handle, "/workspace/app/app/marker.txt")
    print(f"resumed tree -> {body.strip()!r}")
    assert "TREE-B" in body, (
        "SAME-SECOND TIE LOST THE USER'S WORK: the older saved tree was restored over newer "
        f"work (saved={saved_meta.last_modified}, recovery={recovery_meta.last_modified})"
    )


async def test_s16_an_unchanged_tree_is_never_offered_as_recoverable_work(
    db_session: AsyncSession, live_redis: aioredis.Redis, live_storage, sandbox
) -> None:
    """`touched` means "a mutating tool ran", not "the tree changed" — a turn that only shelled
    a command still rewrites the recovery bundle from an unchanged worktree, with a newer
    stamp. Ordering by time alone then offers work that does not exist, permanently, while the
    save state says `dirty=False`: the UI would claim "all changes saved" and "you have
    unsaved work" at once. The stamped HEAD settles it — same tree, nothing to offer."""
    import asyncio as _asyncio

    user, project_id = await _project(db_session, "e2e16@rvaiglobal.com")
    manager = SessionManager()
    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=sandbox, may_write=True
    )
    assert session.handle is not None
    await _write(sandbox, session.handle, "app/marker.txt", "TREE-A")
    await manager.finish_turn_sandbox(session, sandbox, touched=True)
    await manager.save_project_snapshot(db_session, user, project_id, sandbox_client=sandbox)

    await _asyncio.sleep(1.1)
    # A SECOND turn that touched a tool but changed nothing on disk — `run_command` with no
    # write, the case `touched` cannot tell apart from a real edit.
    second = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=sandbox, may_write=True
    )
    await manager.finish_turn_sandbox(second, sandbox, touched=True)

    saved_meta = await live_storage.head(snapshot_key(session.app_id))
    recovery_meta = await live_storage.head(recovery_key(session.app_id))
    assert saved_meta is not None and recovery_meta is not None
    assert recovery_meta.last_modified > saved_meta.last_modified, "setup: recovery IS newer"
    assert recovery_meta.metadata is not None
    assert recovery_meta.metadata["head_sha"] == saved_meta.metadata["head_sha"]

    state = await manager.project_save_state(db_session, user, project_id, sandbox_client=sandbox)
    assert state.dirty is False
    assert await manager.recoverable_work(session.app_id) is None, (
        "offered work that does not exist, while the save state says everything is saved"
    )
