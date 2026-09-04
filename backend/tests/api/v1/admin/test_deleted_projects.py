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
from datetime import datetime
from hashlib import sha256
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.db.models.audit import AuditLog
from src.db.models.deleted_project import DeletedProject
from src.db.models.user import User
from src.main import create_app
from src.services.auth.csrf import issue_csrf_token
from src.services.auth.session_jwt import mint_session_jwt
from tests.factories import UserFactory

_TTL = settings.auth.access_ttl_seconds
# A POST, and the method is the security property rather than a style choice — see the route's
# own docstring. `refuse_cross_origin_writes` dispatches on the method, so the audited read had
# to become one to be covered by it.
_URL = "/v1/admin/deleted-projects/search"
_AUDIT_URL = "/v1/admin/deleted-projects/audit"


def _headers(user: User, *, with_csrf: bool = True) -> dict[str, str]:
    """Session cookie plus the signed double-submit pair the route now requires.

    Mirrors `tests/api/v1/build_sessions/conftest.py`'s `auth_headers` rather than importing it
    — that one is a fixture-module helper for a different package, and copying six lines is
    cheaper than a cross-package import that would drag its Azure settings fixture along.
    """
    jwt = mint_session_jwt(user.id, user.token_version, _TTL)
    if not with_csrf:
        return {"Cookie": f"session={jwt}"}
    csrf = issue_csrf_token(user.id, user.token_version)
    return {"Cookie": f"session={jwt}; csrf={csrf}", "X-CSRF-Token": csrf}


async def _admin(db: AsyncSession) -> dict[str, str]:
    return _headers(await UserFactory.create(db, email="admin@bial.com"))


async def _citizen(db: AsyncSession) -> dict[str, str]:
    return _headers(await UserFactory.create(db, email="asha@bial.example"))


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
    resp = await client.post(_URL, headers=headers, json=params)
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
    responses = create_app().openapi()["paths"][_URL]["post"]["responses"]
    assert {"401", "403", "422", "500"} <= set(responses)


async def test_an_unauthenticated_caller_is_refused(client) -> None:
    """401, NOT 403, and the distinction is worth pinning rather than assuming.

    `require_csrf` depends on `CurrentUser`, so authentication resolves BEFORE the CSRF check
    — an anonymous caller must be told it is unauthenticated, not handed a CSRF failure that
    implies a session it does not have. Dependency ordering makes that true; this asserts it,
    because reversing it would be a silent contract change.
    """
    assert (await client.post(_URL, json={})).status_code == 401


async def test_a_citizen_is_refused(client, db_session) -> None:
    """Fail-closed: anyone not on the allowlist is a citizen (AE1)."""
    await _tombstone(db_session)
    await db_session.commit()

    resp = await client.post(_URL, headers=await _citizen(db_session), json={})

    assert resp.status_code == 403


async def test_a_request_without_the_csrf_token_is_refused(client, db_session) -> None:
    """THE FINDING THIS CLOSES. As a GET this route sat outside `refuse_cross_origin_writes`
    (`main.py` limits it to POST/PUT/PATCH/DELETE, precisely because a GET is not supposed to
    mutate) while committing an audit row on every call. With a `SameSite=Lax` cookie and
    generated apps served same-site, app code written by a model from a citizen's prompt could
    drive a super-admin's session into writing audit rows under their identity — against the
    very table offered as the control that makes cross-owner reading acceptable.

    Mutation receipt: drop `dependencies=[RequireCsrf]` from the route and this is the test
    that goes red. Nothing else in the file distinguishes a token-bearing call from a bare one.
    """
    admin = await UserFactory.create(db_session, email="admin@bial.com")
    await _tombstone(db_session)
    await db_session.commit()

    resp = await client.post(_URL, headers=_headers(admin, with_csrf=False), json={})

    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "csrf_failed"


