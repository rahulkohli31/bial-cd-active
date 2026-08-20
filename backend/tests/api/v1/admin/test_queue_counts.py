"""The waiting-count route (U13, P1) — per-status counts, superadmin-only, and cheap.

The badge that reads this route is the only thing telling an administrator a queue has
items in it, so three properties are pinned here rather than assumed:

* it counts every status, zero-filling the empty ones (a blank badge and a zero badge
  must never be the same pixel),
* it is gated exactly like every other admin route (a citizen gets the standard refusal),
* and it does NOT run the listing's app-database size probe. That last one is the whole
  reason this route exists instead of `len(listApps('pending'))`, and it is the property
  a future field addition would silently break — so it is asserted, not documented.

Plus the withdrawal race the badge's queue produces: an owner pulling their submission
back (U8/P6) while an administrator is mid-review, which approve and reject must answer
by NAMING the event rather than describing a column.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import storage_dependency, storage_or_none_dependency
from src.config import settings
from src.db.models.app_registry import AppRegistry, ApprovalRoute, AppStatus
from src.services.auth.session_jwt import mint_session_jwt
from src.services.storage import submission_key
from tests.factories import AppRegistryFactory, UserFactory
from tests.fakes import FakeStorage

_TTL = settings.auth.access_ttl_seconds
_SHA = "ab" * 20


def _cookie(jwt: str) -> dict[str, str]:
    return {"Cookie": f"session={jwt}"}


async def _admin(db: AsyncSession) -> dict[str, str]:
    user = await UserFactory.create(db, email="admin@bial.com")
    return _cookie(mint_session_jwt(user.id, user.token_version, _TTL))


async def _citizen(db: AsyncSession) -> dict[str, str]:
    user = await UserFactory.create(db, email="nobody@rvaiglobal.com")
    return _cookie(mint_session_jwt(user.id, user.token_version, _TTL))


async def _app(db: AsyncSession, **overrides: Any) -> AppRegistry:
    owner = await UserFactory.create(db)
    return await AppRegistryFactory.create(db, user_id=owner.id, **overrides)


async def _owned_app(db: AsyncSession, **overrides: Any) -> tuple[AppRegistry, dict[str, str]]:
    """An app plus its OWNER's cookie — the withdrawal race needs both actors."""
    owner = await UserFactory.create(db)
    row = await AppRegistryFactory.create(db, user_id=owner.id, **overrides)
    return row, _cookie(mint_session_jwt(owner.id, owner.token_version, _TTL))


def _pending(**extra: Any) -> dict[str, Any]:
    return {
        "status": AppStatus.PENDING,
        "source_submission_id": uuid.uuid4(),
        "source_commit_sha": _SHA,
        "submitted_at": datetime.now(UTC),
        "approval_route": ApprovalRoute.SELF_PUBLISH,
        **extra,
    }


# --- the count route -----------------------------------------------------------


async def test_counts_every_status_and_zero_fills_the_empty_ones(client, db_session) -> None:
    headers = await _admin(db_session)
    await _app(db_session, **_pending())
    await _app(db_session, **_pending())
    await _app(db_session, status=AppStatus.APPROVED)

    resp = await client.get("/v1/admin/apps/counts", headers=headers)

    assert resp.status_code == 200
    counts = resp.json()["counts"]
    assert counts["pending"] == 2
    assert counts["approved"] == 1
    # Zero-filled, not absent: the badge must be able to tell "nothing waiting" from
    # "we didn't ask", and an absent key renders as neither.
    assert counts["rejected"] == 0
    assert counts["disabled"] == 0
    assert counts["draft"] == 0
    assert set(counts) == {member.value for member in AppStatus}


async def test_counts_does_not_run_the_size_probe(client, db_session, monkeypatch) -> None:
    """THE REASON THIS ROUTE EXISTS. The listing probes the app-database cluster once per
    page for its advisory size column; a badge polling that would pay for it on a cadence.

    Mutation check: point the badge at `list_apps` and this goes red on the second assert.
    """
    from src.api.v1.admin import router as admin_router

    probes: list[object] = []

    async def _recording_probe(db: object, project_ids: object) -> dict[uuid.UUID, int]:
        probes.append(project_ids)
        return {}

    monkeypatch.setattr(admin_router, "_advisory_sizes", _recording_probe)

    headers = await _admin(db_session)
    await _app(db_session, **_pending())

    assert (await client.get("/v1/admin/apps/counts", headers=headers)).status_code == 200
    assert probes == []  # the count route never touches the maintenance engine

    # …and the listing still does, so the assertion above is about THIS route rather than
    # about a probe that quietly stopped running everywhere.
    assert (await client.get("/v1/admin/apps", headers=headers)).status_code == 200
    assert len(probes) == 1


