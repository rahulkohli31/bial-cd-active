"""Foundry model wiring — the Foundry-only (AE5) guard and the api_key build path (U12)."""

from __future__ import annotations

import httpx
import pytest
from anthropic import AsyncAnthropicFoundry
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


def _timeout_of(client: AsyncAnthropicFoundry) -> httpx.Timeout:
    # `client.timeout` is typed `float | Timeout | None`; narrow to the httpx.Timeout we passed.
    assert isinstance(client.timeout, httpx.Timeout)
    return client.timeout


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


def test_api_key_client_applies_configured_timeout_and_retries() -> None:
    # U8: the shared model client gets a FINITE, retried socket sourced from FoundryConfig, so a
    # dead server→model connection surfaces as a catchable timeout instead of a hang. Custom
    # values prove the wiring (config → SDK client), not just that a default happened to match.
    client = build_foundry_client(
        _config(read_timeout_s=99.0, connect_timeout_s=7.0, max_retries=4)
    )
    assert client.max_retries == 4
    timeout = _timeout_of(client)
    assert timeout.read == 99.0  # per-chunk idle bound on the streamed response
    assert timeout.connect == 7.0


def test_client_defaults_are_finite_and_retry_modest() -> None:
    # Out of the box the socket is already finite (never an unbounded hang) and retry-modest.
    client = build_foundry_client(_config())
    assert client.max_retries == 2
    timeout = _timeout_of(client)
    assert timeout.read == 120.0
    assert timeout.connect == 10.0


def test_entra_client_also_applies_timeout_and_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    # The PRODUCTION path is managed-identity (entra), not api_key — patching only the api_key
    # branch would leave prod on an untuned socket. Stub the Azure credential + token provider
    # (never invoked at construction) so this exercises the real else-branch wiring.
    import azure.identity

    monkeypatch.setattr(azure.identity, "DefaultAzureCredential", lambda *a, **k: object())
    monkeypatch.setattr(azure.identity, "get_bearer_token_provider", lambda *a, **k: lambda: "t")
    client = build_foundry_client(
        _config(
            auth_mode="entra",
            api_key=None,
            read_timeout_s=99.0,
            connect_timeout_s=7.0,
            max_retries=4,
        )
    )
    assert client.max_retries == 4
    timeout = _timeout_of(client)
    assert timeout.read == 99.0
    assert timeout.connect == 7.0
