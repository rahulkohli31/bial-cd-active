"""`POST /v1/admin/connector-requests/{id}/approve` and `.../decline` — one person deciding
about another.

SEVEN CLAIMS THIS FILE EXISTS FOR:

1. **The guarded UPDATE is the whole gate.** Two administrators opening the same queue and
   clicking within a second of each other is ordinary, not exotic. The second one is refused,
   the first one's decision, remark and name survive intact, and no second audit row is written.
   *Mutant: drop `status = 'pending'` from the WHERE clause and the race test goes red.*
2. **The zero-row branch has THREE outcomes, not one.** A guarded update reports how many rows
   moved, never why none did. A citizen who cancels between the administrator's render and their
   click leaves a row whose decision fields are BOTH null, so the race sentence would name nobody
   at no time — that row gets its own code and its own sentence, and no null reaches the wire.
3. **The audit row and the state change share ONE transaction and ONE commit.** `append_audit`
   flushes and never commits, so a decision whose record could not be written is not made.
4. **The trail carries ids, never the words.** The decline remark's durable home is the request
   row, which both this queue and the citizen read it from; a copy in `detail` would be a second
   emitter of one sentence.
5. **Approving your own request is allowed and RECORDED** as `connector:approve:self`. ADR-0005
   records the missing separation of duties at this gate as an accepted consequence, so the
   answer is a trail that can be queried rather than a refusal.
6. **An approval stores no remark at all** (R10, the `AdminReview` board's largest departure) and
   a decline requires one under the shipped 5-50 word rule — asserted on each refusal's SENTENCE,
   because four distinct refusals share one status code.
7. **Both routes are super-admin only AND behind CSRF**, the second deliberately diverging from
   `admin/router.py`, which declares `RequireCsrf` zero times.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import pytest
import sqlalchemy as sa

from src.api.v1.admin import connectors as admin_connectors
from src.db.models.audit import AuditLog
from src.db.models.connector_access import ConnectorAccessRequest, ConnectorRequestStatus
from src.db.models.user import User
from src.services.audit.log import append_audit
from tests.api.v1.connectors.conftest import (
    CONNECTORS_URL,
    DECLINE_REMARKS,
    KEY,
    REMARKS,
    auth_headers,
    seed_request,
)
from tests.factories import UserFactory

QUEUE = "/v1/admin/connector-requests"


def _approve(request_id: uuid.UUID) -> str:
    return f"{QUEUE}/{request_id}/approve"


def _decline(request_id: uuid.UUID) -> str:
    return f"{QUEUE}/{request_id}/decline"


async def _admin(db, **overrides: Any) -> User:
    """The first super-admin. `.env.test` puts `admin@bial.com` and `superadmin@bial.com` on
    `SUPERADMIN_EMAILS`, and the role is COMPUTED from that allowlist — BIAL runs two."""
    return await UserFactory.create(db, email="admin@bial.com", **overrides)


async def _other_admin(db, **overrides: Any) -> User:
    return await UserFactory.create(db, email="superadmin@bial.com", **overrides)


async def _waiting_request(db, **user_overrides: Any) -> tuple[User, ConnectorAccessRequest]:
    person = await UserFactory.create(db, **user_overrides)
    return person, await seed_request(db, person.id, ConnectorRequestStatus.PENDING)


async def _stored(db, request_id: uuid.UUID) -> Any:
    """The row as the DATABASE holds it. Column selects, never the entity: the route writes
    through a Core UPDATE, which does not synchronise the session's identity map, so an entity
    read here could hand back a stale in-session copy of exactly the row under test."""
    return (
        await db.execute(
            sa.select(
                ConnectorAccessRequest.status,
                ConnectorAccessRequest.decided_by_id,
                ConnectorAccessRequest.decided_at,
                ConnectorAccessRequest.decision_remarks,
                ConnectorAccessRequest.requester_remarks,
            ).where(ConnectorAccessRequest.id == request_id)
        )
    ).one()


async def _audit_rows(db) -> list[AuditLog]:
    return list((await db.execute(sa.select(AuditLog).order_by(AuditLog.id))).scalars())


def _refusal(resp) -> tuple[str, str]:
    """The `{"error": {...}}` envelope's code and message — the shape the SPA branches on."""
    error = resp.json()["error"]
    return error["code"], error["message"]


