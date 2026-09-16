"""Foundry model wiring for Pydantic AI.

The model is reachable ONLY through Azure AI Foundry — never the public Anthropic API. The
sanctioned chain is `AsyncAnthropicFoundry(resource, api_key | entra token provider)` →
`AnthropicProvider(anthropic_client=...)` → `AnthropicModel(deployment, provider=...)`. No
custom Model subclass. `_assert_foundry_only` is the fail-closed guard: the built client's
base URL must be an `*.services.ai.azure.com` Foundry endpoint and must NOT be the public
`api.anthropic.com`, so a wiring mistake can never silently reach the public API.

Auth: `api_key` mode is the tested default. `entra` mode (managed identity) builds a
bearer-token provider; the token SCOPE is an open question (the official docs disagree), so
it is a documented constant to confirm against the resource's RBAC before go-live."""

from __future__ import annotations

from urllib.parse import urlsplit

from anthropic import AsyncAnthropicFoundry, Timeout
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.providers.anthropic import AnthropicProvider

from src.config import FoundryConfig

# The Foundry endpoint host suffix (AsyncAnthropicFoundry interpolates the resource into
# `https://<resource>.services.ai.azure.com/anthropic/`) and the public host we must never hit.
_FOUNDRY_HOST_SUFFIX = ".services.ai.azure.com"
_PUBLIC_ANTHROPIC_HOST = "api.anthropic.com"

# Entra bearer-token scope for Foundry (managed identity). UNVERIFIED — the official docs
# disagree between `ai.azure.com/.default` and `ai.cognitiveservices.com/.default`; confirm
# against the resource's RBAC before go-live. API-key auth sidesteps this.
FOUNDRY_ENTRA_SCOPE = "https://ai.azure.com/.default"


class FoundryOnlyError(RuntimeError):
    """Raised when the model wiring would reach anything other than Azure Foundry,
    or when the Foundry config is internally inconsistent."""


def _assert_foundry_only(base_url: str) -> None:
    """Fail closed unless `base_url` is an Azure Foundry endpoint.

    Checks the PARSED HOST, not a substring of the whole URL — a substring test is satisfiable
    by a path segment or query string that happens to contain the suffix, without the request
    actually going to that host. The only input here is `FOUNDRY__RESOURCE`, written by
    whoever already holds `FOUNDRY__API_KEY`, so this is defence in depth rather than a hole
    reachable by anyone else."""
    host = urlsplit(base_url).hostname or ""
    if host == _PUBLIC_ANTHROPIC_HOST or not host.endswith(_FOUNDRY_HOST_SUFFIX):
        raise FoundryOnlyError(
            "model access must go through Azure AI Foundry "
            f"(*{_FOUNDRY_HOST_SUFFIX}), never the public Anthropic API."
        )


def build_foundry_client(config: FoundryConfig) -> AsyncAnthropicFoundry:
    """Build the Foundry-backed Anthropic client from typed config, then assert it targets
    Foundry (never the public API). The SDK client's socket is made FINITE and RETRIED here
    (both auth branches), so a dropped/wedged connection becomes a catchable
    `APITimeoutError`/`ModelHTTPError` both consumers funnel to a clean error, instead of a
    hang — tuning the SDK's already-sane defaults to the build harness's streaming turns
    (`FoundryConfig` has the read-vs-connect rationale). `read` is httpx's per-CHUNK idle
    timeout, bounding the gap between chunks, not the whole turn; `write`/`pool` inherit it."""
    # THE SDK'S OWN `Timeout`, NOT `httpx.Timeout`. The Anthropic client moved onto a vendored
    # httpx fork (`httpx2`) in 1.x, so the two `Timeout` classes are no longer the same type and
    # the plain httpx one stopped being accepted. Taking it from the SDK's public re-export
    # means we follow whichever httpx it vendors next, instead of pinning ourselves to a private
    # module path that is free to move again.
    timeout = Timeout(config.read_timeout_s, connect=config.connect_timeout_s)
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
