"""Frozen-surface tests for the build-session schemas.

These shapes are a cross-track contract rendered as executable code, so drift — a member
added or removed, a payload field renamed — has to be a test failure rather than a silent
divergence.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from src.api.v1.build_sessions.schemas import (
    HEARTBEAT_CADENCE_SECONDS,
    HEARTBEAT_TTL_SECONDS,
    LOCK_RENEW_CADENCE_SECONDS,
    LOCK_TTL_SECONDS,
    BuildError,
    BuildSessionStatus,
    ErrorSource,
)
from src.main import create_app

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


def test_error_source_has_exactly_the_four_members() -> None:
    assert {s.value for s in ErrorSource} == {"tsc", "next_build", "server", "client"}


# --- BuildError -----------------------------------------------------------


def test_build_error_shape() -> None:
    err = BuildError(source=ErrorSource.SERVER, title="boom", cleaned_stack="stack")
    assert err.source is ErrorSource.SERVER
    with pytest.raises(ValidationError):
        BuildError.model_validate({"source": "server", "title": "boom"})


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
