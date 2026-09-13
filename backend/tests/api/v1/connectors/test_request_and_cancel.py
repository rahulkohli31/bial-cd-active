"""`POST /v1/connectors/{key}/request` and `.../cancel` — the two writes a citizen makes about
their own access.

FOUR CLAIMS THIS FILE EXISTS FOR:

1. **The remark rule is the shipped stated-reason rule** — 5 to 50 words with a 2,000-character
   paste backstop (owner decision D1), refused at the Pydantic boundary with the sentence a
   person actually reads. Asserted on the error's `type` AND its message, never on `422` alone:
   the whole point of the backstop is that a long paste gets the CHARACTER sentence rather than
   a word bound it is already inside.
2. **`declined` is terminal in the DATA** (D10). `Ask again` is not built, and this pins that as
   a server refusal with the code `already_decided` rather than a missing button.
3. **The insert does not lose the race.** `ON CONFLICT DO NOTHING` inferred against the partial
   pending index, proved by two genuinely concurrent requests on real committed rows.
4. **Cancel is an atomic guarded update**, and zero rows is a 409 — never a `Cancel` that
   reports success while the request sails on into the administrator's queue.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
import sqlalchemy as sa

import src.db.base as db_base
from src.db.models.audit import AuditLog
from src.db.models.connector_access import ConnectorAccessRequest, ConnectorRequestStatus
from src.db.models.user import User
from src.db.session import get_db
from tests.api.v1.connectors.conftest import (
    CONNECTORS_URL,
    DECLINE_REMARKS,
    KEY,
    REMARKS,
    UNKNOWN_KEY,
    auth_headers,
    seed_decision,
    seed_request,
)
from tests.factories import UserFactory

_REQUEST = f"{CONNECTORS_URL}/{KEY}/request"
_CANCEL = f"{CONNECTORS_URL}/{KEY}/cancel"


def _words(n: int) -> str:
    return " ".join(f"w{i}" for i in range(n))


async def _ask(client, user, remarks: str = REMARKS, *, key: str = KEY, csrf: bool = True):
    return await client.post(
        f"{CONNECTORS_URL}/{key}/request",
        headers=auth_headers(user, with_csrf=csrf),
        json={"remarks": remarks},
    )


async def _rows(db, user_id: uuid.UUID) -> list[ConnectorAccessRequest]:
    result = await db.execute(
        sa.select(ConnectorAccessRequest)
        .where(ConnectorAccessRequest.user_id == user_id)
        .order_by(ConnectorAccessRequest.id)
    )
    return list(result.scalars())


def _refusal(resp) -> tuple[str, str]:
    """The 422 body's discriminator and its sentence. `validation_exception_handler` keeps
    `type` / `loc` / `msg` and drops `input` / `ctx`, and strips Pydantic's `Value error, `
    prefix — so `msg` is product copy and is asserted as such."""
    detail = resp.json()["detail"][0]
    return detail["type"], detail["msg"]


# --- asking ---------------------------------------------------------------------


async def test_asking_moves_a_new_citizen_to_pending(client, db_session) -> None:
    user = await UserFactory.create(db_session)

    resp = await _ask(client, user)

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["key"] == KEY
    assert body["state"] == "pending"
    assert body["askedAt"] is not None

    rows = await _rows(db_session, user.id)
    assert len(rows) == 1
    assert rows[0].status is ConnectorRequestStatus.PENDING
    assert rows[0].requester_remarks == REMARKS
    # Nobody has decided, and the row says so rather than defaulting to the asker.
    assert rows[0].decided_by_id is None and rows[0].decided_at is None


async def test_the_remark_is_stored_trimmed(client, db_session) -> None:
    """The validator returns the trimmed value, so the column never carries the padding a
    textarea collects."""
    user = await UserFactory.create(db_session)

    assert (await _ask(client, user, f"  {REMARKS}\n")).status_code == 201

    rows = await _rows(db_session, user.id)
    assert rows[0].requester_remarks == REMARKS


async def test_asking_writes_no_audit_row(client, db_session) -> None:
    """R11/origin R9: the citizen is acting on their OWN row, so an audit entry would carry the
    same actor and the same timestamp the request row already holds. Approve and decline — one
    person acting on another — are the audited pair, and they are U5's."""
    user = await UserFactory.create(db_session)

    assert (await _ask(client, user)).status_code == 201
    assert (await client.post(_CANCEL, headers=auth_headers(user))).status_code == 200

    audited = await db_session.scalar(
        sa.select(sa.func.count()).select_from(AuditLog).where(AuditLog.actor_id == user.id)
    )
    assert audited == 0


async def test_a_second_ask_while_one_is_waiting_is_refused(client, db_session) -> None:
    user = await UserFactory.create(db_session)
    assert (await _ask(client, user)).status_code == 201

    resp = await _ask(client, user)

    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "already_pending"
    assert len(await _rows(db_session, user.id)) == 1


