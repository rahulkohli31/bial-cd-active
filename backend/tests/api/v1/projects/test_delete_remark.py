"""What a deletion has to say for itself, and the tombstone that keeps it.

`DELETE /v1/projects/{id}` takes one body field, `remark` (2-50 words); who deleted it is
stamped from the session, never accepted from the client, so those tests are isolation
tests, not validation ones. Two claims are pinned: the server refuses a bad remark on its
own (the rename path — no server-side check — is the shape not to repeat), and
the tombstone survives what it describes, written in the same transaction that drops the
project, holding values rather than references to rows already gone.

The word rule is shared (`src/core/words.py`), so these boundaries match what
`portal/src/utils/words.ts` enforces in the browser.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from src.db.models.deleted_project import (
    MAX_DELETE_REMARK_WORDS,
    MIN_DELETE_REMARK_WORDS,
    DeletedProject,
)
from tests.api.v1.projects.test_projects_crud import _auth
from tests.factories import (
    AppRegistryFactory,
    ConversationFactory,
    ProjectFactory,
    UserFactory,
)

_PROJECTS = "/v1/projects"


def _words(n: int) -> str:
    return " ".join(f"w{i}" for i in range(n))


async def _project(db, user_id, name: str = "Visitor Log"):
    project = await ProjectFactory.create(db, user_id, name=name)
    await db.commit()
    return project


async def _delete_body(client, project_id, headers, body: dict[str, str]):
    """The raw request, for the cases that are ABOUT the body's shape."""
    return await client.request("DELETE", f"{_PROJECTS}/{project_id}", headers=headers, json=body)


async def _delete(client, project_id, headers, remark: str):
    return await _delete_body(client, project_id, headers, {"remark": remark})


# --- the rule ------------------------------------------------------------------


@pytest.mark.parametrize("n", [MIN_DELETE_REMARK_WORDS, 20, MAX_DELETE_REMARK_WORDS])
async def test_a_reason_inside_the_bounds_is_accepted(client, db_session, n: int) -> None:
    headers, user = await _auth(db_session)
    project = await _project(db_session, user.id)

    resp = await _delete(client, project.id, headers, _words(n))

    assert resp.status_code == 200, resp.text


@pytest.mark.parametrize(
    ("n", "expected"),
    [
        (MIN_DELETE_REMARK_WORDS - 1, "at least 2 words"),
        (MAX_DELETE_REMARK_WORDS + 1, "under 50 words"),
    ],
)
async def test_a_reason_outside_the_bounds_is_refused(client, db_session, n, expected) -> None:
    """Both bounds, and the copy a person actually reads.

    The lower bound is the unusual one and it is deliberate: a required field that "no"
    satisfies is a required field that will always say "no". The remark exists so an
    administrator reading this months later learns something.
    """
    headers, user = await _auth(db_session)
    project = await _project(db_session, user.id)

    resp = await _delete(client, project.id, headers, _words(n))

    assert resp.status_code == 422, resp.text
    assert expected in resp.text
    # Written for a person, not Pydantic's own words.
    assert "Value error" not in resp.text


async def test_an_empty_reason_is_refused(client, db_session) -> None:
    headers, user = await _auth(db_session)
    project = await _project(db_session, user.id)

    resp = await _delete(client, project.id, headers, "   ")

    assert resp.status_code == 422, resp.text
    assert "Say why" in resp.text


async def test_a_refused_reason_deletes_nothing(client, db_session) -> None:
    """The refusal must not be a partial delete.

    Validation happens at the Pydantic boundary, before the route body runs at all, so this
    holds by construction — but "by construction" is exactly the kind of claim that stops
    being true when someone moves a check.
    """
    headers, user = await _auth(db_session)
    project = await _project(db_session, user.id)

    assert (await _delete(client, project.id, headers, "no")).status_code == 422

    still_there = await client.get(f"{_PROJECTS}/{project.id}", headers=headers)
    assert still_there.status_code == 200


async def test_whitespace_runs_count_as_one_separator(client, db_session) -> None:
    """Five words however they are spaced — the same splitting the browser uses.

    A reason a citizen typed with a double space or a newline must not be refused for
    reaching the API differently than it looked on screen.
    """
    headers, user = await _auth(db_session)
    project = await _project(db_session, user.id)

    resp = await _delete(client, project.id, headers, "  no\tlonger\nneeded  by   anyone  ")

    assert resp.status_code == 200, resp.text


# --- the tombstone -------------------------------------------------------------


