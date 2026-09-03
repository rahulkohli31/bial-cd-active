"""The reader `deleted_projects` did not have (#176).

#158 §13.2 makes a 5-50 word reason MANDATORY before a project can be deleted, and that issue
landed the tombstone which keeps it — but nothing read the table. Collecting a justification
from every citizen that no one can retrieve is a promise broken rather than a feature missing,
and that is what these tests really pin: the reason a person wrote is reachable, by the right
people, and reaching it is recorded.

Three properties get the most attention because they are the ones that would fail quietly:

  * the RBAC gate from BOTH sides — a citizen refused and an admin admitted. Asserting only the
    refusal passes just as well against a route that refuses everybody.
  * the keyset walk across a page boundary, which is the guarantee keyset exists to give and
    the one thing an offset mistake breaks only at the seam.
  * the audit row, including what is NOT in it — logging the returned rows would grow the audit
    table a second copy of the table it audits.
"""

from __future__ import annotations

import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.db.models.audit import AuditLog
from src.db.models.deleted_project import DeletedProject
from src.db.models.user import User
from src.main import create_app
from src.services.auth.session_jwt import mint_session_jwt
from tests.factories import UserFactory

_TTL = settings.auth.access_ttl_seconds
_URL = "/v1/admin/deleted-projects"


def _cookie(jwt: str) -> dict[str, str]:
    return {"Cookie": f"session={jwt}"}


async def _admin(db: AsyncSession) -> dict[str, str]:
    user = await UserFactory.create(db, email="admin@bial.com")
    return _cookie(mint_session_jwt(user.id, user.token_version, _TTL))


async def _citizen(db: AsyncSession) -> dict[str, str]:
    user = await UserFactory.create(db, email="asha@bial.example")
    return _cookie(mint_session_jwt(user.id, user.token_version, _TTL))