async def test_a_declined_person_cannot_ask_again(client, db_session) -> None:
    """★ THE D10 TEST. `Ask again` is not built and this pass gives an administrator no way to
    grant access directly either, so a decline is FINAL — enforced in the data, not merely
    absent from the screen. The refusal leaves the decline exactly as it was, remark included:
    a 409 that quietly replaced the citizen's state would be worse than no refusal at all."""
    user = await UserFactory.create(db_session)
    admin = await UserFactory.create(db_session, display_name="Rahul Menon")
    await seed_decision(
        db_session,
        user.id,
        ConnectorRequestStatus.DECLINED,
        admin,
        decision_remarks=DECLINE_REMARKS,
    )

    resp = await _ask(client, user)

    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "already_decided"
    assert len(await _rows(db_session, user.id)) == 1

    after = (await client.get(CONNECTORS_URL, headers=auth_headers(user))).json()["connectors"][0]
    assert after["state"] == "declined"
    assert after["decisionRemarks"] == DECLINE_REMARKS


async def test_an_approved_person_asking_again_is_refused(client, db_session) -> None:
    """Not merely tidiness. A second PENDING row would become the most recent non-cancelled row,
    so this person's state would fall back to `pending` — and `resolve_window` switches a
    connector off for anyone who is not APPROVED. Asking again would revoke their own access in
    every project until an administrator acted."""
    user = await UserFactory.create(db_session)
    admin = await UserFactory.create(db_session, display_name="Rahul Menon")
    await seed_decision(db_session, user.id, ConnectorRequestStatus.APPROVED, admin)

    resp = await _ask(client, user)

    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "already_decided"
    assert len(await _rows(db_session, user.id)) == 1

    after = (await client.get(CONNECTORS_URL, headers=auth_headers(user))).json()["connectors"][0]
    assert after["state"] == "approved"


async def test_asking_again_after_cancelling_is_allowed(client, db_session) -> None:
    """The other side of the state rule: a cancel returns you to `neverAsked`, and `neverAsked`
    can ask. The partial index only ever forbids a SECOND OPEN ask."""
    user = await UserFactory.create(db_session)
    assert (await _ask(client, user)).status_code == 201
    assert (await client.post(_CANCEL, headers=auth_headers(user))).status_code == 200

    resp = await _ask(client, user, "This time the stand allocation board genuinely needs it.")

    assert resp.status_code == 201, resp.text
    assert resp.json()["state"] == "pending"
    statuses = [row.status for row in await _rows(db_session, user.id)]
    assert statuses == [ConnectorRequestStatus.CANCELLED, ConnectorRequestStatus.PENDING]


# --- the remark rule ------------------------------------------------------------


@pytest.mark.parametrize(
    ("remarks", "expected"),
    [
        ("", "Say what you need the data for."),
        ("     ", "Say what you need the data for."),
        ("\t\n  ", "Say what you need the data for."),
        (_words(4), "Give a little more detail — at least 5 words."),
        (_words(51), "Keep the reason under 50 words."),
    ],
    ids=["empty", "spaces", "whitespace-run", "four-words", "fifty-one-words"],
)
async def test_a_remark_outside_the_rule_is_refused_with_its_own_sentence(
    client, db_session, remarks, expected
) -> None:
    """Asserted on the error's `type` AND its sentence, not on the status alone: four distinct
    refusals share one status code, and "it 422'd" cannot tell them apart."""
    user = await UserFactory.create(db_session)

    resp = await _ask(client, user, remarks)

    assert resp.status_code == 422, resp.text
    kind, message = _refusal(resp)
    assert kind == "value_error"
    assert message == expected
    # Written for a person: the machine prefix comes off at the boundary.
    assert "Value error" not in resp.text
    assert await _rows(db_session, user.id) == []


async def test_a_long_paste_gets_the_character_sentence_not_the_word_one(
    client, db_session
) -> None:
    """★ THE BACKSTOP'S OWN CASE, and the reason it is not redundant with the word bounds.

    Forty words of URLs or a non-English script can clear 2,000 characters while genuinely
    under fifty words. Telling that person to "keep the reason under 50 words" is telling them
    to get under a bound they are already inside — the one refusal they cannot act on."""
    user = await UserFactory.create(db_session)
    paste = " ".join("u" * 50 for _ in range(40))  # 40 words, 2039 characters
    assert len(paste) > 2000 and len(paste.split()) < 50

    resp = await _ask(client, user, paste)

    assert resp.status_code == 422, resp.text
    kind, message = _refusal(resp)
    assert kind == "value_error"
    assert message == "That reason is too long. Keep it under 2000 characters."
    assert await _rows(db_session, user.id) == []


