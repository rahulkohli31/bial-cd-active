"""An app's History: one numbered version per send, each carrying only its own decision and
publish attempts, with the app's other events between them by date. Built from seeded audit rows
and deploy attempts exactly as the routes write them."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import storage_dependency, storage_or_none_dependency
from src.api.v1.admin.history import HISTORY_CAP
from src.config import settings
from src.db.models.app_registry import AppRegistry, AppStatus
from src.db.models.audit import AuditLog
from src.db.models.deployment import Deployment, DeploymentStatus
from src.db.models.user import User
from src.services.auth.session_jwt import mint_session_jwt
from tests.factories import AppRegistryFactory, UserFactory
from tests.fakes import FakeStorage

_TTL = settings.auth.access_ttl_seconds

C1 = "2e77b10" + "1" * 33
C2 = "c09a4e1" + "2" * 33
C3 = "51bd2f8" + "3" * 33
C4 = "7a3c9e0" + "4" * 33
_URL = "https://pub.example/lost-and-found"
_ADMIN = "admin@bial.com"


def _at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=UTC)


def _cookie(user: User) -> dict[str, str]:
    return {"Cookie": f"session={mint_session_jwt(user.id, user.token_version, _TTL)}"}


async def _admin(db: AsyncSession) -> User:
    return await UserFactory.create(db, email=_ADMIN)


async def _app(db: AsyncSession, owner: User | None = None, **overrides: Any) -> AppRegistry:
    owner = owner or await UserFactory.create(db, email=f"{uuid.uuid4().hex[:8]}@bialairport.com")
    return await AppRegistryFactory.create(db, user_id=owner.id, **overrides)


async def _row(
    db: AsyncSession,
    app: AppRegistry,
    action: str,
    at: datetime,
    actor: User | None = None,
    *,
    resource_id: str | None = None,
    **detail: Any,
) -> AuditLog:
    entry = AuditLog(
        actor_id=actor.id if actor else None,
        action=action,
        resource_type="app",
        resource_id=resource_id if resource_id is not None else str(app.id),
        detail=detail or None,
        created_at=at,
    )
    db.add(entry)
    await db.flush()
    return entry


def _declaration(commit: str, **answers: bool) -> dict[str, Any]:
    return {"commits": {"shipping": commit, "reviewed": commit}, "citizen": {"answers": answers}}


async def _routed(
    db: AsyncSession, app: AppRegistry, owner: User, at: datetime, commit: str, **answers: bool
) -> uuid.UUID:
    """A routed send as the route writes it: the queue's `submit` row, then the gate's row,
    both carrying the submission id."""
    submission = uuid.uuid4()
    await _row(db, app, "submit", at, owner, submissionId=str(submission), commitSha=commit)
    await _row(
        db,
        app,
        "publish_gate",
        at,
        owner,
        appId=str(app.id),
        email=owner.email,
        decision="routed",
        rule="hard_block",
        submissionId=str(submission),
        commitSha=commit,
        declaration=_declaration(commit, **answers),
    )
    return submission


async def _attempt(
    db: AsyncSession,
    app: AppRegistry,
    started: datetime,
    finished: datetime | None,
    status: DeploymentStatus = DeploymentStatus.SUCCEEDED,
    head_sha: str | None = None,
    **fields: Any,
) -> Deployment:
    published = status is DeploymentStatus.SUCCEEDED
    row = Deployment(
        app_id=app.id,
        user_id=app.user_id,
        status=status,
        head_sha=head_sha,
        image_digest="sha256:" + "ab" * 32 if published else None,
        url=_URL if published else None,
        created_at=started,
        finished_at=finished,
        **fields,
    )
    db.add(row)
    await db.flush()
    return row


async def _published(
    db: AsyncSession,
    app: AppRegistry,
    owner: User,
    at: datetime,
    commit: str,
    finished: datetime | None,
    status: DeploymentStatus = DeploymentStatus.SUCCEEDED,
) -> Deployment:
    """A send the gate published by itself, and the attempt it started."""
    attempt = await _attempt(db, app, at, finished, status, head_sha=commit)
    await _row(
        db,
        app,
        "publish_gate",
        at,
        owner,
        appId=str(app.id),
        email=owner.email,
        decision="published",
        rule="all_clear",
        deploymentId=str(attempt.id),
        declaration={"version": 2, "commit": commit},
    )
    return attempt


async def _approve(
    db: AsyncSession,
    app: AppRegistry,
    admin: User,
    at: datetime,
    submission: uuid.UUID,
    commit: str,
    attempt: Deployment | None,
) -> None:
    publishing = (
        {"publishing": "started", "deploymentId": str(attempt.id)}
        if attempt is not None
        else {"publishing": "not_started", "reason": "publish_in_flight"}
    )
    await _row(
        db, app, "approve", at, admin, submissionId=str(submission), commitSha=commit, **publishing
    )


async def _try_again(db: AsyncSession, app: AppRegistry, owner: User, attempt: Deployment) -> None:
    """The owner republishing the approved copy: a gate row, but not a send."""
    await _row(
        db,
        app,
        "publish_gate",
        attempt.created_at,
        owner,
        appId=str(app.id),
        email=owner.email,
        decision="published",
        rule="approved_override",
        deploymentId=str(attempt.id),
    )


async def _history(client, app: AppRegistry, admin: User) -> dict[str, Any]:
    resp = await client.get(f"/v1/admin/apps/{app.id}/history", headers=_cookie(admin))
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


def _versions(body: dict[str, Any]) -> list[dict[str, Any]]:
    return [entry for entry in body["entries"] if entry["kind"] == "version"]


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


async def _lost_and_found(db: AsyncSession) -> tuple[AppRegistry, User]:
    """Four sends, a disable and re-enable, and a restart, as the routes write them."""
    owner = await UserFactory.create(db, email="kavya.n@bialairport.com")
    admin = await _admin(db)
    app = await _app(db, owner, status=AppStatus.APPROVED)

    s1 = await _routed(db, app, owner, _at(4, 17, 15), C1, personal_information=False)
    first = await _attempt(
        db, app, _at(5, 14), _at(5, 14, 20), DeploymentStatus.FAILED, failure_code="build_failed"
    )
    await _approve(db, app, admin, _at(5, 14), s1, C1, first)
    second = await _attempt(db, app, _at(5, 14, 50), _at(5, 15, 2), head_sha=C1)
    await _try_again(db, app, owner, second)

    await _row(db, app, "disable", _at(13, 18, 40), admin)
    await _row(db, app, "enable", _at(14, 9, 2), admin)

    s2 = await _routed(db, app, owner, _at(15, 15, 40), C2, personal_information=True)
    await _row(
        db,
        app,
        "reject",
        _at(16, 10),
        admin,
        submissionId=str(s2),
        note="Remove the passport number field before this goes live.",
    )

    s3 = await _routed(db, app, owner, _at(17, 11, 20), C3, financial_data=True)
    third = await _attempt(db, app, _at(18, 9, 5), _at(18, 9, 12), head_sha=C3)
    await _approve(db, app, admin, _at(18, 9, 5), s3, C3, third)

    restart = await _attempt(db, app, _at(23, 10, 12), _at(23, 10, 20), head_sha=C3)
    await _row(
        db, app, "restart", _at(23, 10, 12), owner, deploymentId=str(restart.id), headSha=C3
    )

    await _published(db, app, owner, _at(25, 16, 2), C4, _at(25, 16, 40))
    return app, admin


async def test_lost_and_found_reads_newest_first_with_its_events_between(
    client, db_session
) -> None:
    app, admin = await _lost_and_found(db_session)

    body = await _history(client, app, admin)

    timeline = [
        (entry["number"], entry["state"]) if entry["kind"] == "version" else entry["action"]
        for entry in body["entries"]
    ]
    assert timeline == [
        (4, "live"),
        "restart",
        (3, "replaced"),
        (2, "rejected"),
        "disable",
        (1, "replaced"),
    ]
    assert body["live"] == {"number": 4, "commitSha": C4, "since": _iso(_at(25, 16, 40))}
    assert body["liveUrl"] == _URL
    assert body["truncated"] is False

    v4, v3, v2, v1 = _versions(body)
    assert v4["decision"] == {"kind": "published", "by": None, "at": None, "note": None}
    assert v4["publishedAt"] == _iso(_at(25, 16, 40))
    assert v4["sentBy"] == "kavya.n@bialairport.com"

    assert v3["decision"]["kind"] == "approved"
    assert v3["decision"]["by"] == _ADMIN
    assert v3["decision"]["at"] == _iso(_at(18, 9, 5))
    assert v3["publishedAt"] == _iso(_at(18, 9, 12))
    assert (v3["replacedBy"], v3["replacedAt"]) == (4, _iso(_at(25, 16, 40)))

    assert v2["decision"] == {
        "kind": "rejected",
        "by": _ADMIN,
        "at": _iso(_at(16, 10)),
        "note": "Remove the passport number field before this goes live.",
    }
    assert v2["attempts"] == []

    assert [a["status"] for a in v1["attempts"]] == ["failed", "succeeded"]
    assert v1["attempts"][0]["failureCode"] == "build_failed"
    assert v1["publishedAt"] == _iso(_at(5, 15, 2))
    assert (v1["replacedBy"], v1["replacedAt"]) == (3, _iso(_at(18, 9, 12)))

    restart, disable = (entry for entry in body["entries"] if entry["kind"] == "event")
    assert (restart["by"], restart["at"]) == ("kavya.n@bialairport.com", _iso(_at(23, 10, 12)))
    assert disable["by"] == _ADMIN
    assert (disable["at"], disable["reenabledAt"]) == (_iso(_at(13, 18, 40)), _iso(_at(14, 9, 2)))


async def test_each_version_carries_its_own_declaration(client, db_session) -> None:
    app, admin = await _lost_and_found(db_session)

    v4, v3, v2, v1 = _versions(await _history(client, app, admin))

    assert v2["declaration"]["citizen"]["answers"] == {"personal_information": True}
    assert v3["declaration"]["citizen"]["answers"] == {"financial_data": True}
    assert (v1["commitSha"], v2["commitSha"], v3["commitSha"], v4["commitSha"]) == (C1, C2, C3, C4)


async def test_an_unchanged_app_sent_withdrawn_and_sent_again_is_two_versions(
    client, db_session
) -> None:
    owner = await UserFactory.create(db_session)
    admin = await _admin(db_session)
    app = await _app(db_session, owner)
    first = await _routed(db_session, app, owner, _at(20, 9), C1)
    await _row(db_session, app, "withdraw", _at(20, 10), owner, submissionId=str(first))
    second = await _routed(db_session, app, owner, _at(20, 11), C1)
    app.status = AppStatus.PENDING
    app.source_submission_id = second
    await db_session.flush()

    versions = _versions(await _history(client, app, admin))

    assert [(v["number"], v["state"], v["decision"]["kind"]) for v in versions] == [
        (2, "waiting", "waiting"),
        (1, "withdrawn", "withdrawn"),
    ]
    assert versions[1]["decision"]["by"] == owner.email


async def test_live_now_sits_on_the_later_send_of_the_same_code(client, db_session) -> None:
    owner = await UserFactory.create(db_session)
    admin = await _admin(db_session)
    app = await _app(db_session, owner)
    first = await _routed(db_session, app, owner, _at(20, 9), C1)
    await _row(db_session, app, "withdraw", _at(20, 10), owner, submissionId=str(first))
    await _published(db_session, app, owner, _at(20, 11), C1, _at(20, 11, 30))

    body = await _history(client, app, admin)
    v2, v1 = _versions(body)

    assert (v2["number"], v2["state"], len(v2["attempts"])) == (2, "live", 1)
    assert (v1["number"], v1["state"], v1["attempts"]) == (1, "withdrawn", [])
    assert v1["publishedAt"] is None
    assert body["live"]["number"] == 2


async def test_a_routed_send_is_one_version_not_two(client, db_session) -> None:
    owner = await UserFactory.create(db_session)
    admin = await _admin(db_session)
    app = await _app(db_session, owner)
    submission = await _routed(db_session, app, owner, _at(20, 9), C1)
    app.status = AppStatus.PENDING
    app.source_submission_id = submission
    await db_session.flush()

    versions = _versions(await _history(client, app, admin))

    assert [(v["number"], v["submissionId"], v["state"]) for v in versions] == [
        (1, str(submission), "waiting")
    ]


async def test_an_approved_send_retried_is_one_version_with_two_attempts(
    client, db_session
) -> None:
    owner = await UserFactory.create(db_session)
    admin = await _admin(db_session)
    app = await _app(db_session, owner, status=AppStatus.APPROVED)
    submission = await _routed(db_session, app, owner, _at(20, 9), C1)
    failed = await _attempt(
        db_session,
        app,
        _at(20, 10),
        _at(20, 10, 5),
        DeploymentStatus.FAILED,
        failure_code="build_failed",
    )
    await _approve(db_session, app, admin, _at(20, 10), submission, C1, failed)
    retried = await _attempt(db_session, app, _at(20, 12), _at(20, 12, 9), head_sha=C1)
    await _try_again(db_session, app, owner, retried)

    versions = _versions(await _history(client, app, admin))

    assert len(versions) == 1
    assert versions[0]["state"] == "live"
    assert [a["status"] for a in versions[0]["attempts"]] == ["failed", "succeeded"]


async def test_refused_decisions_and_unsent_work_are_not_versions(client, db_session) -> None:
    owner = await UserFactory.create(db_session)
    admin = await _admin(db_session)
    app = await _app(db_session, owner)
    for rule in ("disabled", "pending"):
        await _row(
            db_session,
            app,
            "publish_gate",
            _at(20, 9),
            owner,
            appId=str(app.id),
            decision="refused",
            rule=rule,
        )
    await _row(db_session, app, "classification_review", _at(20, 8), owner, appId=str(app.id))
    await _published(db_session, app, owner, _at(20, 10), C1, _at(20, 10, 30))

    versions = _versions(await _history(client, app, admin))

    assert [v["number"] for v in versions] == [1]


async def test_a_legacy_app_with_only_submit_rows_lists_its_versions(client, db_session) -> None:
    owner = await UserFactory.create(db_session)
    admin = await _admin(db_session)
    app = await _app(db_session, owner, status=AppStatus.PENDING)
    older, newer = uuid.uuid4(), uuid.uuid4()
    await _row(db_session, app, "submit", _at(2, 9), owner, submissionId=str(older), commitSha=C1)
    await _row(db_session, app, "submit", _at(3, 9), owner, submissionId=str(newer), commitSha=C2)
    app.source_submission_id = newer
    await db_session.flush()

    versions = _versions(await _history(client, app, admin))

    assert [(v["number"], v["commitSha"], v["state"]) for v in versions] == [
        (2, C2, "waiting"),
        (1, C1, "not_recorded"),
    ]
    assert versions[1]["declaration"] is None


async def test_a_send_the_old_pipeline_deferred_is_one_version(client, db_session) -> None:
    owner = await UserFactory.create(db_session)
    admin = await _admin(db_session)
    app = await _app(db_session, owner, status=AppStatus.PENDING)
    published = await _attempt(db_session, app, _at(10, 9), _at(10, 9, 20), head_sha=C1)
    await _row(
        db_session,
        app,
        "publish_gate",
        _at(10, 9),
        owner,
        appId=str(app.id),
        decision="deferred_to_pipeline",
        rule="saved_over_stale_review",
        deploymentId=str(published.id),
    )
    routed = await _attempt(
        db_session,
        app,
        _at(12, 9),
        _at(12, 9, 3),
        DeploymentStatus.FAILED,
        failure_code="routed_for_review",
    )
    await _row(
        db_session,
        app,
        "publish_gate",
        _at(12, 9),
        owner,
        appId=str(app.id),
        decision="deferred_to_pipeline",
        rule="saved_over_stale_review",
        deploymentId=str(routed.id),
    )
    submission = uuid.uuid4()
    await _row(db_session, app, "submit", _at(12, 9, 3), owner, submissionId=str(submission))
    await _row(
        db_session,
        app,
        "publish_gate",
        _at(12, 9, 3),
        owner,
        appId=str(app.id),
        decision="routed",
        rule="weighted_yes",
        deploymentId=str(routed.id),
        submissionId=str(submission),
        commitSha=C2,
    )
    app.source_submission_id = submission
    await db_session.flush()

    versions = _versions(await _history(client, app, admin))

    assert [(v["number"], v["state"], len(v["attempts"])) for v in versions] == [
        (2, "waiting", 0),
        (1, "live", 1),
    ]


async def test_history_for_one_app_never_includes_another(client, db_session) -> None:
    owner = await UserFactory.create(db_session)
    admin = await _admin(db_session)
    mine = await _app(db_session, owner)
    theirs = await _app(db_session, owner)
    await _published(db_session, mine, owner, _at(20, 9), C1, _at(20, 9, 30))
    await _published(db_session, theirs, owner, _at(20, 10), C2, _at(20, 10, 30))
    await _row(db_session, theirs, "disable", _at(20, 11), admin)

    body = await _history(client, mine, admin)

    assert [(v["number"], v["commitSha"]) for v in _versions(body)] == [(1, C1)]
    assert [entry for entry in body["entries"] if entry["kind"] == "event"] == []


async def test_a_citizen_is_refused_history(client, db_session) -> None:
    owner = await UserFactory.create(db_session, email="nobody@rvaiglobal.com")
    app = await _app(db_session, owner)

    resp = await client.get(f"/v1/admin/apps/{app.id}/history", headers=_cookie(owner))

    assert resp.status_code == 403


async def test_history_of_an_unknown_app_is_not_found(client, db_session) -> None:
    admin = await _admin(db_session)

    resp = await client.get(f"/v1/admin/apps/{uuid.uuid4()}/history", headers=_cookie(admin))

    assert resp.status_code == 404


async def test_history_stops_at_its_cap_and_says_so(client, db_session) -> None:
    owner = await UserFactory.create(db_session)
    admin = await _admin(db_session)
    app = await _app(db_session, owner)
    await _published(db_session, app, owner, _at(1, 9), C1, _at(1, 9, 30))
    for minute in range(HISTORY_CAP):
        await _row(db_session, app, "restart", _at(2, 9) + timedelta(minutes=minute), owner)

    body = await _history(client, app, admin)

    assert body["truncated"] is True
    assert len(body["entries"]) == HISTORY_CAP


async def test_the_old_audit_list_is_gone(client, db_session) -> None:
    admin = await _admin(db_session)
    app = await _app(db_session)

    resp = await client.get(f"/v1/admin/apps/{app.id}/audit", headers=_cookie(admin))

    assert resp.status_code == 404


# --- the registry's live version reads the same numbering ------------------------


async def _listed(client, admin: User, app: AppRegistry) -> dict[str, Any]:
    resp = await client.get("/v1/admin/apps", headers=_cookie(admin))
    assert resp.status_code == 200, resp.text
    row = next(row for row in resp.json()["apps"] if row["appId"] == str(app.id))
    live: dict[str, Any] = row["liveVersion"]
    return live


async def test_the_registry_and_history_give_the_live_send_one_number(client, db_session) -> None:
    app, admin = await _lost_and_found(db_session)

    listed = await _listed(client, admin, app)
    history = await _history(client, app, admin)

    assert listed == history["live"]
    assert listed == {"number": 4, "commitSha": C4, "since": _iso(_at(25, 16, 40))}


async def test_since_is_when_the_send_first_went_live(client, db_session) -> None:
    owner = await UserFactory.create(db_session)
    admin = await _admin(db_session)
    app = await _app(db_session, owner, status=AppStatus.APPROVED)
    submission = await _routed(db_session, app, owner, _at(20, 9), C1)
    went_live = await _attempt(
        db_session, app, _at(20, 10), _at(20, 10, 7), head_sha=C1, unpublished_at=_at(21, 9)
    )
    await _approve(db_session, app, admin, _at(20, 10), submission, C1, went_live)
    again = await _attempt(db_session, app, _at(22, 9), _at(22, 9, 6), head_sha=C1)
    await _try_again(db_session, app, owner, again)
    restart = await _attempt(db_session, app, _at(23, 9), _at(23, 9, 4), head_sha=C1)
    await _row(db_session, app, "restart", _at(23, 9), owner, deploymentId=str(restart.id))

    listed = await _listed(client, admin, app)
    history = await _history(client, app, admin)

    assert listed == {"number": 1, "commitSha": C1, "since": _iso(_at(20, 10, 7))}
    assert history["live"] == listed


async def test_rejecting_records_the_note_history_shows(client, app, db_session) -> None:
    store = FakeStorage()
    app.dependency_overrides[storage_dependency] = lambda: store
    app.dependency_overrides[storage_or_none_dependency] = lambda: store
    owner = await UserFactory.create(db_session)
    admin = await _admin(db_session)
    row = await _app(db_session, owner)
    submission = await _routed(db_session, row, owner, _at(20, 9), C1)
    row.status = AppStatus.PENDING
    row.source_submission_id = submission
    row.source_commit_sha = C1
    await db_session.flush()
    note = "Please name a data owner before this goes live."

    resp = await client.post(
        f"/v1/admin/apps/{row.id}/reject", json={"note": note}, headers=_cookie(admin)
    )
    assert resp.status_code == 200, resp.text

    (version,) = _versions(await _history(client, row, admin))
    assert version["decision"]["kind"] == "rejected"
    assert version["decision"]["note"] == note


@pytest.mark.parametrize("decision", ["approve", "approve:self"])
async def test_an_approval_by_either_action_is_the_decision(
    client, db_session, decision: str
) -> None:
    owner = await UserFactory.create(db_session)
    admin = await _admin(db_session)
    app = await _app(db_session, owner, status=AppStatus.APPROVED)
    submission = await _routed(db_session, app, owner, _at(20, 9), C1)
    await _row(
        db_session,
        app,
        decision,
        _at(20, 10),
        admin,
        submissionId=str(submission),
        publishing="not_started",
        reason="publishing_unavailable",
    )

    (version,) = _versions(await _history(client, app, admin))

    assert (version["decision"]["kind"], version["state"]) == ("approved", "not_published")


async def test_a_record_naming_another_apps_attempt_never_borrows_it(client, db_session) -> None:
    owner = await UserFactory.create(db_session)
    admin = await _admin(db_session)
    mine = await _app(db_session, owner)
    theirs = await _app(db_session, owner)
    await _attempt(db_session, mine, _at(20, 8), _at(20, 8, 30), head_sha=C1)
    borrowed = await _published(db_session, theirs, owner, _at(20, 9), C2, _at(20, 9, 30))
    await _row(
        db_session,
        mine,
        "publish_gate",
        _at(20, 10),
        owner,
        appId=str(mine.id),
        decision="published",
        rule="all_clear",
        deploymentId=str(borrowed.id),
    )

    listed = await _listed(client, admin, mine)

    assert listed == {"number": None, "commitSha": C1, "since": _iso(_at(20, 8, 30))}