async def _tombstone(
    db: AsyncSession, *, owner: User | None = None, **over: Any
) -> DeletedProject:
    """One tombstone, in the shape `delete_project` writes.

    `owner` is its own keyword rather than an `over` key so it stays typed as a `User` — the
    row needs `.id` and `.email` off it, which a `**kwargs: Any` bag cannot promise.
    """
    if owner is None:
        owner = await UserFactory.create(db)
    row = DeletedProject(
        project_id=over.pop("project_id", None) or uuid.uuid4(),
        project_name=over.pop("project_name", "Visitor Log"),
        owner_id=owner.id,
        owner_email=over.pop("owner_email", owner.email),
        deleted_by=over.pop("deleted_by", owner.id),
        deleted_by_name=over.pop("deleted_by_name", "Asha Rao"),
        remark=over.pop("remark", "No longer needed by the ground operations team"),
        chats_deleted=over.pop("chats_deleted", 0),
        had_app=over.pop("had_app", False),
        had_database=over.pop("had_database", False),
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return row


async def _list(client, headers, **params):
    resp = await client.get(_URL, headers=headers, params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _latest_audit(db: AsyncSession) -> AuditLog | None:
    return (
        (
            await db.execute(
                sa.select(AuditLog)
                .where(AuditLog.action == "admin:deletions:list")
                .order_by(AuditLog.id.desc())
            )
        )
        .scalars()
        .first()
    )


# --- the gate, from both sides ----------------------------------------------------


def test_the_route_documents_its_refusals_in_openapi() -> None:
    responses = create_app().openapi()["paths"][_URL]["get"]["responses"]
    assert {"401", "403", "422", "500"} <= set(responses)


async def test_an_unauthenticated_caller_is_refused(client) -> None:
    assert (await client.get(_URL)).status_code == 401


async def test_a_citizen_is_refused(client, db_session) -> None:
    """Fail-closed: anyone not on the allowlist is a citizen (AE1)."""
    await _tombstone(db_session)
    await db_session.commit()

    resp = await client.get(_URL, headers=await _citizen(db_session))

    assert resp.status_code == 403


async def test_an_admin_is_admitted(client, db_session) -> None:
    """The other half. Without it, a route refusing EVERYBODY passes the test above."""
    await _tombstone(db_session)
    await db_session.commit()

    body = await _list(client, await _admin(db_session))

    assert len(body["deletions"]) == 1


# --- what a row says --------------------------------------------------------------


async def test_the_row_carries_the_reason_and_what_went_with_it(client, db_session) -> None:
    await _tombstone(
        db_session,
        project_name="Visitor Gate Pass Tracker",
        remark="Superseded by the new gate pass tool",
        chats_deleted=7,
        had_app=True,
        had_database=True,
    )
    await db_session.commit()

    row = (await _list(client, await _admin(db_session)))["deletions"][0]

    assert row["projectName"] == "Visitor Gate Pass Tracker"
    assert row["remark"] == "Superseded by the new gate pass tool"
    # The three facts that cannot be reconstructed once the children are gone.
    assert row["chatsDeleted"] == 7
    assert row["hadApp"] is True
    assert row["hadDatabase"] is True


async def test_who_acted_and_the_label_for_them_are_separate_fields(client, db_session) -> None:
    """`deletedBy` is the account; `deletedByName` is its readable label.

    Both are stamped server-side so they cannot disagree, but they are different KINDS of fact
    and this screen is where that matters. `deletedByName` was briefly client-supplied, which
    let a browser signed in as one person file a deletion under another person's name.
    """
    owner = await UserFactory.create(db_session, email="asha@bial.example")
    await _tombstone(db_session, owner=owner, deleted_by_name="Asha Rao")
    await db_session.commit()

    row = (await _list(client, await _admin(db_session)))["deletions"][0]

    assert row["deletedBy"] == str(owner.id)
    assert row["deletedByName"] == "Asha Rao"
    assert row["ownerEmail"] == "asha@bial.example"


async def test_an_admin_sees_a_deletion_they_did_not_make(client, db_session) -> None:
    """Cross-owner, like every other route on this router. What has been deleted, and why, is
    not answerable from one person's rows."""
    someone_else = await UserFactory.create(db_session, email="ravi@bial.example")
    await _tombstone(db_session, owner=someone_else, project_name="Ramp Checklist")
    await db_session.commit()

    body = await _list(client, await _admin(db_session))

    assert "Ramp Checklist" in [r["projectName"] for r in body["deletions"]]


# --- the keyset walk ---------------------------------------------------------------


async def test_newest_first(client, db_session) -> None:
    """UUIDv7 keys, so `ORDER BY id DESC` IS creation order — which is why this table needs no
    second sort column."""
    for name in ("First", "Second", "Third"):
        await _tombstone(db_session, project_name=name)
    await db_session.commit()

    body = await _list(client, await _admin(db_session))

    assert [r["projectName"] for r in body["deletions"]] == ["Third", "Second", "First"]


async def test_the_cursor_walk_neither_duplicates_nor_skips(client, db_session) -> None:
    """THE GUARANTEE KEYSET EXISTS TO GIVE, and the one an offset mistake breaks only at the
    seam — which is why the page size here is deliberately smaller than the data."""
    for i in range(5):
        await _tombstone(db_session, project_name=f"P{i}")
    await db_session.commit()
    headers = await _admin(db_session)

    seen: list[str] = []
    cursor: str | None = None
    # Bounded, so a cursor that never advances fails the assert rather than hanging the suite.
    for _ in range(5):
        params: dict[str, object] = {"limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        body = await _list(client, headers, **params)
        seen.extend(r["projectName"] for r in body["deletions"])
        cursor = body["nextCursor"]
        if not body["hasMore"]:
            break

    assert seen == ["P4", "P3", "P2", "P1", "P0"]
    assert len(seen) == len(set(seen))  # nothing repeated across the seam


# --- search -------------------------------------------------------------------------


async def test_search_matches_the_reason_and_the_owner(client, db_session) -> None:
    owner = await UserFactory.create(db_session, email="ravi@bial.example")
    await _tombstone(db_session, project_name="Kept", remark="superseded by the gate pass tool")
    await _tombstone(db_session, owner=owner, project_name="Theirs", remark="not needed any more")
    await db_session.commit()
    headers = await _admin(db_session)

    by_remark = await _list(client, headers, q="superseded")
    assert [r["projectName"] for r in by_remark["deletions"]] == ["Kept"]

    by_owner = await _list(client, headers, q="ravi@bial.example")
    assert [r["projectName"] for r in by_owner["deletions"]] == ["Theirs"]


async def test_a_percent_in_the_search_is_a_literal_not_a_wildcard(client, db_session) -> None:
    """Without `autoescape`, `%` is a wildcard and the search silently returns the whole table.

    THE QUERY HAS TO BE A BARE `%` TO DISCRIMINATE. A first version searched `100%`, which
    matches exactly one row whether or not the `%` is escaped — so it passed against an
    unescaped `icontains` too, and a mutation check caught it. A lone `%` is the case where the
    two behaviours actually differ: escaped it means "rows containing a percent sign", unescaped
    it means "every row".
    """
    await _tombstone(db_session, project_name="Ordinary")
    await _tombstone(db_session, project_name="100% Coverage Log")
    await db_session.commit()

    body = await _list(client, await _admin(db_session), q="%")

    assert [r["projectName"] for r in body["deletions"]] == ["100% Coverage Log"]


# --- the read is audited -------------------------------------------------------------


async def test_reading_the_deletions_is_itself_recorded(client, db_session) -> None:
    """Unusual, and intended: `db:reveal` and `harness:parked:list` are audited reads on the
    same reasoning. Seeing why a citizen destroyed their own work is worth recording."""
    await _tombstone(db_session)
    await db_session.commit()

    await _list(client, await _admin(db_session), q="needed")

    row = await _latest_audit(db_session)
    assert row is not None
    assert row.resource_type == "deleted_project"
    assert row.detail is not None
    assert row.detail["q"] == "needed"
    assert row.detail["count"] == 1


async def test_the_audit_row_records_the_query_not_the_rows(client, db_session) -> None:
    """A read that logged what it returned would grow the audit table a second copy of the
    table it audits — and would put the citizen's own words in two places instead of one."""
    await _tombstone(db_session, remark="Superseded by the new gate pass tool")
    await db_session.commit()

    await _list(client, await _admin(db_session))

    row = await _latest_audit(db_session)
    assert row is not None
    assert "Superseded" not in str(row.detail)
