"""Foundry embedding-model wiring for Pydantic AI (#191 slice 3, R19-R23).

The embedding model is reachable ONLY through Azure AI Foundry — never the public OpenAI
API — the same AE5-shaped guarantee `services/agent/model.py` already gives the Claude path.
The sanctioned chain is `AzureProvider(azure_endpoint, api_key | entra token provider,
api_version)` → `OpenAIEmbeddingModel(deployment, provider=...)` → `Embedder(model)`.
`_assert_foundry_only` is the fail-closed guard: the built client's base URL must be an
`*.services.ai.azure.com` Foundry endpoint and must NOT be the public `api.openai.com` — so a
mistake in wiring can never silently reach the public API.

`FOUNDRY__RESOURCE` and `FOUNDRY__API_KEY` are REUSED AS-IS from the Claude config — no second
key, no second endpoint, no new secret to rotate (measured against the live resource: the
existing key reaches the embedding deployment with no extra grant). Only
`FOUNDRY__EMBEDDING_DEPLOYMENT` is new (`FoundryConfig.embedding_deployment`).

`api_version` IS PINNED AS A MODULE CONSTANT, deliberately NOT an environment variable:
`AzureProvider` refuses to build without one unless the endpoint targets the version-free
`/v1` shape, and a GA date does not vary between deployments — a constant is reviewable in a
diff in a way an env line is not.
"""

from __future__ import annotations

from typing import Annotated

import structlog
from fastapi import Depends
from openai import AsyncAzureOpenAI, Timeout
from pydantic_ai import Embedder
from pydantic_ai.embeddings.openai import OpenAIEmbeddingModel
from pydantic_ai.providers.azure import AzureProvider

from src.config import FoundryConfig, settings
from src.core.alarms import EMBEDDING_GUARD_VIOLATION_EVENT
from src.services.agent.model import FOUNDRY_ENTRA_SCOPE

logger = structlog.get_logger()

# The same suffix/host pair `services/agent/model.py` checks for the Claude path. The
# trailing `/openai/` `AsyncAzureOpenAI` appends to the resource endpoint does not change
# this: it is a SUBSTRING test, not an equality check against a bare resource endpoint, so it
# applies verbatim regardless of which API is being reached on the resource.
_FOUNDRY_HOST_SUFFIX = ".services.ai.azure.com"
_PUBLIC_OPENAI_HOST = "api.openai.com"

# The newest API version the live Foundry resource answers on; measured, not chosen.
_EMBEDDING_API_VERSION = "2024-10-21"


class EmbeddingFoundryOnlyError(RuntimeError):
    """Raised when the embedding client wiring would reach anything other than Azure
    Foundry, or when the Foundry config is internally inconsistent."""


def _assert_foundry_only(base_url: str) -> None:
    """Fail closed unless `base_url` is an Azure Foundry endpoint.

    Logs `EMBEDDING_GUARD_VIOLATION_EVENT` before raising — every caller of this guard (the
    startup check and the per-write build path alike) gets the alarm on the same violation,
    not just whichever one happens to run first.
    """
    if _PUBLIC_OPENAI_HOST in base_url or _FOUNDRY_HOST_SUFFIX not in base_url:
        logger.warning(EMBEDDING_GUARD_VIOLATION_EVENT, endpoint=base_url)
        raise EmbeddingFoundryOnlyError(
            "embedding access must go through Azure AI Foundry "
            f"(*{_FOUNDRY_HOST_SUFFIX}), never the public OpenAI API."
        )