async def test_the_tombstone_outlives_the_project(client, db_session) -> None:
    """It holds VALUES, not references — the project row is gone."""
    headers, user = await _auth(db_session)
    project = await _project(db_session, user.id, name="Visitor Log")
    remark = "Superseded by the new gate pass tool"

    assert (await _delete(client, project.id, headers, remark)).status_code == 200

    row = (
        await db_session.execute(
            sa.select(DeletedProject).where(DeletedProject.project_id == project.id)
        )
    ).scalar_one()

    assert row.project_name == "Visitor Log"  # readable without the project row
    assert row.owner_id == user.id
    assert row.owner_email == user.email
    assert row.deleted_by == user.id
    assert row.deleted_by_name == (user.display_name or user.email)
    assert row.remark == remark
    assert row.deleted_at is not None

    # ...and the project really is gone. A tombstone beside a surviving row would be a soft
    # delete: the record exists to say a deletion HAPPENED, not to stand in for one.
    assert (await client.get(f"{_PROJECTS}/{project.id}", headers=headers)).status_code == 404


async def test_no_tombstone_is_written_when_the_delete_is_refused(client, db_session) -> None:
    """A record of a deletion that did not happen is worse than no record.

    The insert is inside the caller's transaction for this reason: a rolled-back or refused
    delete must leave nothing behind.
    """
    headers, user = await _auth(db_session)
    project = await _project(db_session, user.id)

    await _delete(client, project.id, headers, "no")  # 422

    count = await db_session.scalar(
        sa.select(sa.func.count())
        .select_from(DeletedProject)
        .where(DeletedProject.project_id == project.id)
    )
    assert count == 0


async def test_the_tombstone_records_what_went_with_it(client, db_session) -> None:
    """The counts cannot be reconstructed once the children are deleted, which is why they
    are captured at the moment of deletion rather than derived later."""
    headers, user = await _auth(db_session)
    project = await _project(db_session, user.id)

    assert (await _delete(client, project.id, headers, _words(6))).status_code == 200

    row = (
        await db_session.execute(
            sa.select(DeletedProject).where(DeletedProject.project_id == project.id)
        )
    ).scalar_one()

    # A bare project: no app, no per-app database, no chats. The point is that each field is
    # ANSWERED rather than left null — "we did not record it" and "there were none" are
    # different things to an administrator reading this later.
    assert row.chats_deleted == 0
    assert row.had_app is False
    assert row.had_database is False


# --- who the row says did it ----------------------------------------------------


async def test_the_deleter_is_stamped_from_the_session(client, db_session) -> None:
    headers, user = await _auth(db_session)
    user.display_name = "Asha Rao"
    await db_session.commit()
    project = await _project(db_session, user.id)

    assert (await _delete(client, project.id, headers, _words(6))).status_code == 200

    row = (
        await db_session.execute(
            sa.select(DeletedProject).where(DeletedProject.project_id == project.id)
        )
    ).scalar_one()
    assert row.deleted_by_name == "Asha Rao"
    assert row.deleted_by == user.id


async def test_the_body_cannot_choose_who_deleted_it(client, db_session) -> None:
    """THE TEST THIS FIELD EXISTS FOR: `deletedByName` was briefly a client-supplied field,
    letting one session record a deletion under another person's name. The route stamps it
    from the session now and drops the unknown key — if someone ever reintroduces the field
    as writable, this fails.
    """
    headers, user = await _auth(db_session)
    user.display_name = "Asha Rao"
    await db_session.commit()
    project = await _project(db_session, user.id)

    resp = await _delete_body(
        client,
        project.id,
        headers,
        {"remark": _words(6), "deletedByName": "Someone Else Entirely"},
    )
    assert resp.status_code == 200, resp.text

    row = (
        await db_session.execute(
            sa.select(DeletedProject).where(DeletedProject.project_id == project.id)
        )
    ).scalar_one()
    assert row.deleted_by_name == "Asha Rao"  # the session, not the body
    assert row.deleted_by == user.id


async def test_the_email_stands_in_when_entra_gave_no_display_name(client, db_session) -> None:
    """`display_name` is nullable and really is null for some accounts.

    The alternative to a fallback is a NOT NULL violation on the insert — a delete that
    500s for people whose Entra profile happens to lack a display name — or a blank in the
    one human-readable field on the row. The email identifies the account just as well.
    """
    headers, user = await _auth(db_session)
    user.display_name = None
    await db_session.commit()
    project = await _project(db_session, user.id)

    assert (await _delete(client, project.id, headers, _words(6))).status_code == 200

    row = (
        await db_session.execute(
            sa.select(DeletedProject).where(DeletedProject.project_id == project.id)
        )
    ).scalar_one()
    assert row.deleted_by_name == user.email
    assert row.deleted_by_name  # not blank, which is the failure this guards


# --- one tombstone per physical deletion ----------------------------------------