async def test_a_mismatched_csrf_token_is_refused(client, db_session) -> None:
    """The other half of double-submit: a cookie and header that disagree. An attacker can set
    a header but cannot read the signed cookie, so agreeing values are the proof of same-origin.
    """
    admin = await UserFactory.create(db_session, email="admin@bial.com")
    await db_session.commit()
    jwt = mint_session_jwt(admin.id, admin.token_version, _TTL)

    resp = await client.post(
        _URL,
        headers={"Cookie": f"session={jwt}; csrf=aaa.bbb", "X-CSRF-Token": "ccc.ddd"},
        json={},
    )

    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "csrf_failed"


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
    # Stamped so the row is REACHABLE. It was NULL, and `read_audit` matches on
    # `resource_id == <app id> OR detail["appId"] == <app id>` — a deletions search has no app,
    # so these rows existed where no reader in the product could ever match them.
    assert row.resource_id == "deleted-projects"
    assert row.detail is not None
    assert row.detail["count"] == 1


async def test_the_audited_query_is_hashed_not_stored(client, db_session) -> None:
    """`audit.py`'s own contract is "record WHO did WHAT to WHICH resource — never the record
    CONTENTS", and a 200-character free-text search term is contents. It is kept as a digest so
    an investigator who suspects a term can still confirm it, without `audit_logs` becoming a
    second, retention-free home for words a citizen wrote.

    Mutation receipt: put `search` back in place of the digest and the `not in` below fails.
    """
    await _tombstone(db_session, remark="No longer needed by the ground operations team")
    await db_session.commit()

    await _list(client, await _admin(db_session), q="ground")

    row = await _latest_audit(db_session)
    assert row is not None
    assert row.detail is not None
    assert "ground" not in str(row.detail)
    assert row.detail["filtered"] is True
    assert row.detail["qHash"] == sha256(b"ground").hexdigest()[:16]


async def test_an_unfiltered_read_records_that_it_was_unfiltered(client, db_session) -> None:
    """`filtered` has to distinguish "searched for something" from "walked the whole table" —
    a NULL `qHash` alone reads the same as a hash that failed to compute."""
    await _tombstone(db_session)
    await db_session.commit()

    await _list(client, await _admin(db_session))

    row = await _latest_audit(db_session)
    assert row is not None and row.detail is not None
    assert row.detail["filtered"] is False
    assert row.detail["qHash"] is None


async def test_the_audit_records_which_page_was_read(client, db_session) -> None:
    """Without the cursor, forty walks down the whole table are byte-identical to forty reloads
    of page one — and whether one deletion was read or a thousand is the question this row
    exists to answer. The cursor is an opaque UUIDv7 key, so the objection to storing `q` does
    not apply to it."""
    for name in ("First", "Second", "Third"):
        await _tombstone(db_session, project_name=name)
    await db_session.commit()
    headers = await _admin(db_session)

    first = await _list(client, headers, limit=2)
    await _list(client, headers, limit=2, cursor=first["nextCursor"])

    row = await _latest_audit(db_session)
    assert row is not None and row.detail is not None
    assert row.detail["cursor"] == first["nextCursor"]
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


# --- the date range #176 asked for ----------------------------------------------------


async def _at(db: AsyncSession, row: DeletedProject, when: str) -> None:
    """Move a tombstone's `deleted_at`. It is `server_default=now()`, so a test that wants a
    range needs rows placed either side of a boundary rather than three rows a millisecond
    apart."""
    await db.execute(
        sa.update(DeletedProject)
        .where(DeletedProject.id == row.id)
        .values(deleted_at=datetime.fromisoformat(when))
    )


async def test_the_range_excludes_deletions_before_it(client, db_session) -> None:
    """#176: "filters worth having: by owner, and by date range." The owner half was `q`; this
    is the half that was never built, while the PR said "Closes #176".

    Mutation receipt: drop the `deleted_at >= deleted_from` predicate and this goes red.
    """
    old = await _tombstone(db_session, project_name="January")
    new = await _tombstone(db_session, project_name="August")
    await _at(db_session, old, "2026-01-15T09:00:00+00:00")
    await _at(db_session, new, "2026-08-15T09:00:00+00:00")
    await db_session.commit()

    body = await _list(client, await _admin(db_session), deletedFrom="2026-06-01")

    assert [r["projectName"] for r in body["deletions"]] == ["August"]


async def test_the_range_excludes_deletions_after_it(client, db_session) -> None:
    """The other bound, separately — a single test using both cannot tell which one works."""
    old = await _tombstone(db_session, project_name="January")
    new = await _tombstone(db_session, project_name="August")
    await _at(db_session, old, "2026-01-15T09:00:00+00:00")
    await _at(db_session, new, "2026-08-15T09:00:00+00:00")
    await db_session.commit()

    body = await _list(client, await _admin(db_session), deletedTo="2026-06-01")

    assert [r["projectName"] for r in body["deletions"]] == ["January"]