def _build_provider(config: FoundryConfig) -> AzureProvider:
    """Build the Azure provider with the SAME anti-hang socket bounds the Claude path uses
    (`services/agent/model.py::build_foundry_client`) — `config.read_timeout_s` /
    `connect_timeout_s` / `max_retries` are ONE shared `FoundryConfig`, not a second knob set
    for this client to drift from. Without them, an unresponsive Foundry endpoint hangs on the
    OpenAI SDK's own default timeout (up to several minutes) with the caller's DB transaction
    still open the whole time — `write_description_embedding` runs before `db.commit()`, so a
    wedged embed call would hold that connection, not just the one request (review of #191,
    agc129).

    BUILDS AN EXPLICIT `AsyncAzureOpenAI` IN BOTH AUTH MODES, unlike before: `AzureProvider`'s
    own `azure_endpoint`/`api_key`/`api_version` constructor path has no `timeout=`/
    `max_retries=` parameters at all, so passing those meant handing it a client it built
    internally with the SDK's bare defaults. Building the client here and handing it in via
    `openai_client=` — already the entra branch's own shape — is the only way to reach them.
    """
    azure_endpoint = f"https://{config.resource}.services.ai.azure.com"
    # The SDK's OWN `Timeout`, not `httpx.Timeout` — see `build_foundry_client`'s identical
    # note: `openai.Timeout` is a distinct type from `httpx.Timeout`, so taking it from the
    # SDK's public re-export tracks whichever httpx it vendors next.
    timeout = Timeout(config.read_timeout_s, connect=config.connect_timeout_s)
    if config.auth_mode == "api_key":
        if config.api_key is None:
            # The config validator already enforces this pairing; narrow + fail closed.
            raise EmbeddingFoundryOnlyError("FOUNDRY__API_KEY is required in api_key auth mode.")
        client = AsyncAzureOpenAI(
            azure_endpoint=azure_endpoint,
            api_key=config.api_key.get_secret_value(),
            api_version=_EMBEDDING_API_VERSION,
            timeout=timeout,
            max_retries=config.max_retries,
        )
    else:  # entra — managed identity, no static secret; same scope as the Claude path
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider

        token_provider = get_bearer_token_provider(DefaultAzureCredential(), FOUNDRY_ENTRA_SCOPE)
        client = AsyncAzureOpenAI(
            azure_endpoint=azure_endpoint,
            azure_ad_token_provider=token_provider,
            api_version=_EMBEDDING_API_VERSION,
            timeout=timeout,
            max_retries=config.max_retries,
        )
    provider = AzureProvider(openai_client=client)
    _assert_foundry_only(str(provider.client.base_url))
    return provider


def build_embedder(config: FoundryConfig) -> Embedder | None:
    """Resolve typed Foundry config to an `Embedder`, or `None` if embeddings aren't
    configured at all (`embedding_deployment` unset — R20's documented optional-knob
    exception: semantic search is off, callers fall back to keyword-only)."""
    if config.embedding_deployment is None:
        return None
    provider = _build_provider(config)
    model = OpenAIEmbeddingModel(config.embedding_deployment, provider=provider)
    return Embedder(model)


def embedder_dependency() -> Embedder | None:
    """The shared embedder, or `None` when Foundry / the embedding deployment isn't
    configured — a dependency (mirroring `conversations/_shared.py::chat_model`) so tests
    inject a fake `Embedder` via `dependency_overrides` instead of hitting the real Foundry
    resource, and so every consumer (the description write path, the marketplace's search
    query, the duplicate check) resolves the SAME wiring rather than each building its own."""
    if settings.foundry is None:
        return None
    return build_embedder(settings.foundry)


EmbedderDep = Annotated[Embedder | None, Depends(embedder_dependency)]


def assert_embedding_guard_at_startup(config: FoundryConfig | None) -> None:
    """Run the Foundry-only guard once at application boot (R23), so a mis-wired
    `FOUNDRY__RESOURCE`/`FOUNDRY__EMBEDDING_DEPLOYMENT` fails the deploy rather than
    degrading silently into `EMBEDDING_WRITE_FAILED_EVENT` on the first real write.

    A no-op — not an error — when Foundry or the embedding deployment isn't configured at
    all: dev/test boot with neither, and that is a supported, deliberate configuration
    (R20), not a wiring mistake to fail loudly over.
    """
    if config is None or config.embedding_deployment is None:
        return
    _build_provider(config)  # raises EmbeddingFoundryOnlyError on a bad wire, else discarded
