"""The state rule itself, driven directly — `current_access`, with no route in front of it.

WHY THIS FILE EXISTS SEPARATELY FROM THE ROUTE TESTS. `current_access` earns its own module
under ADR-0010 partly on reuse and partly on a REALIZED testing benefit, and this is that
benefit realized: the rule is "the most recent NON-cancelled row", and the cases that tell it
apart from a naive `ORDER BY created_at DESC LIMIT 1` are ledger shapes, not user journeys.
Reaching them through HTTP would prove the rule only for the one surface that happens to call
it, and the switch-on refusal U4 builds calls it too.

★ THE MUTANT THIS FILE IS BUILT AROUND: drop `status != CANCELLED` from `current_access`'s
predicate — the "latest row wins" rule — and
`test_a_history_of_nothing_but_cancels_reads_never_asked` and
`test_a_cancel_written_after_an_approval_does_not_hide_the_approval` both go red.

Every test seeds `connector_access_requests` DIRECTLY rather than through the routes: two of
the shapes below (an approval followed by a cancel; a decline sitting under a later ask) are
ledger states the routes will not currently produce, and a rule that holds only because the
routes are disciplined is a rule that breaks the first time somebody adds a route.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from src.core.connectors import CONNECTORS
from src.db.models.connector_access import ConnectorAccessRequest, ConnectorRequestStatus
from src.services.connectors import ConnectorPersonState, current_access
from tests.factories import UserFactory

# Read off the registry rather than written down. `current_access` does not validate keys — the
# route does — so any string would drive these tests; the real one keeps them honest, and taking
# it from the catalogue means there is no second place the key is spelled.
_KEY = next(iter(CONNECTORS))
_OTHER = "another-system"
_REMARKS = "I build the stand and turnaround boards and they need on-block times."
_DECLINE = "Nothing you have built needs operational flight data yet."


async def _seed(
    db,
    user_id: uuid.UUID,
    status: ConnectorRequestStatus,
    **overrides: Any,
) -> ConnectorAccessRequest:
    """One ledger row, flushed. Rows land in creation order, and the `db_session` fixture runs
    the whole test in ONE transaction — so `now()` is identical on every row here and the
    UUIDv7 `id` tie-break is what actually orders them. That is not an artefact of the test: it
    is the same tie a production backfill or a two-row transaction would create."""
    data: dict[str, Any] = {
        "user_id": user_id,
        "connector_key": _KEY,
        "status": status,
        "requester_remarks": _REMARKS,
    }
    data.update(overrides)
    row = ConnectorAccessRequest(**data)
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return row


async def _decided(
    db,
    user_id: uuid.UUID,
    status: ConnectorRequestStatus,
    decider,
    **overrides: Any,
) -> ConnectorAccessRequest:
    return await _seed(
        db,
        user_id,
        status,
        decided_by_id=decider.id,
        decided_at=datetime.now(UTC),
        **overrides,
    )


# --- the four states ------------------------------------------------------------


async def test_a_person_with_no_rows_has_never_asked(db_session) -> None:
    user = await UserFactory.create(db_session)

    access = await current_access(db_session, user_id=user.id, connector_key=_KEY)

    assert access.state is ConnectorPersonState.NEVER_ASKED
    assert access.request is None
    assert access.decided_by_name is None
    # The shape `core.connectors.resolve_window` takes: no state, so nothing is effectively on.
    assert access.request_status is None


async def test_a_waiting_row_reads_pending_and_carries_no_decider(db_session) -> None:
    user = await UserFactory.create(db_session)
    row = await _seed(db_session, user.id, ConnectorRequestStatus.PENDING)

    access = await current_access(db_session, user_id=user.id, connector_key=_KEY)

    assert access.state is ConnectorPersonState.PENDING
    assert access.request is not None and access.request.id == row.id
    assert access.decided_by_name is None
    assert access.request_status is ConnectorRequestStatus.PENDING


async def test_an_approval_names_its_administrator(db_session) -> None:
    user = await UserFactory.create(db_session)
    admin = await UserFactory.create(
        db_session, email="rahul.menon@rvaiglobal.com", display_name="Rahul Menon"
    )
    await _decided(db_session, user.id, ConnectorRequestStatus.APPROVED, admin)

    access = await current_access(db_session, user_id=user.id, connector_key=_KEY)

    assert access.state is ConnectorPersonState.APPROVED
    assert access.decided_by_name == "Rahul Menon"
    assert access.request is not None and access.request.decided_at is not None
    # This is the value the window resolver branches on — the whole point of the grant.
    assert access.request_status is ConnectorRequestStatus.APPROVED


async def test_an_administrator_with_no_display_name_is_named_by_their_email(db_session) -> None:
    """`users.display_name` IS NULLABLE and really is null for some Entra accounts.

    The board's sentence is `Approved for you 2 Sep · Rahul Menon`; with no fallback it becomes
    `Approved for you 2 Sep · ` and the citizen is told their access was granted by nobody. The
    email identifies the account just as well, and the project tombstone already made this call
    for the same reason."""
    user = await UserFactory.create(db_session)
    admin = await UserFactory.create(
        db_session, email="ops.admin@rvaiglobal.com", display_name=None
    )
    await _decided(db_session, user.id, ConnectorRequestStatus.APPROVED, admin)

    access = await current_access(db_session, user_id=user.id, connector_key=_KEY)

    assert access.decided_by_name == "ops.admin@rvaiglobal.com"
    assert access.decided_by_name  # not blank, which is the failure this guards


async def test_a_decline_keeps_the_administrators_words(db_session) -> None:
    user = await UserFactory.create(db_session)
    admin = await UserFactory.create(db_session, display_name="Rahul Menon")
    await _decided(
        db_session,
        user.id,
        ConnectorRequestStatus.DECLINED,
        admin,
        decision_remarks=_DECLINE,
    )

    access = await current_access(db_session, user_id=user.id, connector_key=_KEY)

    assert access.state is ConnectorPersonState.DECLINED
    assert access.request is not None and access.request.decision_remarks == _DECLINE
    assert access.decided_by_name == "Rahul Menon"


async def test_a_decision_outlives_its_deleted_administrator(db_session) -> None:
    """`decided_by_id` is `ON DELETE SET NULL`, so an administrator leaving BIAL leaves the
    grant standing with nobody named. `None` here means "no decider to name", which is a
    different thing from "the decider's name is blank" — and the citizen keeps their access."""
    user = await UserFactory.create(db_session)
    await _seed(
        db_session,
        user.id,
        ConnectorRequestStatus.APPROVED,
        decided_by_id=None,
        decided_at=datetime.now(UTC),
    )

    access = await current_access(db_session, user_id=user.id, connector_key=_KEY)

    assert access.state is ConnectorPersonState.APPROVED
    assert access.decided_by_name is None


# --- ★ the rule: the most recent NON-cancelled row ------------------------------


async def test_a_history_of_nothing_but_cancels_reads_never_asked(db_session) -> None:
    """★ THE MUTANT TRAP. Under "latest row wins" this person reads `cancelled` — which is not
    one of the four person states at all — and reads it forever, so `Request access` is never
    offered to them again. Two cancels, not one, because a single row could pass by accident on
    an implementation that only ever looks at the newest of several."""
    user = await UserFactory.create(db_session)
    await _seed(db_session, user.id, ConnectorRequestStatus.CANCELLED)
    await _seed(db_session, user.id, ConnectorRequestStatus.CANCELLED)

    access = await current_access(db_session, user_id=user.id, connector_key=_KEY)

    assert access.state is ConnectorPersonState.NEVER_ASKED
    assert access.request is None
    assert access.request_status is None


async def test_a_cancel_written_after_an_approval_does_not_hide_the_approval(db_session) -> None:
    """★ THE SECOND MUTANT TRAP, and the stronger one: under "latest row wins" this reads
    `cancelled` where the truth is `approved`, so an approved citizen loses their access to a
    row that is not a decision.

    The routes do not produce this shape today — `cancel` only ever touches a `pending` row —
    but the LEDGER permits it, and the rule must not depend on route discipline for its
    correctness. This is origin Q4's own scenario, kept after `Ask again` was dropped (D10)."""
    user = await UserFactory.create(db_session)
    admin = await UserFactory.create(db_session, display_name="Rahul Menon")
    await _decided(db_session, user.id, ConnectorRequestStatus.APPROVED, admin)
    await _seed(db_session, user.id, ConnectorRequestStatus.CANCELLED)

    access = await current_access(db_session, user_id=user.id, connector_key=_KEY)

    assert access.state is ConnectorPersonState.APPROVED
    assert access.decided_by_name == "Rahul Menon"


