"""What a deletion has to say for itself, and the tombstone that keeps it (#158 §13).

The body of `DELETE /v1/projects/{id}` carries exactly ONE field, the reason, 5 to 50 words.
The deletion also records WHO, but the route stamps that from the session rather than
accepting it — so the tests for it are not validation tests at all, they are tests that the
client CANNOT influence it. Both live here because they are one request.

Two things are being pinned, and they are separate claims:

1.  **The server refuses independently of the client.** §13.2 asks for validation on both
    sides, and names the rename path as the shape NOT to repeat — there, the client had no
    check at all and the server's raw validator string reached the screen. So these tests
    drive the API directly, with no browser in the picture, and assert both the refusal and
    that its wording is written for a person.

2.  **The record survives what it describes.** `delete_project` force-drops the project's
    database and deletes every child row; the tombstone is written in the same transaction
    and has to still be readable afterwards, holding values rather than references to rows
    that no longer exist.

The word rule is the shared one (`src/core/words.py`), so the boundaries tested here are the
same boundaries `portal/src/utils/words.ts` enforces in the browser.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from src.db.models.deleted_project import (
    MAX_DELETE_REMARK_WORDS,
    MIN_DELETE_REMARK_WORDS,
    DeletedProject,
)
from tests.api.v1.projects.test_projects_crud import _auth
from tests.factories import ProjectFactory

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
        (MIN_DELETE_REMARK_WORDS - 1, "at least 5 words"),
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

    # ...and the project really is gone. A tombstone beside a surviving row would be a
    # soft delete, which is the thing §13.3 argues against.
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
    """The readable name on the tombstone is the ACCOUNT's, not anything sent."""
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
    """THE TEST THIS FIELD EXISTS FOR.

    `deletedByName` was briefly a required body field, and a browser session signed in as
    one person could record a deletion under another person's name — which is exactly the
    question an administrator reads this row to answer. The route now stamps it from the
    session and Pydantic drops the unknown key, so sending one changes nothing.

    If someone ever reintroduces the field, this fails.
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