async def test_the_upper_bound_includes_its_own_day(client, db_session) -> None:
    """INCLUSIVE, deliberately. An administrator asking for deletions up to the 15th means the
    15th; a half-open bound silently drops the last day of every range they type, which is the
    day they are most likely to care about.

    Mutation receipt: change `<=` to `<` and this is the only test that notices.
    """
    row = await _tombstone(db_session, project_name="On the boundary")
    await _at(db_session, row, "2026-08-15T00:00:00+00:00")
    await db_session.commit()

    body = await _list(client, await _admin(db_session), deletedTo="2026-08-15")

    assert [r["projectName"] for r in body["deletions"]] == ["On the boundary"]


async def test_a_malformed_date_is_refused_in_the_platform_envelope(client, db_session) -> None:
    """422 in the `{"error": {"message"}}` shape every other bad page argument raises, NOT
    FastAPI's native `{"detail": [...]}`. That is why the bound is a `str` on the request model
    and parsed in the handler: a `datetime` annotation would put two different 422 bodies on one
    endpoint, and `error_responses(...)` can document only one schema per status."""
    await db_session.commit()

    resp = await client.post(
        _URL, headers=await _admin(db_session), json={"deletedFrom": "last Tuesday"}
    )

    assert resp.status_code == 422
    assert "error" in resp.json()
    assert "deletedFrom" in resp.json()["error"]["message"]


# --- who read the log ------------------------------------------------------------------


async def test_the_audit_rows_are_reachable_by_a_reader(client, db_session) -> None:
    """THE FINDING THIS CLOSES. The argument for reading across owners is that the read is
    recorded — but `read_audit` matches on an app id, and a deletions search has no app, so the
    rows it wrote could never be retrieved by anything in the product. An audit row nobody can
    read is not a control.

    Mutation receipt: drop `resource_id=_DELETIONS_AUDIT_RESOURCE` from the write and this goes
    red, because the reader's predicate stops matching.
    """
    await _tombstone(db_session)
    await db_session.commit()
    headers = await _admin(db_session)
    await _list(client, headers, q="ground")

    resp = await client.get(_AUDIT_URL, headers=headers)

    assert resp.status_code == 200, resp.text
    events = resp.json()["events"]
    assert [e["action"] for e in events] == ["admin:deletions:list"]
    assert events[0]["username"] == "admin@bial.com"
    assert events[0]["count"] == 1


async def test_the_audit_reader_is_gated_like_everything_else(client, db_session) -> None:
    """It reports who exercised a privilege, so it is itself privileged. Fail-closed (AE1)."""
    await db_session.commit()

    assert (await client.get(_AUDIT_URL)).status_code == 401
    citizen = await client.get(_AUDIT_URL, headers=await _citizen(db_session))
    assert citizen.status_code == 403


async def test_the_audit_reader_shows_the_newest_read_first(client, db_session) -> None:
    """Two reads, ordered. A reader that returned them oldest-first would put the least
    interesting row at the top of a 200-row cap."""
    await _tombstone(db_session)
    await db_session.commit()
    headers = await _admin(db_session)

    await _list(client, headers)
    await _list(client, headers, q="ground")

    events = (await client.get(_AUDIT_URL, headers=headers)).json()["events"]

    assert len(events) == 2
    assert events[0]["detail"]["filtered"] is True
    assert events[1]["detail"]["filtered"] is False


# --- the mutants the first thirteen tests let live --------------------------------------


async def test_the_search_is_case_insensitive_on_every_column(client, db_session) -> None:
    """`icontains` on all four, not `contains` on some. Every earlier search test happened to
    match the stored casing exactly, so swapping any column to case-SENSITIVE passed.

    Mutation receipt: change any one of the four `icontains` to `contains` and one of these
    four assertions fails — which one names the column that regressed.
    """
    owner = await UserFactory.create(db_session, email="Priya.Nair@BIAL.example")
    await _tombstone(
        db_session,
        owner=owner,
        project_name="Gate Pass Tracker",
        deleted_by_name="Priya Nair",
        remark="Superseded by the Ground Operations rota",
    )
    await db_session.commit()
    headers = await _admin(db_session)

    assert len((await _list(client, headers, q="gate pass"))["deletions"]) == 1
    assert len((await _list(client, headers, q="PRIYA.NAIR@bial.EXAMPLE"))["deletions"]) == 1
    assert len((await _list(client, headers, q="priya nair"))["deletions"]) == 1
    assert len((await _list(client, headers, q="GROUND OPERATIONS"))["deletions"]) == 1