async def test_a_second_tombstone_for_the_same_project_is_refused(client, db_session) -> None:
    """THE DATABASE IS THE GUARD: `owned_project_or_404` takes no row lock and the cascade
    deletes through Core `sa.delete()`, so a double-click or a proxy retry can run the whole
    delete twice — two tombstones for one physical deletion. Truly overlapping requests are
    NOT stageable here (this fixture serialises everything on one `db_session`), so this
    drives the unique constraint directly instead. `test_migration_0036_deleted_projects.py`
    pins the index's existence; this pins its effect.
    """
    headers, user = await _auth(db_session)
    project = await _project(db_session, user.id)
    project_id = project.id

    assert (await _delete(client, project_id, headers, _words(6))).status_code == 200

    # The same project, deleted a second time: exactly what the retry would write.
    # A SAVEPOINT, not a bare flush: the fixture wraps each test in an outer transaction it
    # rolls back afterwards, and rolling back the session by hand deassociates that
    # transaction and makes the teardown noisy. `begin_nested` unwinds only this insert.
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(
                DeletedProject(
                    project_id=project_id,
                    project_name="Visitor Log",
                    owner_id=user.id,
                    owner_email=user.email,
                    deleted_by=user.id,
                    deleted_by_name="E2E Dev User",
                    remark=_words(6),
                )
            )


async def test_the_loser_gets_a_404_through_the_route_not_a_500(client, db_session) -> None:
    """THE ROUTE'S OWN HANDLING through HTTP — companion to the test above. Seeds the
    winner's tombstone directly, then a real DELETE hits the same unique constraint
    mid-cascade (at the autoflush, not the final commit), where a real race's loser would
    land; before this fix that IntegrityError reached the unhandled-exception handler
    uncaught as a 500. Row state is NOT asserted after, deliberately: the route's own
    rollback on a real constraint violation would erase this test's seed data too — the
    companion above already pins that guarantee.
    """
    headers, user = await _auth(db_session)
    project = await _project(db_session, user.id)
    project_id = project.id

    # The winner's row, as if this project had already been tombstoned by an earlier,
    # since-committed request.
    db_session.add(
        DeletedProject(
            project_id=project_id,
            project_name=project.name,
            owner_id=user.id,
            owner_email=user.email,
            deleted_by=user.id,
            deleted_by_name="Someone Else",
            remark=_words(6),
        )
    )
    await db_session.commit()

    resp = await _delete(client, project_id, headers, _words(6))

    assert resp.status_code == 404, resp.text
    assert "Value error" not in resp.text  # written for a person, not a stack trace
    assert "Internal server error" not in resp.text  # the bug: this used to be a 500


async def test_the_tombstone_records_what_actually_went_with_it(client, db_session) -> None:
    """The counts, asserted NON-ZERO — which is the only version of this test that means
    anything.

    They were previously only ever asserted at `0`/`False` on a bare project, so an
    implementation hard-coding all three survived: `chats_deleted=0, had_app=False,
    had_database=False` passed. These three are the only facts on the row that cannot be
    reconstructed once the children are gone, so they are the ones worth pinning hardest.
    """
    headers, user = await _auth(db_session)
    project = await _project(db_session, user.id)
    await AppRegistryFactory.create(db_session, project_id=project.id, user_id=user.id)
    for _ in range(3):
        await ConversationFactory.create(db_session, user.id, project_id=project.id)
    await db_session.commit()
    project_id = project.id

    assert (await _delete(client, project_id, headers, _words(6))).status_code == 200

    row = (
        await db_session.execute(
            sa.select(DeletedProject).where(DeletedProject.project_id == project_id)
        )
    ).scalar_one()

    assert row.chats_deleted == 3
    assert row.had_app is True


async def test_chats_are_counted_by_the_same_predicate_the_cascade_deletes_by(
    client, db_session
) -> None:
    """`chats_deleted` must describe the rows that actually went.

    The count read `project_id` alone while `delete_project_cascade` enumerates on
    `project_id` AND `user_id`. Not exploitable — ownership is checked before either runs —
    but it made the recorded number and the deleted set two different things by
    construction, on a table whose whole job is to be accurate about what went.
    """
    headers, user = await _auth(db_session)
    other = await UserFactory.create(db_session)
    project = await _project(db_session, user.id)
    await ConversationFactory.create(db_session, user.id, project_id=project.id)
    # A stray row under the same project id owned by somebody else. The cascade will not
    # delete it, so the tombstone must not count it either.
    await ConversationFactory.create(db_session, other.id, project_id=project.id)
    await db_session.commit()
    project_id = project.id

    assert (await _delete(client, project_id, headers, _words(6))).status_code == 200

    row = (
        await db_session.execute(
            sa.select(DeletedProject).where(DeletedProject.project_id == project_id)
        )
    ).scalar_one()
    assert row.chats_deleted == 1
