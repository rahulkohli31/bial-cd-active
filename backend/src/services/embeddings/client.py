"""Foundry embedding-model wiring for Pydantic AI.

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
from urllib.parse import urlsplit

import structlog
from fastapi import Depends
from openai import AsyncAzureOpenAI, Timeout
from pydantic_ai import Embedder
from pydantic_ai.embeddings.openai import OpenAIEmbeddingModel
from pydantic_ai.providers.azure import AzureProvider

from src.config import FoundryConfig, settings
from src.core.alarms import EMBEDDING_GUARD_VIOLATION_EVENT, SEMANTIC_SEARCH_STARTUP_STATE_EVENT
from src.services.agent.model import FOUNDRY_ENTRA_SCOPE

logger = structlog.get_logger()

# The same suffix/host pair `services/agent/model.py` checks for the Claude path.
_FOUNDRY_HOST_SUFFIX = ".services.ai.azure.com"
_PUBLIC_OPENAI_HOST = "api.openai.com"

# Measured against the live resource on 2026-09-08 (see the issue's Dependencies section).
_EMBEDDING_API_VERSION = "2024-10-21"


class EmbeddingFoundryOnlyError(RuntimeError):
    """Raised when the embedding client wiring would reach anything other than Azure
    Foundry, or when the Foundry config is internally inconsistent."""


def _assert_foundry_only(base_url: str) -> None:
    """Fail closed unless `base_url` is an Azure Foundry endpoint.

    Checks the PARSED HOST, not a substring of the whole URL — a substring test is satisfiable
    by a path segment or query string that happens to contain the suffix, without the request
    actually going to that host. The only input here is `FOUNDRY__RESOURCE`, written by
    whoever already holds `FOUNDRY__API_KEY`, so this is defence in depth rather than a hole
    reachable by anyone else.

    Logs `EMBEDDING_GUARD_VIOLATION_EVENT` before raising — every caller of this guard (the
    startup check and the per-write build path alike) gets the alarm on the same violation,
    not just whichever one happens to run first.
    """
    host = urlsplit(base_url).hostname or ""
    if host == _PUBLIC_OPENAI_HOST or not host.endswith(_FOUNDRY_HOST_SUFFIX):
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
    wedged embed call would hold that connection, not just the one request.

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
    configured at all (`embedding_deployment` unset — the documented optional-knob
    exception: semantic search is off, callers fall back to keyword-only).

    Caches the built `AzureProvider` at module scope as a side effect (`_cached_provider`),
    so `aclose_embedder` has the underlying HTTP client to close — see `embedder_dependency`
    for why a fresh client per call is wrong in the first place."""
    global _cached_provider
    if config.embedding_deployment is None:
        return None
    provider = _build_provider(config)
    _cached_provider = provider
    model = OpenAIEmbeddingModel(config.embedding_deployment, provider=provider)
    return Embedder(model)


_cached_embedder: Embedder | None = None
_cached_provider: AzureProvider | None = None
_embedder_resolved = False


def embedder_dependency() -> Embedder | None:
    """The shared embedder, built ONCE for the life of the process and reused — a dependency
    (mirroring `conversations/_shared.py::chat_model`) so tests inject a fake `Embedder` via
    `dependency_overrides` instead of hitting the real Foundry resource, and so every consumer
    (the description write path, the marketplace's search query, the duplicate check) resolves
    the SAME wiring AND THE SAME underlying HTTP client — rather than each request building
    its own fresh `AzureProvider` → `AsyncAzureOpenAI` → `httpx.AsyncClient` (its own TLS
    handshake and connection pool) that nothing ever closes. `_embedder_resolved` distinguishes
    "not built yet" from "built, and the answer was `None`" (embeddings unconfigured), since
    `Embedder | None` alone cannot tell those apart. `aclose_embedder` closes the cached client
    at shutdown."""
    global _cached_embedder, _embedder_resolved
    if not _embedder_resolved:
        _cached_embedder = None if settings.foundry is None else build_embedder(settings.foundry)
        _embedder_resolved = True
    return _cached_embedder


async def aclose_embedder() -> None:
    """Close the shared embedder's underlying HTTP client, if a real one was ever built — a
    no-op when embeddings aren't configured, or no request has resolved `EmbedderDep` yet."""
    global _cached_embedder, _cached_provider, _embedder_resolved
    if _cached_provider is not None:
        await _cached_provider.client.close()
    _cached_embedder = None
    _cached_provider = None
    _embedder_resolved = False


EmbedderDep = Annotated[Embedder | None, Depends(embedder_dependency)]


def assert_embedding_guard_at_startup(config: FoundryConfig | None) -> None:
    """Run the Foundry-only guard once at application boot, so a mis-wired
    `FOUNDRY__RESOURCE`/`FOUNDRY__EMBEDDING_DEPLOYMENT` fails the deploy rather than
    degrading silently into `EMBEDDING_WRITE_FAILED_EVENT` on the first real write.

    Logs `SEMANTIC_SEARCH_STARTUP_STATE_EVENT` in EITHER arm — on, or off — so the state is
    always visible on boot rather than only discoverable by noticing search behaving oddly.

    Off (`config is None or config.embedding_deployment is None`) is a no-op, not an error:
    dev/test boot with neither, and that is a supported, deliberate configuration, not a
    wiring mistake to fail loudly over.
    """
    if config is None or config.embedding_deployment is None:
        logger.info(SEMANTIC_SEARCH_STARTUP_STATE_EVENT, enabled=False)
        return
    _build_provider(config)  # raises EmbeddingFoundryOnlyError on a bad wire, else discarded
    logger.info(SEMANTIC_SEARCH_STARTUP_STATE_EVENT, enabled=True)
