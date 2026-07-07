"""Client-compiled artifact validation (U2, AE4): present/non-empty/size-bounded,
server never compiles."""

from __future__ import annotations

import pytest

from src.services.appserving.artifact import (
    MAX_ARTIFACT_BYTES,
    ArtifactError,
    validate_artifact,
)


def test_valid_artifact_returned_unchanged() -> None:
    compiled = "var App = function(){ return React.createElement('div', null, 'hi'); };"
    assert validate_artifact(compiled) == compiled


def test_missing_artifact_rejected() -> None:
    with pytest.raises(ArtifactError):
        validate_artifact(None)


def test_non_string_artifact_rejected() -> None:
    with pytest.raises(ArtifactError):
        validate_artifact({"compiled": "x"})


@pytest.mark.parametrize("value", ["", "   ", "\n\t "])
def test_empty_or_whitespace_artifact_rejected(value: str) -> None:
    with pytest.raises(ArtifactError):
        validate_artifact(value)


def test_oversized_artifact_rejected() -> None:
    # One byte over the ceiling → rejected (server never compiles, so size is the
    # only DoS bound a non-compiling validator can enforce).
    with pytest.raises(ArtifactError):
        validate_artifact("a" * (MAX_ARTIFACT_BYTES + 1))


def test_artifact_exactly_at_limit_accepted() -> None:
    at_limit = "a" * MAX_ARTIFACT_BYTES
    assert validate_artifact(at_limit) == at_limit
