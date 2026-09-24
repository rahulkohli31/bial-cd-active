"""Frozen-surface tests for the build-session schemas.

These shapes are a cross-track contract rendered as executable code, so drift — a member
added or removed, a payload field renamed, a wrong discriminator accepted — has to be a
test failure rather than a silent divergence.
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
    EndedEvent,
    ErrorEvent,
    ErrorSource,
    EscalationEvent,
    PreviewReadyEvent,
    PreviewReconnectingEvent,
    ProgressEnvelope,
    QuotaExceededEvent,
    StartBuildResponse,
    StepEvent,
)
from src.main import create_app

_NOW = datetime(2026, 7, 13, 12, 0, 0, tzinfo=UTC)


# --- BuildSessionStatus StrEnum ------------------------------------------------


def test_status_enum_has_exactly_the_five_members() -> None:
    assert {s.value for s in BuildSessionStatus} == {
        "provisioning",
        "building",
        "ready",
        "ended",
        "failed",
    }


def test_status_enum_round_trips_value_to_member() -> None:
    assert BuildSessionStatus("ready") is BuildSessionStatus.READY
    assert BuildSessionStatus.READY.value == "ready"
    assert str(BuildSessionStatus.PROVISIONING) == "provisioning"


def test_status_enum_rejects_unknown_value() -> None:
    with pytest.raises(ValueError):
        BuildSessionStatus("deploying")


# --- the frozen lock TTL + cadence constants ----------------------------------


def test_cadence_constants_are_the_frozen_values() -> None:
    assert LOCK_TTL_SECONDS == 900
    assert LOCK_RENEW_CADENCE_SECONDS == 300
    assert HEARTBEAT_CADENCE_SECONDS == 30
    assert HEARTBEAT_TTL_SECONDS == 90


# --- control-op response models -----------------------------------------------


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
    with pytest.raises(ValidationError):
        StartBuildResponse.model_validate({"session_id": str(uuid.uuid4())})


# --- the tagged-union progress envelope ---------------------------------------

_ENVELOPE: TypeAdapter[ProgressEnvelope] = TypeAdapter(ProgressEnvelope)

_C7_TYPES = {"step", "error", "preview_ready", "escalation", "quota_exceeded", "ended"}


def test_envelope_union_is_the_six_members_plus_the_reconnecting_signal() -> None:
    # `ProgressEnvelope` is `Annotated[Union[...], Field(discriminator="type")]`, so the union
    # itself is `get_args(...)[0]` — walking the annotation directly finds only the Annotated.
    union = get_args(ProgressEnvelope)[0]
    assert set(get_args(union)) == {
        StepEvent,
        ErrorEvent,
        PreviewReadyEvent,
        EscalationEvent,
        QuotaExceededEvent,
        EndedEvent,
        PreviewReconnectingEvent,
    }


def test_each_of_the_six_types_validates_its_own_payload() -> None:
    payloads: dict[str, dict[str, object]] = {
        "step": {
            "type": "step",
            "seq": 1,
            "name": "scaffold",
            "label": "Scaffolding…",
            "state": "started",
        },
        "error": {
            "type": "error",
            "seq": 2,
            "source": "tsc",
            "title": "Type error",
            "cleaned_stack": "app/page.tsx:1",
        },
        "preview_ready": {"type": "preview_ready", "seq": 3, "preview_url": "https://x/"},
        "escalation": {
            "type": "escalation",
            "seq": 4,
            "reason": "self_heal_budget_exhausted",
            "detail": "gave up",
            "last_error": None,
        },
        "quota_exceeded": {
            "type": "quota_exceeded",
            "seq": 5,
            "limit": 100,
            "used": 100,
            "resets_at": "2026-07-14T18:30:00Z",
        },
        "ended": {
            "type": "ended",
            "seq": 6,
            "status": "ended",
            "preview_url": None,
            "snapshot_committed": True,
            "reason": "completed",
        },
    }
    expected = {
        "step": StepEvent,
        "error": ErrorEvent,
        "preview_ready": PreviewReadyEvent,
        "escalation": EscalationEvent,
        "quota_exceeded": QuotaExceededEvent,
        "ended": EndedEvent,
    }
    assert set(payloads) == _C7_TYPES
    for type_name, payload in payloads.items():
        parsed = _ENVELOPE.validate_python(payload)
        assert isinstance(parsed, expected[type_name])
        assert parsed.seq == payload["seq"]


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


def test_error_source_has_exactly_the_four_members() -> None:
    assert {s.value for s in ErrorSource} == {"tsc", "next_build", "server", "client"}


# --- BuildError + BuildResult -------------------------------------------------


def test_build_error_shape() -> None:
    err = BuildError(source=ErrorSource.SERVER, title="boom", cleaned_stack="stack")
    assert err.source is ErrorSource.SERVER
    with pytest.raises(ValidationError):
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
    assert result.preview_url is None
    assert result.error is None


def test_build_result_carries_the_reason_the_terminal_frame_is_rendered_from() -> None:
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
    assert completed.reason != quota.reason


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
    with pytest.raises(ValidationError):
        BuildResult.model_validate({"status": "ended"})
    with pytest.raises(ValidationError):
        BuildResult.model_validate(
            {
                "status": "ended",
                "app_id": str(uuid.uuid4()),
                "last_seq": 1,
                "snapshot_committed": False,
            }
        )


# --- terminal-status narrowing (EndedEvent + BuildResult) ---------------------


def test_ended_event_rejects_non_terminal_status() -> None:
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
    ended = EndedEvent(
        seq=7, status=BuildSessionStatus.ENDED, snapshot_committed=True, reason="completed"
    )
    failed = EndedEvent(
        seq=8, status=BuildSessionStatus.FAILED, snapshot_committed=False, reason="build_failed"
    )
    assert ended.status is BuildSessionStatus.ENDED
    assert failed.status is BuildSessionStatus.FAILED


def test_build_result_rejects_non_terminal_status() -> None:
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


# --- the router mounts cleanly ------------------------------------------------


def test_app_boots_with_build_sessions_router_mounted() -> None:
    app = create_app()
    assert isinstance(app, FastAPI)
    schema = app.openapi()
    assert schema["openapi"].startswith("3.")
    paths = schema.get("paths", {})
    assert "/v1/build-sessions/relaunch" in paths
    # Nothing can mint a session id for a client to name, so a route addressed by one would have
    # no caller that could reach anything but a 404.
    assert "/v1/build-sessions" not in paths
    assert not [p for p in paths if p.startswith("/v1/build-sessions/{session_id}")]