@pytest.mark.parametrize("n", [5, 27, 50], ids=["floor", "middle", "ceiling"])
async def test_a_remark_inside_the_rule_is_accepted(client, db_session, n) -> None:
    """Both bounds are INCLUSIVE — a rule whose stated limit is itself refused is a rule the
    live counter in the browser would disagree with."""
    user = await UserFactory.create(db_session)

    assert (await _ask(client, user, _words(n))).status_code == 201


async def test_a_missing_remarks_field_is_refused(client, db_session) -> None:
    user = await UserFactory.create(db_session)

    resp = await client.post(_REQUEST, headers=auth_headers(user), json={})

    assert resp.status_code == 422, resp.text
    assert _refusal(resp)[0] == "missing"
    assert await _rows(db_session, user.id) == []


# --- an unknown connector -------------------------------------------------------


async def test_asking_for_a_connector_that_does_not_exist_is_a_404(client, db_session) -> None:
    """The registry is the catalogue (R15) — there is no `connectors` table — so an unknown key
    is refused at the route and never reaches the database."""
    user = await UserFactory.create(db_session)

    resp = await _ask(client, user, key=UNKNOWN_KEY)

    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "unknown_connector"
    assert await _rows(db_session, user.id) == []


async def test_cancelling_a_connector_that_does_not_exist_is_a_404(client, db_session) -> None:
    user = await UserFactory.create(db_session)

    resp = await client.post(f"{CONNECTORS_URL}/{UNKNOWN_KEY}/cancel", headers=auth_headers(user))

    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "unknown_connector"


# --- cancelling -----------------------------------------------------------------


async def test_cancelling_a_waiting_request_returns_the_citizen_to_never_asked(
    client, db_session
) -> None:
    """The row is KEPT as history — a cancel is not a delete — and the derived state is read
    back from the remaining rows rather than asserted by the route."""
    user = await UserFactory.create(db_session)
    assert (await _ask(client, user)).status_code == 201

    resp = await client.post(_CANCEL, headers=auth_headers(user))

    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "neverAsked"
    rows = await _rows(db_session, user.id)
    assert len(rows) == 1
    assert rows[0].status is ConnectorRequestStatus.CANCELLED
    assert rows[0].requester_remarks == REMARKS  # the words survive the withdrawal


async def test_cancelling_with_nothing_waiting_is_refused(client, db_session) -> None:
    user = await UserFactory.create(db_session)

    resp = await client.post(_CANCEL, headers=auth_headers(user))

    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "nothing_pending"


@pytest.mark.parametrize(
    "decided", [ConnectorRequestStatus.APPROVED, ConnectorRequestStatus.DECLINED]
)
async def test_cancelling_a_decided_request_is_refused_and_changes_nothing(
    client, db_session, decided
) -> None:
    """The guarded UPDATE's `status = 'pending'` predicate IS the gate. Without it a citizen
    could cancel their own approval, or erase a decline they did not like."""
    user = await UserFactory.create(db_session)
    admin = await UserFactory.create(db_session, display_name="Rahul Menon")
    await seed_decision(db_session, user.id, decided, admin)

    resp = await client.post(_CANCEL, headers=auth_headers(user))

    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "nothing_pending"
    rows = await _rows(db_session, user.id)
    assert [row.status for row in rows] == [decided]


async def test_one_citizen_cannot_cancel_anothers_request(client, db_session) -> None:
    """`user_id` is in the UPDATE's predicate, so this is a 409 about the caller's own (absent)
    request rather than a write that reaches across the boundary."""
    asha = await UserFactory.create(db_session, email="asha@rvaiglobal.com")
    ravi = await UserFactory.create(db_session, email="ravi@rvaiglobal.com")
    await seed_request(db_session, asha.id, ConnectorRequestStatus.PENDING)

    resp = await client.post(_CANCEL, headers=auth_headers(ravi))

    assert resp.status_code == 409, resp.text
    rows = await _rows(db_session, asha.id)
    assert [row.status for row in rows] == [ConnectorRequestStatus.PENDING]


# --- CSRF -----------------------------------------------------------------------


async def test_asking_without_the_csrf_header_is_refused(client, db_session) -> None:
    user = await UserFactory.create(db_session)

    resp = await _ask(client, user, csrf=False)

    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "csrf_failed"
    assert await _rows(db_session, user.id) == []


async def test_cancelling_without_the_csrf_header_is_refused(client, db_session) -> None:
    user = await UserFactory.create(db_session)
    await seed_request(db_session, user.id, ConnectorRequestStatus.PENDING)

    resp = await client.post(_CANCEL, headers=auth_headers(user, with_csrf=False))

    assert resp.status_code == 403, resp.text
    rows = await _rows(db_session, user.id)
    assert [row.status for row in rows] == [ConnectorRequestStatus.PENDING]


