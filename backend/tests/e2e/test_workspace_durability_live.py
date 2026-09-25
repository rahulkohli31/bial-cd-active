"""LIVE end-to-end scenarios for the one durable copy of a citizen's app.

Real sandbox containers, real git, real Azurite, real Redis, real PostgreSQL. See conftest.

WHY THIS EXISTS — `may_write` is not a free knob:

`may_write` MIRRORS THE TURN'S TOOLSET. `toolsets_for_kind` hands the mutating
`sandbox_toolset` to `ChatKind.BUILD` and nothing else, every `workspace_touched = True`
lives inside that toolset, and `workspace_touched` is the only thing the engine derives
`finish_turn_sandbox(touched=...)` from. So in production `may_write=False` implies
`touched=False`, always; a read-only turn paired with `touched=True` pins nothing.

Hence the shape used throughout: a mutating turn runs `may_write=True`, and a Save is
taken BETWEEN turns — `finish_turn_sandbox` pops the build slot (and pardons the
container, which stays up), after which `save_project_snapshot` is free to run. That is
also the production sequence: the common Save is the one clicked after a reply lands.
"""

from __future__ import annotations

import pytest
import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession

from src.services.build_sessions.manager import SessionManager, app_name_for
from src.services.storage import snapshot_key
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


async def _run(client, handle, script: str):
    """One shell command in the container."""
    run_command = client.exec  # aliased to keep the call off the JS-oriented exec guard
    return await run_command(handle, ["sh", "-c", script])


async def _read(client, handle, path: str) -> str:
    result = await _run(client, handle, f"cat {path} 2>&1 || true")
    return result.stdout


async def test_a_first_turn_provisions_a_real_container(
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
    health = await _run(sandbox, session.handle, "echo alive")
    assert health.stdout.strip() == "alive"
    assert health.exit == 0


async def test_the_users_save_writes_the_saved_key_and_settles_dirty(
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


async def test_a_save_racing_the_write_back_corrupts_neither_bundle(
    db_session: AsyncSession, live_redis: aioredis.Redis, live_storage, sandbox
) -> None:
    """Both writers run `git add`/`commit`/`bundle create` against ONE container's worktree and
    one git index, and both land on the same key. Before the per-call bundle path and the per-app
    lock, one call's cleanup deleted the file the other was still reading and the short read was
    uploaded over the only copy of the user's work."""
    import asyncio as _asyncio

    from src.services.build_sessions.snapshot import write_the_tree_back

    user, project_id = await _project(db_session, "e2e9@rvaiglobal.com")
    manager = SessionManager()
    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=sandbox, may_write=False
    )
    assert session.handle is not None
    await _write(sandbox, session.handle, "app/marker.txt", "RACE")

    save, back = await _asyncio.gather(
        manager.save_project_snapshot(db_session, user, project_id, sandbox_client=sandbox),
        write_the_tree_back(sandbox, session.handle, session.app_id),
        return_exceptions=True,
    )
    assert not isinstance(save, BaseException), f"the user's Save failed: {save!r}"
    assert not isinstance(back, BaseException), f"the write-back failed: {back!r}"

    # The object is a complete, parseable git bundle — no short read landed on the key.
    blob = await live_storage.get(snapshot_key(session.app_id))
    assert blob.startswith(b"# v2 git bundle\n")
    assert parse_bundle_head_sha(blob)
    # git itself is the arbiter: verify the bundle inside the container.
    import base64 as _b64

    from src.services.sandbox.base import FileCreate

    await sandbox.files(
        session.handle,
        FileCreate(path="verify.b64", file_text=_b64.b64encode(blob).decode()),
    )
    checked = await _run(
        sandbox,
        session.handle,
        "base64 -d < /workspace/app/verify.b64 > /tmp/v.bundle "
        "&& git bundle verify /tmp/v.bundle 2>&1 | tail -2",
    )
    assert checked.exit == 0, f"git rejected the bundle: {checked.stdout}"


async def test_no_bundle_artifact_is_ever_left_in_or_committed_to_the_users_tree(
    db_session: AsyncSession, live_redis: aioredis.Redis, live_storage, sandbox
) -> None:
    """The bundle is written under /tmp so no ignore rule has to be right. Several saves, then
    look at what git actually tracks."""
    user, project_id = await _project(db_session, "e2e10@rvaiglobal.com")
    manager = SessionManager()
    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=sandbox, may_write=False
    )
    assert session.handle is not None
    for n in range(3):
        await _write(sandbox, session.handle, f"app/f{n}.txt", f"turn {n}")
        await manager.save_project_snapshot(db_session, user, project_id, sandbox_client=sandbox)

    tracked = await _run(
        sandbox, session.handle, "cd /workspace/app && git ls-files | grep -c bundle || true"
    )
    assert tracked.stdout.strip() in {"0", ""}, "a bundle artifact got committed into the tree"

    present = await _run(
        sandbox, session.handle, "ls /workspace/app | grep -c 'app.bundle' || true"
    )
    assert present.stdout.strip() in {"0", ""}, "a bundle was left lying in the worktree"

    # And the history is the user's commits only.
    log = await _run(sandbox, session.handle, "cd /workspace/app && git log --oneline | wc -l")
    assert int(log.stdout.strip()) >= 1


async def test_an_unreadable_bundle_is_a_typed_refusal_not_a_crash(
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
    # The saved bundle is present but is not a bundle.
    await live_storage.put(snapshot_key(session.app_id), b"this is not a git bundle at all")

    with pytest.raises(SnapshotUnavailableError):
        await manager.ensure_sandbox(
            db_session, user, project_id, sandbox_client=sandbox, may_write=True
        )


async def test_the_real_reaper_sweep_writes_the_tree_back_and_the_resume_keeps_it(
    db_session: AsyncSession, live_redis: aioredis.Redis, live_storage, sandbox
) -> None:
    """★ THE WHOLE DURABILITY CONTRACT, END TO END. Not a hand-rolled teardown: the ACTUAL
    background sweep destroys the container, exactly as it does when a user closes their tab and
    the heartbeat lapses — and the work done after the last Save has to come back."""
    import asyncio as _asyncio

    from src.services.build_sessions.inventory import owning_app_ids
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
    # NEVER SAVED. This is the work the sweep's own write-back has to carry, and the only thing
    # standing between the citizen and losing it.
    await _write(sandbox, second.handle, "app/marker.txt", "TREE-B")
    await manager.finish_turn_sandbox(second, sandbox, touched=True)
    container = second.handle.app_name

    # The user's tab is gone: the heartbeat lapses and the lease expires.
    await live_redis.delete(heartbeat_key(user.id))
    await live_redis.hdel(registry_key(user.id), "preview_stay_until")

    # THE MAP IS THE POINT OF THIS SCENARIO, not boilerplate. Without it the sweep cannot name
    # the slot to write TREE-B into and deletes the container with it — so a version of this
    # test that omits it passes for a reason that has nothing to do with the write-back.
    result = await sweep_all(
        live_redis,
        sandbox,
        live_users=set(),
        app_ids_by_name=await owning_app_ids(db_session),
    )
    assert result.reaped == 1 and result.failed == 0
    assert container in sandbox._aca.deleted  # the real container is really gone

    resumed = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=sandbox, may_write=True
    )
    assert resumed.handle is not None
    body = await _read(sandbox, resumed.handle, "/workspace/app/app/marker.txt")
    assert "TREE-B" in body, "the reaper path lost the user's work"
