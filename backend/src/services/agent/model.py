"""Foundry model wiring for Pydantic AI (R11, AE5).

The model is reachable ONLY through Azure AI Foundry — never the public Anthropic API. The
sanctioned chain is `AsyncAnthropicFoundry(resource, api_key | entra token provider)` →
`AnthropicProvider(anthropic_client=...)` → `AnthropicModel(deployment, provider=...)`. No
custom Model subclass. `_assert_foundry_only` is the fail-closed AE5 guard: the built client's
base URL must be an `*.services.ai.azure.com` Foundry endpoint and must NOT be the public
`api.anthropic.com` — so a mistake in wiring can never silently reach the public API.

Auth: `api_key` mode is the tested default. `entra` mode (managed identity) builds a bearer-token
provider; the exact token SCOPE is an open question (the official docs disagree), so it is a
documented constant to confirm against the resource's RBAC before go-live — API-key auth
sidesteps it initially.
"""

from __future__ import annotations

import httpx
from anthropic import AsyncAnthropicFoundry
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.providers.anthropic import AnthropicProvider

from src.config import FoundryConfig

# The Foundry endpoint host suffix (AsyncAnthropicFoundry interpolates the resource into
# `https://<resource>.services.ai.azure.com/anthropic/`) and the public host we must never hit.
_FOUNDRY_HOST_SUFFIX = ".services.ai.azure.com"
_PUBLIC_ANTHROPIC_HOST = "api.anthropic.com"

# Entra bearer-token scope for Foundry (managed identity). UNVERIFIED — the official docs
# disagree between `ai.azure.com/.default` and `ai.cognitiveservices.com/.default`; confirm
# against the resource's RBAC before go-live (candidate ADR-0026). API-key auth sidesteps this.
FOUNDRY_ENTRA_SCOPE = "https://ai.azure.com/.default"


class FoundryOnlyError(RuntimeError):
    """Raised when the model wiring would reach anything other than Azure Foundry (AE5),
    or when the Foundry config is internally inconsistent."""


def _assert_foundry_only(base_url: str) -> None:
    """Fail closed unless `base_url` is an Azure Foundry endpoint (AE5)."""
    if _PUBLIC_ANTHROPIC_HOST in base_url or _FOUNDRY_HOST_SUFFIX not in base_url:
        raise FoundryOnlyError(
            "model access must go through Azure AI Foundry "
            f"(*{_FOUNDRY_HOST_SUFFIX}), never the public Anthropic API."
        )


def build_foundry_client(config: FoundryConfig) -> AsyncAnthropicFoundry:
    """Build the Foundry-backed Anthropic client from typed config, then assert it targets
    Foundry (never the public API).

    The SDK client's socket is made FINITE and RETRIED here (both auth branches), so a dropped
    or wedged server→model connection becomes a catchable `APITimeoutError`/`ModelHTTPError`
    that both consumers already funnel to a clean error — instead of a hang. The SDK's own
    defaults (a finite timeout + 2 retries) are already sane; this just tunes the shape to the
    build harness's streaming turns (see `FoundryConfig` for the read-vs-connect rationale). A
    `read` timeout is httpx's per-CHUNK idle timeout on a streamed response, so it bounds the
    gap between model chunks, not the whole turn; `write`/`pool` inherit the `read` default."""
    timeout = httpx.Timeout(config.read_timeout_s, connect=config.connect_timeout_s)
    if config.auth_mode == "api_key":
        if config.api_key is None:
            # The config validator already enforces this pairing; narrow + fail closed.
            raise FoundryOnlyError("FOUNDRY__API_KEY is required in api_key auth mode.")
        client = AsyncAnthropicFoundry(
            resource=config.resource,
            api_key=config.api_key.get_secret_value(),
            timeout=timeout,
            max_retries=config.max_retries,
        )
    else:  # entra — managed identity, no static secret
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider

        token_provider = get_bearer_token_provider(DefaultAzureCredential(), FOUNDRY_ENTRA_SCOPE)
        client = AsyncAnthropicFoundry(
            resource=config.resource,
            azure_ad_token_provider=token_provider,
            timeout=timeout,
            max_retries=config.max_retries,
        )
    _assert_foundry_only(str(client.base_url))
    return client


def build_foundry_model(config: FoundryConfig) -> AnthropicModel:
    """Resolve typed Foundry config to a Pydantic AI `AnthropicModel` (Foundry-only)."""
    provider = AnthropicProvider(anthropic_client=build_foundry_client(config))
    return AnthropicModel(config.deployment, provider=provider)