# --- the race -------------------------------------------------------------------


async def test_two_simultaneous_asks_leave_one_row_one_201_and_one_409(app, client) -> None:
    """★ THE GENUINE RACE, on REAL committed rows with a fresh session per request.

    Both requests read the pre-check before either insert lands, so both pass it; the partial
    unique index then settles which one wins and `ON CONFLICT DO NOTHING` turns the loser's
    zero returned rows into the same 409 the pre-check raises. This is reachable rather than
    theoretical — `ComposerBox.tsx` records that `aria-disabled` "says so; it does not do so",
    so `Ask an administrator` stays clickable while the first request is in flight.

    ★ MUTANT: replace `.on_conflict_do_nothing(...)` with a plain insert and the loser raises
    `UniqueViolation`, reaching the unhandled-exception handler as a 500 — this test goes red on
    the status pair AND on the "no 500" assertion.

    Genuine concurrency is impossible on the suite's single savepointed session, and faking it
    here would fake the very race under test — so the rows really commit and are really cleaned
    up afterwards (the shared `citizen_one_test` database must not accrete one orphan per run).
    """

    async def _fresh_db():
        async with db_base.async_session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _fresh_db

    async with db_base.async_session_factory() as setup:
        user = await UserFactory.create(setup, email=f"race-{uuid.uuid4()}@rvaiglobal.com")
        await setup.commit()
        user_id = user.id

    headers = auth_headers(user)
    body = {"remarks": REMARKS}
    try:
        first, second = await asyncio.gather(
            client.post(_REQUEST, headers=headers, json=body),
            client.post(_REQUEST, headers=headers, json=body),
        )

        statuses = sorted([first.status_code, second.status_code])
        assert statuses == [201, 409], f"{first.text} | {second.text}"
        loser = first if first.status_code == 409 else second
        assert loser.json()["error"]["code"] == "already_pending"
        # The bug this guard exists for: the loser used to reach the unhandled handler.
        assert "Internal server error" not in loser.text

        async with db_base.async_session_factory() as check:
            rows = await check.scalar(
                sa.select(sa.func.count())
                .select_from(ConnectorAccessRequest)
                .where(ConnectorAccessRequest.user_id == user_id)
            )
            assert rows == 1
    finally:
        async with db_base.async_session_factory() as cleanup:
            await cleanup.execute(
                sa.delete(ConnectorAccessRequest).where(ConnectorAccessRequest.user_id == user_id)
            )
            await cleanup.execute(sa.delete(User).where(User.id == user_id))
            await cleanup.commit()


# --- isolation, across every action ---------------------------------------------


async def test_everything_one_citizen_does_leaves_the_other_untouched(client, db_session) -> None:
    """★ Two people, and every write this router offers driven on one of them: ask, cancel, ask
    again. The other's state never moves, their own ask still works while the first is waiting,
    and neither remark ever appears in the other's response.

    The partial unique index spans `(user_id, connector_key)`, so "one open ask" is one open ask
    PER PERSON — a constraint written on `connector_key` alone would make the second citizen's
    request collide with the first's and read as `already_pending` to somebody who never asked.
    """
    asha = await UserFactory.create(db_session, email="asha@rvaiglobal.com")
    ravi = await UserFactory.create(db_session, email="ravi@rvaiglobal.com")
    ravis_words = "The night shift handover board needs the same schedule the day board reads."

    async def state_of(user) -> dict:
        resp = await client.get(CONNECTORS_URL, headers=auth_headers(user))
        assert resp.status_code == 200, resp.text
        return resp.json()["connectors"][0]

    assert (await _ask(client, asha)).status_code == 201
    assert (await state_of(ravi))["state"] == "neverAsked"

    # Ravi asks while Asha is still waiting: two open asks, one each.
    assert (await _ask(client, ravi, ravis_words)).status_code == 201
    assert (await state_of(asha))["state"] == "pending"
    assert (await state_of(ravi))["state"] == "pending"

    # Asha cancels and asks again; Ravi's request is untouched by all of it.
    assert (await client.post(_CANCEL, headers=auth_headers(asha))).status_code == 200
    assert (await state_of(ravi))["state"] == "pending"
    assert (await _ask(client, asha)).status_code == 201
    assert (await state_of(ravi))["state"] == "pending"

    # Neither person's words reach the other's payload — the remark is written for an
    # administrator and this surface carries no `requesterRemarks` at all.
    asha_body = (await client.get(CONNECTORS_URL, headers=auth_headers(asha))).text
    ravi_body = (await client.get(CONNECTORS_URL, headers=auth_headers(ravi))).text
    assert ravis_words not in asha_body
    assert REMARKS not in ravi_body

    assert [row.status for row in await _rows(db_session, ravi.id)] == [
        ConnectorRequestStatus.PENDING
    ]