def _field_error(resp) -> tuple[str, str]:
    """The 422 body's discriminator and its sentence. `validation_exception_handler` keeps
    `type` / `loc` / `msg`, drops `input` / `ctx`, and strips Pydantic's `Value error, ` prefix —
    so `msg` is product copy and is asserted as such."""
    detail = resp.json()["detail"][0]
    return detail["type"], detail["msg"]


def _words(n: int) -> str:
    return " ".join(f"w{i}" for i in range(n))


# --- approving ------------------------------------------------------------------


async def test_approving_stamps_the_decision_and_the_citizen_reads_it_at_once(
    client, db_session
) -> None:
    """★ THE HAPPY PATH, END TO END. The row moves to `approved` with the decider and the time
    stamped, exactly one `connector:approve` audit row is written, and the SUBJECT's own
    `GET /v1/connectors` reads `approved` on the next request — no second write, no cache, no
    step in between."""
    admin = await _admin(db_session, display_name="Rahul Menon")
    person, request = await _waiting_request(db_session)

    resp = await client.post(_approve(request.id), headers=auth_headers(admin))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["requestId"] == str(request.id)
    assert body["userId"] == str(person.id)
    assert body["connectorKey"] == KEY
    assert body["status"] == "approved"
    assert body["decidedAt"] is not None

    row = await _stored(db_session, request.id)
    assert row.status is ConnectorRequestStatus.APPROVED
    assert row.decided_by_id == admin.id
    assert row.decided_at is not None
    # AN APPROVAL STORES NO REMARK (R10). `null` here is correct, not a missing write.
    assert row.decision_remarks is None
    # And the citizen's own words survive the decision untouched.
    assert row.requester_remarks == REMARKS

    audited = await _audit_rows(db_session)
    assert [entry.action for entry in audited] == ["connector:approve"]

    mine = await client.get(CONNECTORS_URL, headers=auth_headers(person))
    assert mine.status_code == 200, mine.text
    entry = mine.json()["connectors"][0]
    assert entry["state"] == "approved"
    assert entry["approvedByName"] == "Rahul Menon"


async def test_the_audit_row_carries_ids_and_never_the_words(client, db_session) -> None:
    """★ THE TRAIL'S SHAPE. `resource_type` / `resource_id` / an ids-only `detail`. The decline
    remark is NOT in there: unlike `app:delete`'s reason — whose subject is destroyed, leaving
    the audit row its only durable home — this remark already lives on the request row, which
    the queue reads and the citizen is quoted verbatim. Two homes for one sentence is two things
    that can disagree."""
    admin = await _admin(db_session)
    person, request = await _waiting_request(db_session)

    resp = await client.post(
        _decline(request.id), headers=auth_headers(admin), json={"remarks": DECLINE_REMARKS}
    )

    assert resp.status_code == 200, resp.text
    entry = (await _audit_rows(db_session))[0]
    assert entry.actor_id == admin.id
    assert entry.action == "connector:decline"
    assert entry.resource_type == "connector_request"
    assert entry.resource_id == str(request.id)
    assert entry.detail == {"connectorKey": KEY, "userId": str(person.id)}
    # The words are on the row, and NOWHERE in the trail.
    assert DECLINE_REMARKS not in str(entry.detail)


async def test_a_super_admin_approving_their_own_request_is_recorded_as_such(
    client, db_session
) -> None:
    """★ ADR-0005'S RECORDED CONSEQUENCE, made queryable. RBAC has two computed roles and no
    concept of a second approver, so a refusal here would leave an administrator unable to reach
    the data they administer. The answer is a distinguishable action word, following the app
    registry's own `approve:self` precedent — both rows carry identical `detail`, so "list every
    self-approval" is one predicate.

    Mutation check: drop the `:self` suffix and this goes red; the plain `connector:approve`
    assertion in the test below is what stops the suffix being added unconditionally."""
    admin = await _admin(db_session)
    request = await seed_request(db_session, admin.id, ConnectorRequestStatus.PENDING)

    resp = await client.post(_approve(request.id), headers=auth_headers(admin))

    assert resp.status_code == 200, resp.text
    assert [entry.action for entry in await _audit_rows(db_session)] == ["connector:approve:self"]