async def test_counts_is_superadmin_only(client, db_session) -> None:
    await _app(db_session, **_pending())
    assert (await client.get("/v1/admin/apps/counts")).status_code == 401
    citizen = await _citizen(db_session)
    assert (await client.get("/v1/admin/apps/counts", headers=citizen)).status_code == 403


async def test_counts_is_not_shadowed_by_the_app_id_routes(client, db_session) -> None:
    """`/counts` is a literal segment on a router whose other paths are `/{app_id}/…`.
    A regression that let it parse as an app id would 422 on the uuid, not 200."""
    headers = await _admin(db_session)
    resp = await client.get("/v1/admin/apps/counts", headers=headers)
    assert resp.status_code == 200


# --- the rejection-note floor (P3) ---------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({}, id="omitted"),
        pytest.param({"note": None}, id="null"),
        pytest.param({"note": ""}, id="empty"),
        pytest.param({"note": "too short"}, id="under-the-floor"),
        pytest.param({"note": " " * 40}, id="whitespace-only"),
    ],
)
async def test_reject_refuses_a_note_that_says_nothing(client, db_session, body) -> None:
    """A rejection is the only thing the citizen gets back, and an empty one rendered as a
    bare red badge. Whitespace is in the table on purpose: `min_length` alone counts
    spaces, so forty of them would clear a naive floor and reach the citizen as blank."""
    headers = await _admin(db_session)
    row = await _app(db_session, **_pending())

    resp = await client.post(f"/v1/admin/apps/{row.id}/reject", json=body, headers=headers)

    assert resp.status_code == 422
    await db_session.refresh(row)
    assert row.status is AppStatus.PENDING  # nothing was decided
    assert row.rejection_note is None


async def test_reject_stores_the_trimmed_note(client, db_session) -> None:
    headers = await _admin(db_session)
    row = await _app(db_session, **_pending())
    note = "  Personal data in a public app needs a named owner.  "

    resp = await client.post(
        f"/v1/admin/apps/{row.id}/reject", json={"note": note}, headers=headers
    )

    assert resp.status_code == 200
    await db_session.refresh(row)
    assert row.status is AppStatus.REJECTED
    # Trimmed, so the column never carries the padding the floor was measured against.
    assert row.rejection_note == note.strip()


# --- the withdrawal race (U8/P6 → U13) -----------------------------------------


async def test_approving_a_withdrawn_submission_names_the_withdrawal(
    app, client, db_session
) -> None:
    headers = await _admin(db_session)
    row, owner = await _owned_app(db_session, **_pending())
    reviewed = row.source_submission_id
    assert reviewed is not None
    store = FakeStorage()
    app.dependency_overrides[storage_dependency] = lambda: store
    app.dependency_overrides[storage_or_none_dependency] = lambda: store
    # The artifact is present, so a refusal here can only be about the withdrawal — never
    # about R11's missing-bundle branch.
    store.objects[submission_key(row.id, reviewed)] = b"# v2 git bundle\nfake"

    # The owner withdraws while the administrator has the modal open (U8's exact shape:
    # DRAFT, pin and lineage and declaration cleared).
    withdrawn = await client.post(f"/v1/apps/{row.id}/withdraw", headers=owner)
    assert withdrawn.status_code == 200

    resp = await client.post(
        f"/v1/admin/apps/{row.id}/approve",
        json={"submissionId": str(reviewed)},
        headers=headers,
    )

    assert resp.status_code == 409
    error = resp.json()["error"]
    assert error["code"] == "submission_withdrawn"
    assert "withdrew" in error["message"]
    # NOT the column-shaped copy — the administrator learns what happened, not which
    # predicate refused.
    assert "Only a pending app" not in error["message"]


async def test_rejecting_a_withdrawn_submission_names_the_withdrawal(client, db_session) -> None:
    headers = await _admin(db_session)
    row, owner = await _owned_app(db_session, **_pending())

    withdrawn = await client.post(f"/v1/apps/{row.id}/withdraw", headers=owner)
    assert withdrawn.status_code == 200

    resp = await client.post(
        f"/v1/admin/apps/{row.id}/reject",
        json={"note": "This one needs a named data owner before it goes live."},
        headers=headers,
    )

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "submission_withdrawn"


async def test_a_decided_app_still_gets_the_state_copy_not_the_withdrawal_copy(
    client, db_session
) -> None:
    """The withdrawal branch is DRAFT-with-no-pin, not "anything that isn't pending" — a
    rejected app must not be reported as withdrawn."""
    headers = await _admin(db_session)
    row = await _app(db_session, status=AppStatus.REJECTED, source_submission_id=uuid.uuid4())

    resp = await client.post(
        f"/v1/admin/apps/{row.id}/reject",
        json={"note": "This is a long enough note to clear the floor."},
        headers=headers,
    )

    assert resp.status_code == 409
    assert resp.json()["error"].get("code") is None
    assert "Only a pending app can be rejected" in resp.json()["error"]["message"]
