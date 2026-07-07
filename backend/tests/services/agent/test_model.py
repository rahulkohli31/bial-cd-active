"""Foundry model wiring — the Foundry-only (AE5) guard and the api_key build path (U12)."""

from __future__ import annotations

import pytest
from pydantic_ai.models.anthropic import AnthropicModel

from src.config import FoundryConfig
from src.services.agent.model import (
    FoundryOnlyError,
    _assert_foundry_only,
    build_foundry_client,
    build_foundry_model,
)


def _config(**overrides) -> FoundryConfig:
    data = {
        "resource": "myfoundry",
        "deployment": "claude-opus",
        "auth_mode": "api_key",
        "api_key": "k",
    }
    data.update(overrides)
    return FoundryConfig.model_validate(data)


def test_guard_rejects_public_anthropic_api() -> None:
    # AE5: the public API endpoint is refused, fail-closed.
    with pytest.raises(FoundryOnlyError):
        _assert_foundry_only("https://api.anthropic.com/v1")


def test_guard_rejects_non_foundry_host() -> None:
    with pytest.raises(FoundryOnlyError):
        _assert_foundry_only("https://example.com/anthropic")


def test_guard_accepts_foundry_host() -> None:
    _assert_foundry_only("https://myfoundry.services.ai.azure.com/anthropic/")  # no raise


def test_build_client_targets_foundry() -> None:
    client = build_foundry_client(_config())
    base_url = str(client.base_url)
    assert ".services.ai.azure.com" in base_url
    assert "api.anthropic.com" not in base_url


def test_build_model_from_api_key_config() -> None:
    # AE5: a valid Foundry config builds an AnthropicModel (Foundry-backed).
    model = build_foundry_model(_config())
    assert isinstance(model, AnthropicModel)