async def test_the_search_matches_who_deleted_it(client, db_session) -> None:
    """`deleted_by_name` is one of the four columns `q` searches, and the input's own
    placeholder promises it ("Search by project, person, or reason…") — but every fixture used
    the same default name, so dropping that arm of the `sa.or_` passed the whole suite.

    Mutation receipt: remove `DeletedProject.deleted_by_name.icontains(...)` and this goes red.
    The distinct remark and project name below keep the other three arms from answering for it.
    """
    await _tombstone(
        db_session,
        project_name="Visitor Log",
        deleted_by_name="Rohan Mehta",
        remark="No longer needed by the ground operations team",
    )
    await _tombstone(db_session, project_name="Gate Pass", deleted_by_name="Asha Rao")
    await db_session.commit()

    body = await _list(client, await _admin(db_session), q="Rohan")

    assert [r["projectName"] for r in body["deletions"]] == ["Visitor Log"]


async def test_the_row_carries_its_own_id_and_when_it_happened(client, db_session) -> None:
    """`id` and `deletedAt` were never asserted — so the envelope's key and the screen's entire
    "when" column were both unguarded, and the panel renders `deletedAt` in every row."""
    row = await _tombstone(db_session)
    await _at(db_session, row, "2026-08-15T09:30:00+00:00")
    await db_session.commit()

    got = (await _list(client, await _admin(db_session)))["deletions"][0]

    assert got["id"] == str(row.id)
    assert got["deletedAt"].startswith("2026-08-15T09:30:00")


async def test_had_app_and_had_database_are_not_interchangeable(client, db_session) -> None:
    """Both were `True` in the only test that read them, so swapping the two fields was
    invisible. They mean different things to an administrator — an app is public, a database
    held the citizen's data.

    Mutation receipt: swap `had_app=row.had_app` and `had_database=row.had_database` in
    `_to_response`'s deleted-project projection and this fails.
    """
    await _tombstone(db_session, had_app=True, had_database=False)
    await db_session.commit()

    got = (await _list(client, await _admin(db_session)))["deletions"][0]

    assert got["hadApp"] is True
    assert got["hadDatabase"] is False


async def test_the_actor_is_not_the_owner_when_someone_else_deleted_it(client, db_session) -> None:
    """The test that exists to separate `deletedBy` from `ownerId` could not: `_tombstone`
    defaults `deleted_by=owner.id`, so both fields held the same UUID and a projection reading
    `owner_id` for both passed. Overridden here so the two genuinely differ.

    Inert today — no route lets one citizen delete another's project — but this is precisely
    the field an administrator reads to answer "who did this", and it must not start lying the
    day such a route exists.
    """
    owner = await UserFactory.create(db_session, email="owner@bial.example")
    actor = await UserFactory.create(db_session, email="ops@bial.example")
    await _tombstone(db_session, owner=owner, deleted_by=actor.id, deleted_by_name="Ops Desk")
    await db_session.commit()

    got = (await _list(client, await _admin(db_session)))["deletions"][0]

    assert got["ownerId"] == str(owner.id)
    assert got["deletedBy"] == str(actor.id)
    assert got["deletedBy"] != got["ownerId"]


async def test_the_audited_count_is_the_page_not_the_probe_row(client, db_session) -> None:
    """`count` was only ever checked at 1, where `len(page)` and `len(rows)` agree. The query
    fetches `limit + 1` to detect `hasMore`, so auditing `len(rows)` would over-report every
    page that has a next one — here, 3 instead of 2.

    Mutation receipt: change `len(page)` to `len(rows)` in the audit write and this goes red.
    """
    for name in ("First", "Second", "Third"):
        await _tombstone(db_session, project_name=name)
    await db_session.commit()

    await _list(client, await _admin(db_session), limit=2)

    row = await _latest_audit(db_session)
    assert row is not None and row.detail is not None
    assert row.detail["count"] == 2
