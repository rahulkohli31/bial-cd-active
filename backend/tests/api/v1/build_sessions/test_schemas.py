"""Frozen-surface tests for the build-session schemas (C3 + C7, U8).

C3 and C7 are cross-track coordination contracts rendered as executable code, so their
shapes are PINNED here: drift (a member added/removed, a payload field renamed, a wrong
discriminator accepted) is a test failure, not a silent divergence. Also asserts the
Stage-0 stub router mounts cleanly so `create_app()` still boots.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import get_args

import pytest
from fastapi import FastAPI
from pydantic import TypeAdapter, ValidationError

from src.api.v1.build_sessions.schemas import (
    HEARTBEAT_CADENCE_SECONDS,
    HEARTBEAT_TTL_SECONDS,
    LOCK_RENEW_CADENCE_SECONDS,
    LOCK_TTL_SECONDS,
    BuildError,
    BuildResult,
    BuildSessionStatus,
    BuildSessionStatusResponse,
    EndedEvent,
    ErrorEvent,
    ErrorSource,
    EscalationEvent,
    ForceEndResponse,
    HeartbeatResponse,
    LockReleaseResponse,
    LockStateResponse,
    LogEvent,
    PreviewReadyEvent,
    PreviewReconnectingEvent,
    ProgressEnvelope,
    QuotaExceededEvent,
    StartBuildResponse,
    StepEvent,
    StopBuildResponse,
)
from src.main import create_app

_NOW = datetime(2026, 7, 13, 12, 0, 0, tzinfo=UTC)


# --- C3: BuildSessionStatus StrEnum -------------------------------------------


def test_status_enum_has_exactly_the_five_c3_members() -> None:
    assert {s.value for s in BuildSessionStatus} == {
        "provisioning",
        "building",
        "ready",
        "ended",
        "failed",
    }


def test_status_enum_round_trips_value_to_member() -> None:
    # StrEnum: value ↔ member both directions.
    assert BuildSessionStatus("ready") is BuildSessionStatus.READY
    assert BuildSessionStatus.READY.value == "ready"
    assert str(BuildSessionStatus.PROVISIONING) == "provisioning"


def test_status_enum_rejects_unknown_value() -> None:
    with pytest.raises(ValueError):
        BuildSessionStatus("deploying")


# --- C3: frozen lock TTL + cadence constants ----------------------------------


def test_cadence_constants_are_the_frozen_c3_values() -> None:
    # Byte-stable pins (like the C5 key constants in tests/services/redis/test_keys.py):
    # SESSION-API's Wave-1 lock ops + the portal keep-alive loop are coded to these exact
    # seconds, so silent drift must break a test, not slip through (C3 §3).
    assert LOCK_TTL_SECONDS == 900
    assert LOCK_RENEW_CADENCE_SECONDS == 300
    assert HEARTBEAT_CADENCE_SECONDS == 30
    assert HEARTBEAT_TTL_SECONDS == 90


# --- C3: control-op response models -------------------------------------------


def test_status_response_serializes_with_preview_url_camel() -> None:
    resp = BuildSessionStatusResponse(
        session_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        app_id=uuid.uuid4(),
        status=BuildSessionStatus.READY,
        preview_url="https://app-xyz.westeurope.azurecontainerapps.io/",
        last_seq=42,
        created_at=_NOW,
        updated_at=_NOW,
    )
    wire = resp.model_dump(by_alias=True)
    # CamelModel: snake_case Python ⇄ camelCase wire; preview_url MUST be present (C3 §2.3).
    assert wire["previewUrl"] == "https://app-xyz.westeurope.azurecontainerapps.io/"
    assert wire["lastSeq"] == 42
    assert "sessionId" in wire and "session_id" not in wire


def test_status_response_preview_url_nullable_until_ready() -> None:
    resp = BuildSessionStatusResponse(
        session_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        app_id=uuid.uuid4(),
        status=BuildSessionStatus.PROVISIONING,
        preview_url=None,
        last_seq=None,
        created_at=_NOW,
        updated_at=_NOW,
    )
    assert resp.preview_url is None
    assert resp.last_seq is None


def test_start_response_defaults_preview_url_null() -> None:
    resp = StartBuildResponse(
        session_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        app_id=uuid.uuid4(),
        status=BuildSessionStatus.PROVISIONING,
        created_at=_NOW,
    )
    assert resp.preview_url is None
    assert resp.status is BuildSessionStatus.PROVISIONING


def test_start_response_requires_its_fields() -> None:
    # A partial body (only session_id) must fail — project_id/app_id/status/created_at required.
    with pytest.raises(ValidationError):
        StartBuildResponse.model_validate({"session_id": str(uuid.uuid4())})


# --- C3: lock-op response models ----------------------------------------------


def test_lock_state_response_validates_required_fields() -> None:
    resp = LockStateResponse(
        session_id=uuid.uuid4(),
        held=True,
        owner_user_id=uuid.uuid4(),
        ttl_seconds=900,
        expires_at=_NOW,
    )
    assert resp.held is True
    assert resp.ttl_seconds == 900


def test_lock_state_response_rejects_missing_owner() -> None:
    with pytest.raises(ValidationError):
        LockStateResponse.model_validate(
            {"session_id": str(uuid.uuid4()), "held": True, "ttl_seconds": 900}
        )


def test_release_force_end_heartbeat_responses_validate() -> None:
    sid = uuid.uuid4()
    assert LockReleaseResponse(session_id=sid, released=True).released is True
    assert ForceEndResponse(session_id=sid, status=BuildSessionStatus.ENDED).status is (
        BuildSessionStatus.ENDED
    )
    hb = HeartbeatResponse(
        session_id=sid, alive=True, cadence_seconds=30, heartbeat_expires_at=_NOW
    )
    assert hb.alive is True and hb.cadence_seconds == 30


def test_stop_response_carries_terminal_status() -> None:
    resp = StopBuildResponse(session_id=uuid.uuid4(), status=BuildSessionStatus.ENDED)
    assert resp.status is BuildSessionStatus.ENDED


# --- C7: the tagged-union progress envelope -----------------------------------

_ENVELOPE: TypeAdapter[ProgressEnvelope] = TypeAdapter(ProgressEnvelope)

_C7_TYPES = {"step", "log", "error", "preview_ready", "escalation", "quota_exceeded", "ended"}


def test_envelope_union_is_the_seven_c7_members_plus_the_reconnecting_signal() -> None:
    # `ProgressEnvelope` is `Annotated[Union[...], Field(discriminator="type")]`: get_args()[0]
    # is the Union. Its args are the seven C7 variant classes PLUS the U5 `preview_reconnecting`
    # feed-only signal (F8) — a member that does NOT change the frozen 5-member BuildSessionStatus.
    # This is the exact-membership guard: an accidental addition/removal turns it red.
    union = get_args(ProgressEnvelope)[0]
    assert set(get_args(union)) == {
        StepEvent,
        LogEvent,
        ErrorEvent,
        PreviewReadyEvent,
        EscalationEvent,
        QuotaExceededEvent,
        EndedEvent,
        PreviewReconnectingEvent,
    }


def test_each_of_the_seven_types_validates_its_own_payload() -> None:
    payloads: dict[str, dict[str, object]] = {
        "step": {
            "type": "step",
            "seq": 1,
            "name": "scaffold",
            "label": "Scaffolding…",
            "state": "started",
        },
        "log": {"type": "log", "seq": 2, "source": "exec", "stream": "stdout", "text": "hi"},
        "error": {
            "type": "error",
            "seq": 3,
            "source": "tsc",
            "title": "Type error",
            "cleaned_stack": "app/page.tsx:1",
        },
        "preview_ready": {"type": "preview_ready", "seq": 4, "preview_url": "https://x/"},
        "escalation": {
            "type": "escalation",
            "seq": 5,
            "reason": "self_heal_budget_exhausted",
            "detail": "gave up",
            "last_error": None,
        },
        "quota_exceeded": {
            "type": "quota_exceeded",
            "seq": 6,
            "limit": 100,
            "used": 100,
            "resets_at": "2026-07-14T18:30:00Z",
        },
        "ended": {
            "type": "ended",
            "seq": 7,
            "status": "ended",
            "preview_url": None,
            "snapshot_committed": True,
            "reason": "completed",
        },
    }
    expected = {
        "step": StepEvent,
        "log": LogEvent,
        "error": ErrorEvent,
        "preview_ready": PreviewReadyEvent,
        "escalation": EscalationEvent,
        "quota_exceeded": QuotaExceededEvent,
        "ended": EndedEvent,
    }
    assert set(payloads) == _C7_TYPES  # the fixture itself covers all seven
    for type_name, payload in payloads.items():
        parsed = _ENVELOPE.validate_python(payload)
        assert isinstance(parsed, expected[type_name])
        assert parsed.seq == payload["seq"]  # every variant carries `seq` (§2)


def test_preview_ready_validates_and_carries_preview_url() -> None:
    ev = _ENVELOPE.validate_python(
        {"type": "preview_ready", "seq": 4, "preview_url": "https://p/"}
    )
    assert isinstance(ev, PreviewReadyEvent)
    assert ev.preview_url == "https://p/"


def test_error_event_carries_source_title_cleaned_stack() -> None:
    ev = _ENVELOPE.validate_python(
        {
            "type": "error",
            "seq": 3,
            "source": "next_build",
            "title": "Build failed",
            "cleaned_stack": "next build: exit 1",
        }
    )
    assert isinstance(ev, ErrorEvent)
    assert ev.source is ErrorSource.NEXT_BUILD
    assert ev.title == "Build failed"
    assert ev.cleaned_stack == "next build: exit 1"


def test_envelope_snake_case_wire_is_byte_stable() -> None:
    # C7 keeps snake_case keys + snake_case `type` literals (NO camelCase alias generator).
    ev = PreviewReadyEvent(seq=4, preview_url="https://p/")
    dumped = ev.model_dump()
    assert dumped == {"type": "preview_ready", "seq": 4, "preview_url": "https://p/"}
    assert "previewUrl" not in ev.model_dump_json()


def test_envelope_rejects_unknown_type() -> None:
    with pytest.raises(ValidationError):
        _ENVELOPE.validate_python({"type": "deploying", "seq": 1})


def test_envelope_rejects_missing_type() -> None:
    with pytest.raises(ValidationError):
        _ENVELOPE.validate_python({"seq": 1, "name": "scaffold"})


def test_envelope_rejects_mismatched_payload_for_type() -> None:
    # A `preview_ready` without its `preview_url`, or with a foreign field, must fail.
    with pytest.raises(ValidationError):
        _ENVELOPE.validate_python({"type": "preview_ready", "seq": 4})
    with pytest.raises(ValidationError):
        _ENVELOPE.validate_python(
            {
                "type": "step",
                "seq": 1,
                "name": "s",
                "label": "l",
                "state": "started",
                "extra": "nope",
            }
        )


def test_error_source_has_exactly_the_four_c7_members() -> None:
    assert {s.value for s in ErrorSource} == {"tsc", "next_build", "server", "client"}


# --- C7: BuildError + BuildResult ---------------------------------------------


def test_build_error_shape() -> None:
    err = BuildError(source=ErrorSource.SERVER, title="boom", cleaned_stack="stack")
    assert err.source is ErrorSource.SERVER
    with pytest.raises(ValidationError):  # cleaned_stack required
        BuildError.model_validate({"source": "server", "title": "boom"})


def test_build_result_validates_required_fields() -> None:
    result = BuildResult(
        status=BuildSessionStatus.ENDED,
        reason="completed",
        app_id=uuid.uuid4(),
        last_seq=7,
        snapshot_committed=True,
    )
    assert result.status is BuildSessionStatus.ENDED
    assert result.preview_url is None  # defaults null
    assert result.error is None


def test_build_result_carries_the_reason_the_terminal_frame_is_rendered_from() -> None:
    # `reason` travels on the verdict because BRAIN emits no terminal frame (R7): SESSION-API
    # renders the one `ended` from this after its C4 snapshot. Status alone cannot supply it —
    # `completed` and `quota_exceeded` are both ENDED, and both must reach the portal distinctly.
    completed = BuildResult(
        status=BuildSessionStatus.ENDED,
        reason="completed",
        app_id=uuid.uuid4(),
        last_seq=4,
        snapshot_committed=False,
    )
    quota = BuildResult(
        status=BuildSessionStatus.ENDED,
        reason="quota_exceeded",
        app_id=uuid.uuid4(),
        last_seq=4,
        snapshot_committed=False,
    )
    assert completed.status is quota.status
    assert completed.reason != quota.reason  # only `reason` tells them apart


def test_build_result_carries_error_on_failed() -> None:
    result = BuildResult(
        status=BuildSessionStatus.FAILED,
        reason="build_failed",
        app_id=uuid.uuid4(),
        preview_url=None,
        last_seq=9,
        snapshot_committed=False,
        error=BuildError(source=ErrorSource.TSC, title="t", cleaned_stack="s"),
    )
    assert result.error is not None
    assert result.error.source is ErrorSource.TSC


def test_build_result_rejects_missing_required() -> None:
    # app_id / last_seq / snapshot_committed / reason are required.
    with pytest.raises(ValidationError):
        BuildResult.model_validate({"status": "ended"})
    # `reason` specifically: without it SESSION-API cannot render an accurate terminal frame.
    with pytest.raises(ValidationError):
        BuildResult.model_validate(
            {
                "status": "ended",
                "app_id": str(uuid.uuid4()),
                "last_seq": 1,
                "snapshot_committed": False,
            }
        )


# --- C7: terminal-status narrowing (EndedEvent + BuildResult) ------------------


def test_ended_event_rejects_non_terminal_status() -> None:
    # EndedEvent.status is narrowed to the terminal members {ended, failed}: a terminal frame
    # carrying a non-terminal status (e.g. `building`) must FAIL validation, not slip through.
    with pytest.raises(ValidationError):
        EndedEvent.model_validate(
            {
                "type": "ended",
                "seq": 7,
                "status": "building",
                "preview_url": None,
                "snapshot_committed": True,
                "reason": "completed",
            }
        )
    # Both terminal members still validate.
    ended = EndedEvent(
        seq=7, status=BuildSessionStatus.ENDED, snapshot_committed=True, reason="completed"
    )
    failed = EndedEvent(
        seq=8, status=BuildSessionStatus.FAILED, snapshot_committed=False, reason="build_failed"
    )
    assert ended.status is BuildSessionStatus.ENDED
    assert failed.status is BuildSessionStatus.FAILED


def test_build_result_rejects_non_terminal_status() -> None:
    # BuildResult.status is narrowed the same way — a non-terminal verdict is invalid.
    with pytest.raises(ValidationError):
        BuildResult.model_validate(
            {
                "status": "provisioning",
                "reason": "completed",
                "app_id": str(uuid.uuid4()),
                "last_seq": 1,
                "snapshot_committed": False,
            }
        )


# --- C3 router mounts cleanly -------------------------------------------------


def test_app_boots_with_build_sessions_router_mounted() -> None:
    app = create_app()
    # Wave 1 fills the C3 control surface; the app + OpenAPI schema must build cleanly with
    # the routes mounted (Stage 0 asserted the inert stub; SESSION-API added the endpoints).
    assert isinstance(app, FastAPI)
    schema = app.openapi()
    assert schema["openapi"].startswith("3.")
    paths = schema.get("paths", {})
    assert "/v1/build-sessions" in paths  # start
    assert "/v1/build-sessions/{session_id}" in paths  # status
    assert "/v1/build-sessions/{session_id}/events" in paths  # SSE feed