async def test_the_newest_non_cancelled_row_wins_when_the_timestamps_tie(db_session) -> None:
    """Asked, cancelled, asked again: the second ask is the answer.

    Both surviving rows carry the SAME `created_at` — `now()` is the transaction's timestamp —
    so this is the case the UUIDv7 `id` tie-break settles. Remove that tie-break and the
    database is free to return either row."""
    user = await UserFactory.create(db_session)
    await _seed(db_session, user.id, ConnectorRequestStatus.CANCELLED, requester_remarks="first")
    second = await _seed(db_session, user.id, ConnectorRequestStatus.PENDING)
    third = await _seed(db_session, user.id, ConnectorRequestStatus.DECLINED)
    assert second.created_at == third.created_at, "the tie this test is about did not happen"

    access = await current_access(db_session, user_id=user.id, connector_key=_KEY)

    assert access.state is ConnectorPersonState.DECLINED
    assert access.request is not None and access.request.id == third.id


# --- scoping --------------------------------------------------------------------


async def test_one_persons_history_never_answers_for_another(db_session) -> None:
    asha = await UserFactory.create(db_session, email="asha@rvaiglobal.com")
    ravi = await UserFactory.create(db_session, email="ravi@rvaiglobal.com")
    admin = await UserFactory.create(db_session, display_name="Rahul Menon")
    await _decided(db_session, asha.id, ConnectorRequestStatus.APPROVED, admin)

    assert (
        await current_access(db_session, user_id=ravi.id, connector_key=_KEY)
    ).state is ConnectorPersonState.NEVER_ASKED
    assert (
        await current_access(db_session, user_id=asha.id, connector_key=_KEY)
    ).state is ConnectorPersonState.APPROVED


async def test_one_connectors_history_never_answers_for_another(db_session) -> None:
    """The predicate spans the PAIR. A grant is per connector, so an approval on one must not
    read as an approval on the next one added to the registry."""
    user = await UserFactory.create(db_session)
    admin = await UserFactory.create(db_session, display_name="Rahul Menon")
    await _decided(db_session, user.id, ConnectorRequestStatus.APPROVED, admin)

    other = await current_access(db_session, user_id=user.id, connector_key=_OTHER)

    assert other.state is ConnectorPersonState.NEVER_ASKED


# --- the invariant the mapping refuses to paper over ----------------------------


async def test_a_cancelled_row_reaching_the_mapping_is_refused_not_defaulted(db_session) -> None:
    """The mapping's own guard, driven directly.

    If the predicate above were ever loosened, a cancelled row would reach `_person_state`.
    Answering `neverAsked` there would offer `Request access` to somebody who is approved, and
    answering `pending` would silently switch their connector off everywhere — so it raises."""
    from src.services.connectors.access import _person_state

    with pytest.raises(ValueError, match="should have excluded it"):
        _person_state(ConnectorRequestStatus.CANCELLED)
