"""An app's publishing history, derived from its audit trail and its deploy attempts.

A version is one send for publishing, numbered in the order sent by `_records`, the one query both
the History panel and the App Registry's live version read. Everything a version shows (its
decision, its publish attempts, whether it is live) comes only from records that name that send:
its submission id, or the deployment id its decision recorded. Never from a matching commit.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from src.api.v1.admin.schemas import (
    AppHistoryResponse,
    DecisionKind,
    HistoryAttempt,
    HistoryDecision,
    HistoryEntry,
    HistoryEvent,
    HistoryVersion,
    LiveVersion,
    VersionState,
)
from src.db.models.app_registry import AppRegistry, AppStatus
from src.db.models.audit import AuditLog
from src.db.models.deployment import Deployment, DeploymentStatus
from src.db.models.user import User
from src.services.deploy.gate import GATE_AUDIT_ACTION
from src.services.deploy.service import FAIL_ROUTED_FOR_REVIEW

HISTORY_CAP: Final = 200
"""How many audit records one History read covers, newest first."""

_SUBMIT: Final = "submit"
_APPROVALS: Final = ("approve", "approve:self")
_REJECT: Final = "reject"
_WITHDRAW: Final = "withdraw"
_DISABLE: Final = "disable"
_ENABLE: Final = "enable"
_EVENTS: Final = (_DISABLE, _ENABLE, "takedown", "unpublish", "restart")
_ATTEMPT_RECORDS: Final = (GATE_AUDIT_ACTION, _SUBMIT, *_APPROVALS)
_HISTORY_RECORDS: Final = (*_ATTEMPT_RECORDS, _REJECT, _WITHDRAW, *_EVENTS)

# The owner republishing the approved copy: an attempt on the approved send, never a send.
_APPROVED_COPY: Final = "approved_override"
# An older gate left some sends to the pipeline, whose own routed row (carrying the deployment
# id) is that send's outcome rather than a second send.
_DEFERRED: Final = "deferred_to_pipeline"


@dataclass(frozen=True)
class _Record:
    id: uuid.UUID
    at: datetime
    app_ref: str
    action: str
    detail: Mapping[str, Any]
    actor: str | None
    number: int | None


@dataclass(frozen=True)
class _Attempt:
    id: uuid.UUID
    app_id: uuid.UUID
    status: DeploymentStatus
    started_at: datetime
    finished_at: datetime | None
    failure_code: str | None
    head_sha: str | None


@dataclass
class _Send:
    record: _Record
    number: int
    submission: uuid.UUID | None
    routed: bool
    attempts: list[_Attempt] = field(default_factory=list)
    decision: HistoryDecision | None = None

    @property
    def successes(self) -> list[_Attempt]:
        return [a for a in self.attempts if a.status is DeploymentStatus.SUCCEEDED]

    @property
    def published_at(self) -> datetime | None:
        finished = [a.finished_at for a in self.successes if a.finished_at is not None]
        return min(finished, default=None)

    @property
    def last_success_at(self) -> datetime | None:
        finished = [a.finished_at for a in self.successes if a.finished_at is not None]
        return max(finished, default=None)


@dataclass
class _Event:
    record: _Record
    reenabled_at: datetime | None = None


@dataclass(frozen=True)
class _Derived:
    sends: list[_Send]
    events: list[_Event]
    live: _Send | None


def _uuid(value: object) -> uuid.UUID | None:
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, dict) else {}


# --- the one numbering ----------------------------------------------------------


def _records(app_ids: Sequence[uuid.UUID], actions: Sequence[str]) -> sa.Subquery:
    """The apps' audit records of `actions`, each send numbered 1, 2, … per app in the order sent.
    Every one of these actions is written app-scoped, so the resource index finds them without
    reading the rest of the trail.

    A send is a `publish_gate` row that published or routed, or a `submit` row no gate row shares
    a submission id with. Refusals, the approved copy's republish and a deferred send's own
    routed row are not sends."""
    ids = [str(app_id) for app_id in app_ids]
    app_ref = AuditLog.resource_id
    decision = AuditLog.detail["decision"].astext
    gate = aliased(AuditLog)
    is_send = sa.or_(
        sa.and_(
            AuditLog.action == GATE_AUDIT_ACTION,
            sa.or_(
                sa.and_(
                    decision.in_(("published", _DEFERRED)),
                    AuditLog.detail["rule"].astext.is_distinct_from(_APPROVED_COPY),
                ),
                sa.and_(decision == "routed", AuditLog.detail["deploymentId"].astext.is_(None)),
            ),
        ),
        sa.and_(
            AuditLog.action == _SUBMIT,
            ~sa.exists().where(
                gate.action == GATE_AUDIT_ACTION,
                gate.resource_type == "app",
                gate.resource_id == AuditLog.resource_id,
                gate.detail["submissionId"].astext == AuditLog.detail["submissionId"].astext,
            ),
        ),
    )
    number = sa.case(
        (
            is_send,
            sa.func.row_number().over(
                partition_by=(app_ref, is_send), order_by=(AuditLog.created_at, AuditLog.id)
            ),
        )
    )
    return (
        sa.select(
            AuditLog.id,
            AuditLog.created_at,
            AuditLog.action,
            AuditLog.detail,
            app_ref.label("app_ref"),
            number.label("number"),
            sa.func.coalesce(User.email, AuditLog.detail["email"].astext).label("actor"),
        )
        .outerjoin(User, AuditLog.actor_id == User.id)
        .where(
            AuditLog.resource_type == "app",
            AuditLog.resource_id.in_(ids),
            AuditLog.action.in_(actions),
        )
        .subquery()
    )


async def _load(
    db: AsyncSession, app_ids: Sequence[uuid.UUID], actions: Sequence[str], cap: int | None
) -> tuple[list[_Record], dict[uuid.UUID, _Attempt], bool]:
    """The records (the newest `cap` of them when capped) and the attempts they name."""
    records_of = _records(app_ids, actions)
    query = sa.select(records_of).order_by(records_of.c.created_at.desc(), records_of.c.id.desc())
    if cap is not None:
        query = query.limit(cap + 1)
    rows = (await db.execute(query)).all()
    truncated = cap is not None and len(rows) > cap
    records = [
        _Record(
            id=row.id,
            at=row.created_at,
            app_ref=row.app_ref,
            action=row.action,
            detail=_mapping(row.detail),
            actor=row.actor,
            number=row.number,
        )
        for row in rows[:cap]
    ]
    named = {
        deployment
        for record in records
        if record.action in (GATE_AUDIT_ACTION, *_APPROVALS)
        and (deployment := _uuid(record.detail.get("deploymentId"))) is not None
    }
    if not named:
        return records, {}, truncated
    attempts = await db.execute(
        sa.select(
            Deployment.id,
            Deployment.app_id,
            Deployment.status,
            Deployment.created_at,
            Deployment.finished_at,
            Deployment.failure_code,
            Deployment.head_sha,
        ).where(Deployment.id.in_(named), Deployment.app_id.in_(app_ids))
    )
    return (
        records,
        {row.id: _Attempt(*row) for row in attempts},
        truncated,
    )


# --- the derivation ---------------------------------------------------------------


def _derive(
    records: Sequence[_Record],
    attempts: Mapping[uuid.UUID, _Attempt],
    *,
    app_id: uuid.UUID,
    serving: bool,
) -> _Derived:
    """Attach each decision, attempt and event to the send its record names, then place the one
    live send when the app is `serving` anything now."""
    ordered = sorted(records, key=lambda r: (r.at, r.id))
    sends: list[_Send] = []
    events: list[_Event] = []
    by_submission: dict[uuid.UUID, _Send] = {}
    by_deployment: dict[uuid.UUID, _Send] = {}
    approved: _Send | None = None
    open_disable: _Event | None = None

    def attach(send: _Send, deployment_id: object) -> None:
        key = _uuid(deployment_id)
        attempt = attempts.get(key) if key is not None else None
        if (
            attempt is not None
            and attempt.app_id == app_id
            and attempt.failure_code != FAIL_ROUTED_FOR_REVIEW
        ):
            send.attempts.append(attempt)

    for record in ordered:
        detail = record.detail
        if record.number is not None:
            decision = detail.get("decision")
            send = _Send(
                record=record,
                number=record.number,
                submission=_uuid(detail.get("submissionId")),
                routed=record.action == _SUBMIT or decision == "routed",
            )
            sends.append(send)
            if send.submission is not None:
                by_submission[send.submission] = send
            if (deployment := _uuid(detail.get("deploymentId"))) is not None:
                by_deployment[deployment] = send
            attach(send, detail.get("deploymentId"))
        elif record.action == GATE_AUDIT_ACTION:
            if detail.get("rule") == _APPROVED_COPY and approved is not None:
                attach(approved, detail.get("deploymentId"))
            elif detail.get("decision") == "routed":
                deployment = _uuid(detail.get("deploymentId"))
                deferred = by_deployment.get(deployment) if deployment is not None else None
                submission = _uuid(detail.get("submissionId"))
                if deferred is not None and submission is not None:
                    deferred.routed = True
                    deferred.submission = submission
                    by_submission[submission] = deferred
        elif record.action in (*_APPROVALS, _REJECT, _WITHDRAW):
            submission = _uuid(detail.get("submissionId"))
            decided = by_submission.get(submission) if submission is not None else None
            if decided is None and submission is None:
                # A decision recorded without its submission id decides the one send waiting.
                decided = next(
                    (s for s in reversed(sends) if s.routed and s.decision is None), None
                )
            if decided is None or decided.decision is not None:
                continue
            if record.action in _APPROVALS:
                decided.decision = HistoryDecision(
                    kind=DecisionKind.APPROVED, by=record.actor, at=record.at, note=None
                )
                approved = decided
                attach(decided, detail.get("deploymentId"))
            elif record.action == _REJECT:
                decided.decision = HistoryDecision(
                    kind=DecisionKind.REJECTED,
                    by=record.actor,
                    at=record.at,
                    note=_text(detail.get("note")),
                )
            else:
                decided.decision = HistoryDecision(
                    kind=DecisionKind.WITHDRAWN, by=record.actor, at=record.at, note=None
                )
        elif record.action == _ENABLE and open_disable is not None:
            open_disable.reenabled_at = record.at
            open_disable = None
        elif record.action in _EVENTS:
            event = _Event(record=record)
            events.append(event)
            if record.action == _DISABLE:
                open_disable = event

    for send in sends:
        send.attempts.sort(key=lambda a: (a.started_at, a.id))
    # What serves is the newest success, or a restart of it, so while anything serves the live
    # send is the one owning the newest successful attempt.
    successes = [(attempt.id, send) for send in sends for attempt in send.successes]
    live = max(successes, key=lambda pair: pair[0])[1] if serving and successes else None
    return _Derived(sends=sends, events=events, live=live)


def _live_version(live: _Send | None, serving: Deployment) -> LiveVersion:
    if live is None:
        return LiveVersion(number=None, commit_sha=serving.head_sha, since=serving.finished_at)
    return LiveVersion(number=live.number, commit_sha=serving.head_sha, since=live.published_at)


def _commit(send: _Send, declaration: Mapping[str, Any]) -> str | None:
    detail = send.record.detail
    return (
        _text(detail.get("commitSha"))
        or _text(declaration.get("commit"))
        or _text(_mapping(declaration.get("commits")).get("shipping"))
        or next((a.head_sha for a in send.attempts if a.head_sha), None)
    )


def _decision(send: _Send, pending: uuid.UUID | None) -> HistoryDecision:
    if send.decision is not None:
        return send.decision
    if not send.routed:
        kind = DecisionKind.PUBLISHED
    elif send.submission is not None and send.submission == pending:
        kind = DecisionKind.WAITING
    else:
        kind = DecisionKind.NOT_RECORDED
    return HistoryDecision(kind=kind, by=None, at=None, note=None)


_STATE_OF_DECISION: Final = {
    DecisionKind.WAITING: VersionState.WAITING,
    DecisionKind.REJECTED: VersionState.REJECTED,
    DecisionKind.WITHDRAWN: VersionState.WITHDRAWN,
    DecisionKind.APPROVED: VersionState.NOT_PUBLISHED,
    DecisionKind.PUBLISHED: VersionState.NOT_RECORDED,
    DecisionKind.NOT_RECORDED: VersionState.NOT_RECORDED,
}


def _version(send: _Send, derived: _Derived, pending: uuid.UUID | None) -> HistoryVersion:
    decision = _decision(send, pending)
    replaced: _Send | None = None
    last_success = send.last_success_at
    if send is not derived.live and last_success is not None:
        later = [
            (published, other)
            for other in derived.sends
            if (published := other.published_at) is not None and published > last_success
        ]
        replaced = min(later, key=lambda pair: pair[0])[1] if later else None
    if send is derived.live:
        state = VersionState.LIVE
    elif send.successes:
        state = VersionState.REPLACED if replaced is not None else VersionState.TAKEN_OFFLINE
    elif send.attempts:
        latest = send.attempts[-1].status
        running = latest is DeploymentStatus.RUNNING
        state = VersionState.PUBLISHING if running else VersionState.PUBLISH_FAILED
    else:
        state = _STATE_OF_DECISION[decision.kind]
    declaration = send.record.detail.get("declaration")
    stored = declaration if isinstance(declaration, dict) else None
    return HistoryVersion(
        number=send.number,
        commit_sha=_commit(send, stored or {}),
        submission_id=send.submission,
        sent_at=send.record.at,
        sent_by=send.record.actor,
        declaration=stored,
        decision=decision,
        attempts=[
            HistoryAttempt(
                status=a.status,
                started_at=a.started_at,
                finished_at=a.finished_at,
                failure_code=a.failure_code,
            )
            for a in send.attempts
        ],
        state=state,
        published_at=send.published_at,
        replaced_by=replaced.number if replaced is not None else None,
        replaced_at=replaced.published_at if replaced is not None else None,
    )


# --- readers ----------------------------------------------------------------------


async def live_versions(
    db: AsyncSession, serving: Mapping[uuid.UUID, Deployment]
) -> dict[uuid.UUID, LiveVersion]:
    """Each serving app's live version, numbered as its History numbers it. Two queries however
    many apps."""
    if not serving:
        return {}
    records, attempts, _ = await _load(db, list(serving), _ATTEMPT_RECORDS, cap=None)
    by_app: dict[str, list[_Record]] = {}
    for record in records:
        by_app.setdefault(record.app_ref, []).append(record)
    return {
        app_id: _live_version(
            _derive(by_app.get(str(app_id), []), attempts, app_id=app_id, serving=True).live,
            row,
        )
        for app_id, row in serving.items()
    }


async def app_history(
    db: AsyncSession, app: AppRegistry, serving: Deployment | None
) -> AppHistoryResponse:
    """The app's versions and events, newest first, from its newest `HISTORY_CAP` records."""
    records, attempts, truncated = await _load(db, [app.id], _HISTORY_RECORDS, cap=HISTORY_CAP)
    derived = _derive(records, attempts, app_id=app.id, serving=serving is not None)
    pending = app.source_submission_id if app.status is AppStatus.PENDING else None
    dated: list[tuple[datetime, uuid.UUID, HistoryEntry]] = [
        (send.record.at, send.record.id, _version(send, derived, pending))
        for send in derived.sends
    ]
    dated.extend(
        (
            event.record.at,
            event.record.id,
            HistoryEvent(
                action=event.record.action,
                at=event.record.at,
                by=event.record.actor,
                reenabled_at=event.reenabled_at,
            ),
        )
        for event in derived.events
    )
    dated.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
    return AppHistoryResponse(
        entries=[entry for _, _, entry in dated],
        live=_live_version(derived.live, serving) if serving is not None else None,
        live_url=serving.url if serving is not None else None,
        truncated=truncated,
    )