async def test_declining_your_own_request_keeps_the_plain_action_word(client, db_session) -> None:
    """ONLY APPROVE HAS THE VARIANT, exactly as the app registry has `approve:self` and no
    `reject:self`. Refusing yourself grants nothing, so there is nothing for a reviewer to query,
    and an action word that exists only to be symmetrical is one more thing to look up."""
    admin = await _admin(db_session)
    request = await seed_request(db_session, admin.id, ConnectorRequestStatus.PENDING)

    resp = await client.post(
        _decline(request.id), headers=auth_headers(admin), json={"remarks": DECLINE_REMARKS}
    )

    assert resp.status_code == 200, resp.text
    assert [entry.action for entry in await _audit_rows(db_session)] == ["connector:decline"]


# --- declining ------------------------------------------------------------------


async def test_declining_stores_the_remark_and_the_citizen_reads_it_verbatim(
    client, db_session
) -> None:
    """★ THE OTHER HAPPY PATH. The administrator's words are the whole of what a refused person
    is told — `Ask again` is not built, so a decline has no path back and no second explanation.
    They reach the citizen unaltered: not trimmed to a preview, not re-worded, not summarised."""
    admin = await _admin(db_session, display_name="Rahul Menon")
    person, request = await _waiting_request(db_session)

    resp = await client.post(
        _decline(request.id), headers=auth_headers(admin), json={"remarks": DECLINE_REMARKS}
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "declined"

    row = await _stored(db_session, request.id)
    assert row.status is ConnectorRequestStatus.DECLINED
    assert row.decision_remarks == DECLINE_REMARKS
    assert row.decided_by_id == admin.id

    mine = await client.get(CONNECTORS_URL, headers=auth_headers(person))
    entry = mine.json()["connectors"][0]
    assert entry["state"] == "declined"
    assert entry["decisionRemarks"] == DECLINE_REMARKS
    assert entry["decidedByName"] == "Rahul Menon"


async def test_the_remark_is_stored_trimmed(client, db_session) -> None:
    """The validator returns the trimmed value, so the column never carries the padding a
    textarea collects — and the citizen is never shown a leading blank line."""
    admin = await _admin(db_session)
    _person, request = await _waiting_request(db_session)

    resp = await client.post(
        _decline(request.id),
        headers=auth_headers(admin),
        json={"remarks": f"  {DECLINE_REMARKS}\n"},
    )

    assert resp.status_code == 200, resp.text
    assert (await _stored(db_session, request.id)).decision_remarks == DECLINE_REMARKS


@pytest.mark.parametrize(
    ("remarks", "expected"),
    [
        ("", "Say why you are declining this request."),
        ("     ", "Say why you are declining this request."),
        ("\t\n  ", "Say why you are declining this request."),
        (_words(4), "Give a little more detail — at least 5 words."),
        (_words(51), "Keep the reason under 50 words."),
    ],
    ids=["empty", "spaces", "whitespace-run", "four-words", "fifty-one-words"],
)
async def test_a_decline_remark_outside_the_rule_is_refused_with_its_own_sentence(
    client, db_session, remarks, expected
) -> None:
    """The shipped 5-50 word stated-reason rule (owner decision D1), bound to THIS surface's own
    empty-field sentence. Asserted on the error's `type` AND its message, never on `422` alone:
    four distinct refusals share one status code and "it 422'd" cannot tell them apart.

    The empty-field sentence is this route's, NOT the project delete's or the app destroy's —
    `clean_stated_reason` takes it as a parameter precisely because "Say why you are deleting
    this project." is not a sentence a decline could reuse."""
    admin = await _admin(db_session)
    _person, request = await _waiting_request(db_session)

    resp = await client.post(
        _decline(request.id), headers=auth_headers(admin), json={"remarks": remarks}
    )

    assert resp.status_code == 422, resp.text
    kind, message = _field_error(resp)
    assert kind == "value_error"
    assert message == expected
    # Written for a person: the machine prefix comes off at the boundary.
    assert "Value error" not in resp.text
    # Nothing was written, so the request is still waiting on somebody.
    assert (await _stored(db_session, request.id)).status is ConnectorRequestStatus.PENDING


async def test_a_long_paste_gets_the_character_sentence_not_the_word_one(
    client, db_session
) -> None:
    """★ THE BACKSTOP'S OWN CASE. Forty words of URLs or a non-English script can clear 2,000
    characters while genuinely under fifty words. Telling that administrator to "keep the reason
    under 50 words" is telling them to get under a bound they are already inside."""
    admin = await _admin(db_session)
    _person, request = await _waiting_request(db_session)
    paste = " ".join("u" * 50 for _ in range(40))  # 40 words, 2039 characters
    assert len(paste) > 2000 and len(paste.split()) < 50

    resp = await client.post(
        _decline(request.id), headers=auth_headers(admin), json={"remarks": paste}
    )

    assert resp.status_code == 422, resp.text
    assert _field_error(resp) == (
        "value_error",
        "That reason is too long. Keep it under 2000 characters.",
    )


async def test_a_missing_remarks_field_is_refused(client, db_session) -> None:
    admin = await _admin(db_session)
    _person, request = await _waiting_request(db_session)

    resp = await client.post(_decline(request.id), headers=auth_headers(admin), json={})

    assert resp.status_code == 422, resp.text
    assert _field_error(resp)[0] == "missing"
    assert (await _stored(db_session, request.id)).status is ConnectorRequestStatus.PENDING


@pytest.mark.parametrize("n", [5, 27, 50], ids=["floor", "middle", "ceiling"])
async def test_a_remark_inside_the_rule_is_accepted(client, db_session, n) -> None:
    """Both bounds are INCLUSIVE — a rule whose stated limit is itself refused is a rule the
    live counter in the browser would disagree with."""
    admin = await _admin(db_session)
    _person, request = await _waiting_request(db_session)

    resp = await client.post(
        _decline(request.id), headers=auth_headers(admin), json={"remarks": _words(n)}
    )

    assert resp.status_code == 200, resp.text


async def test_approve_ignores_a_body_and_stores_nothing_from_it(client, db_session) -> None:
    """THERE IS NO APPROVE BODY. The route declares none, so an extra key is ignored the way
    Pydantic ignores any unknown key — and nothing a caller sends can put words on an approved
    row, which is the state R10 says has none."""
    admin = await _admin(db_session)
    _person, request = await _waiting_request(db_session)

    resp = await client.post(
        _approve(request.id), headers=auth_headers(admin), json={"remarks": "smuggled"}
    )

    assert resp.status_code == 200, resp.text
    assert (await _stored(db_session, request.id)).decision_remarks is None


# --- the races ------------------------------------------------------------------


async def test_the_second_administrator_is_told_who_decided_and_when(client, db_session) -> None:
    """★ THE RACE, AND THE MUTANT THIS FILE IS NAMED FOR. BIAL runs two super-admins and they
    read the same queue. Without `status = 'pending'` in the guarded UPDATE's WHERE clause the
    second click would overwrite the first administrator's decision, their remark and their
    name — and write a second audit row claiming it happened twice.

    The refusal names WHO and carries WHEN: the sentence has the name, `error.detail` has the
    instant, because this codebase formats no human-readable date on the server and the console
    rendering it already formats every other date on that screen.

    Mutation check: drop the status predicate and this goes red on the status code, on the
    remark, and on the audit count — three ways."""
    first = await _admin(db_session, display_name="Rahul Menon")
    second = await _other_admin(db_session, display_name="Anant Gupta")
    _person, request = await _waiting_request(db_session)

    won = await client.post(
        _decline(request.id), headers=auth_headers(first), json={"remarks": DECLINE_REMARKS}
    )
    assert won.status_code == 200, won.text
    decided_at = (await _stored(db_session, request.id)).decided_at

    lost = await client.post(_approve(request.id), headers=auth_headers(second))

    assert lost.status_code == 409, lost.text
    code, message = _refusal(lost)
    assert code == "already_decided"
    # NAMES WHAT THE WINNER ACTUALLY DID, which is the opposite of what the loser was about to.
    assert message == "Rahul Menon has already declined this request."
    detail = lost.json()["error"]["detail"]
    assert detail == {
        "status": "declined",
        "decidedByName": "Rahul Menon",
        # SPELLED EXACTLY AS THE WINNING DECISION'S OWN BODY SPELLED IT, asserted against that
        # body rather than against a format string: this feature emits one instant format, and a
        # literal here would go green on a refusal that answered `+00:00` where every response
        # answers `Z` — two spellings of one value, one client-side date parser away from a bug
        # report. Pinned to the emitter, so the rule cannot be satisfied by coincidence.
        "decidedAt": won.json()["decidedAt"],
    }
    # And it is the instant actually STORED, not merely a well-formed string agreeing with
    # itself: without this, both emitters could be wrong together.
    assert datetime.fromisoformat(detail["decidedAt"]) == decided_at

    row = await _stored(db_session, request.id)
    assert row.status is ConnectorRequestStatus.DECLINED
    assert row.decided_by_id == first.id
    assert row.decision_remarks == DECLINE_REMARKS
    # ONE decision, ONE audit row. A second entry would claim an approval that never happened.
    assert [entry.action for entry in await _audit_rows(db_session)] == ["connector:decline"]


async def test_a_citizen_cancelling_first_gets_its_own_refusal(client, db_session) -> None:
    """★ THE CASE THE DECIDED READ CANNOT SERVE. A citizen who withdraws between the
    administrator's render and their click leaves a row with BOTH decision fields null, so the
    race sentence above would render a nameless decider at no time at all.

    The code is `request_cancelled`, and it is deliberately NOT called `withdrawn`: that word is
    reserved for the person state `ConnectorStates` draws fifth — the connector's owner revoking
    the platform's access — which this pass keeps out of the enum, the resolver and the scope
    table. A wire-facing `withdrawn` for an unrelated concept would hand the next reviewer
    grepping for that state a false hit.

    Mutation check: route this through the decided branch and the assertion that no null reaches
    the wire goes red."""
    admin = await _admin(db_session)
    person, request = await _waiting_request(db_session)
    # Through the citizen's OWN route, not a hand-written UPDATE: the race this reproduces is the
    # product's, and the row has to reach `cancelled` the way a person actually gets it there.
    withdrawn = await client.post(f"{CONNECTORS_URL}/{KEY}/cancel", headers=auth_headers(person))
    assert withdrawn.status_code == 200, withdrawn.text

    resp = await client.post(_approve(request.id), headers=auth_headers(admin))

    assert resp.status_code == 409, resp.text
    assert _refusal(resp) == (
        "request_cancelled",
        "This request was cancelled before you decided it.",
    )
    # NO NULL NAME AND NO NULL DATE ON THE WIRE — there is nothing measured to hand over, so the
    # envelope carries no `detail` at all rather than one full of holes.
    assert resp.json()["error"] == {
        "message": "This request was cancelled before you decided it.",
        "code": "request_cancelled",
    }
    assert (await _stored(db_session, request.id)).status is ConnectorRequestStatus.CANCELLED
    assert await _audit_rows(db_session) == []


@pytest.mark.parametrize(
    "decided", [ConnectorRequestStatus.APPROVED, ConnectorRequestStatus.DECLINED]
)
async def test_a_decision_cannot_be_revisited(client, db_session, decided) -> None:
    """`declined` is terminal for this pass and `approved` is not re-decidable: this route gives
    an administrator no way to change their mind, server-side as well as on screen. The refusal
    leaves the stored decision exactly as it was."""
    first = await _admin(db_session, display_name="Rahul Menon")
    second = await _other_admin(db_session)
    person = await UserFactory.create(db_session)
    request = await seed_request(
        db_session,
        person.id,
        decided,
        decided_by_id=first.id,
        decided_at=sa.func.now(),
    )

    resp = await client.post(
        _decline(request.id), headers=auth_headers(second), json={"remarks": DECLINE_REMARKS}
    )

    assert resp.status_code == 409, resp.text
    assert _refusal(resp)[0] == "already_decided"
    assert (await _stored(db_session, request.id)).status is decided


async def test_a_decision_whose_administrator_is_gone_still_refuses_by_name(
    client, db_session
) -> None:
    """`decided_by_id` is `ON DELETE SET NULL`, so a decision outlives its decider — and the
    refusal then has nobody to name. `An administrator` rather than a hole where a person should
    be, and `decidedByName: null` in the detail so a client can tell the two apart."""
    departing = await _admin(db_session, display_name="Gone Already")
    remaining = await _other_admin(db_session)
    person = await UserFactory.create(db_session)
    request = await seed_request(
        db_session,
        person.id,
        ConnectorRequestStatus.APPROVED,
        decided_by_id=departing.id,
        decided_at=sa.func.now(),
    )
    await db_session.delete(departing)
    await db_session.flush()

    resp = await client.post(_approve(request.id), headers=auth_headers(remaining))

    assert resp.status_code == 409, resp.text
    code, message = _refusal(resp)
    assert code == "already_decided"
    assert message == "An administrator has already approved this request."
    assert resp.json()["error"]["detail"]["decidedByName"] is None


@pytest.mark.parametrize("route", [_approve, _decline], ids=["approve", "decline"])
async def test_a_request_that_does_not_exist_is_a_404(client, db_session, route) -> None:
    admin = await _admin(db_session)

    resp = await client.post(
        route(uuid.uuid4()), headers=auth_headers(admin), json={"remarks": DECLINE_REMARKS}
    )

    assert resp.status_code == 404, resp.text
    assert _refusal(resp) == ("request_not_found", "Request not found.")


# --- one transaction ------------------------------------------------------------


async def test_the_audit_row_is_written_before_the_single_commit(
    client, db_session, monkeypatch
) -> None:
    """★ THE ORDERING, ASSERTED RATHER THAN DOCUMENTED. `append_audit` flushes and never commits,
    so the accountability row and the state change reach the database together or not at all.

    Mutation check: commit before appending the audit row and `events` reads
    `["commit", "audit"]`; commit twice and the list grows."""
    events: list[str] = []
    real_commit = db_session.commit

    async def _recording_commit() -> None:
        events.append("commit")
        await real_commit()

    async def _recording_append(*args: Any, **kwargs: Any) -> Any:
        events.append("audit")
        # Called through its OWN module, not through the route module's binding: the latter is
        # the name being patched, and `mypy --strict` reads it as a non-exported attribute.
        return await append_audit(*args, **kwargs)

    monkeypatch.setattr(db_session, "commit", _recording_commit)
    monkeypatch.setattr(admin_connectors, "append_audit", _recording_append)

    admin = await _admin(db_session)
    _person, request = await _waiting_request(db_session)

    resp = await client.post(_approve(request.id), headers=auth_headers(admin))

    assert resp.status_code == 200, resp.text
    assert events == ["audit", "commit"]


@pytest.mark.route_rollback
async def test_a_failed_audit_write_leaves_the_decision_unmade(
    client, db_session, monkeypatch
) -> None:
    """★ THE INTEGRATION CLAIM. Force the trail's write to fail and the status must not move:
    the two share one transaction, and a decision nobody could record is a decision that did not
    happen.

    `route_rollback` joins the fixture's transaction by SAVEPOINT so the seeds below can be
    released with a commit of their own and survive the rollback — which is performed here in
    place of `get_db`'s, since the test override of that dependency yields the shared session
    without the production wrapper's `except: rollback`."""
    admin = await _admin(db_session)
    _person, request = await _waiting_request(db_session)
    # The ids are read out as PLAIN VALUES before anything rolls back. A rollback EXPIRES the
    # session's instances, so `request.id` afterwards is a lazy load — synchronous IO inside a
    # coroutine, which asyncpg answers with `MissingGreenlet` rather than the row.
    request_id, admin_headers = request.id, auth_headers(admin)
    await db_session.commit()

    async def _the_trail_is_down(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(admin_connectors, "append_audit", _the_trail_is_down)

    with pytest.raises(RuntimeError, match="audit unavailable"):
        await client.post(_approve(request_id), headers=admin_headers)

    await db_session.rollback()

    # The liveness half: the row is still THERE, still waiting, and still asking — a rollback
    # that took the fixtures with it would satisfy the status assertion for the wrong reason.
    row = await _stored(db_session, request_id)
    assert row.status is ConnectorRequestStatus.PENDING
    assert row.decided_by_id is None
    assert row.decided_at is None
    assert row.requester_remarks == REMARKS
    assert await _audit_rows(db_session) == []


# --- the gates ------------------------------------------------------------------


@pytest.mark.parametrize("route", [_approve, _decline], ids=["approve", "decline"])
@pytest.mark.parametrize(
    ("email", "allowed"),
    [("nobody@rvaiglobal.com", False), ("admin@bial.com", True)],
    ids=["citizen", "super-admin"],
)
async def test_every_decision_here_is_super_admin_only(
    client, db_session, route, email, allowed
) -> None:
    """★ RBAC ACROSS BOTH COMPUTED ROLES, on both writes. A citizen reaching these would grant
    themselves access to operational data — the exact thing the gate exists for.

    The caller carries a VALID CSRF token, so the refusal can only come from the RBAC gate; it
    is asserted on the gate's own sentence, not merely on 403, because the CSRF gate answers 403
    too and the two must not be able to stand in for each other."""
    caller = await UserFactory.create(db_session, email=email)
    _person, request = await _waiting_request(db_session)

    resp = await client.post(
        route(request.id), headers=auth_headers(caller), json={"remarks": DECLINE_REMARKS}
    )

    if allowed:
        assert resp.status_code == 200, resp.text
    else:
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"] == "Super-admin privileges required."
        assert (await _stored(db_session, request.id)).status is ConnectorRequestStatus.PENDING


@pytest.mark.parametrize("route", [_approve, _decline], ids=["approve", "decline"])
async def test_a_decision_without_the_csrf_header_is_refused(client, db_session, route) -> None:
    """★ THE DELIBERATE DIVERGENCE. `admin/router.py` declares `RequireCsrf` zero times, so an
    implementer following the neighbouring precedent would ship these two behind nothing but a
    session cookie and `SameSite=Lax` — which `deps_csrf.py` itself calls "a second line, not
    the only one". A forged approval of a data-access grant is not a defect worth inheriting.

    The request arrives exactly as a cross-site form post would: the cookie rides along, the
    header does not.

    Mutation check: remove `dependencies=[RequireCsrf]` and this goes red."""
    admin = await _admin(db_session)
    _person, request = await _waiting_request(db_session)

    resp = await client.post(
        route(request.id),
        headers=auth_headers(admin, with_csrf=False),
        json={"remarks": DECLINE_REMARKS},
    )

    assert resp.status_code == 403, resp.text
    assert _refusal(resp) == ("csrf_failed", "CSRF check failed.")
    assert (await _stored(db_session, request.id)).status is ConnectorRequestStatus.PENDING


@pytest.mark.parametrize("route", [_approve, _decline], ids=["approve", "decline"])
async def test_an_unauthenticated_caller_is_refused(client, db_session, route) -> None:
    _person, request = await _waiting_request(db_session)

    resp = await client.post(route(request.id), json={"remarks": DECLINE_REMARKS})

    assert resp.status_code in (401, 403), resp.text
    assert (await _stored(db_session, request.id)).status is ConnectorRequestStatus.PENDING
