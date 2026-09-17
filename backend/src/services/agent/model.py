"""Foundry model wiring for Pydantic AI.

The model is reachable ONLY through Azure AI Foundry — never the public Anthropic API. The
sanctioned chain is `AsyncAnthropicFoundry(resource, api_key | entra token provider)` →
`AnthropicProvider(anthropic_client=...)` → `AnthropicModel(deployment, provider=...)`. No
custom Model subclass. `_assert_foundry_only` is the fail-closed guard: the built client's
base URL must be an `*.services.ai.azure.com` Foundry endpoint and must NOT be the public
`api.anthropic.com`, so a wiring mistake can never silently reach the public API.

Auth: `api_key` mode is the tested default. `entra` mode (managed identity) builds a
bearer-token provider; the token SCOPE is an open question (the official docs disagree), so
it is a documented constant to confirm against the resource's RBAC before go-live.

This module also widens one library exclusion at import, so that a turn-scoped system message
reaches Foundry as a `{'role': 'system'}` entry instead of being rewritten as `<system>`-tagged
text inside the citizen's own message — see `_widen_foundry_inline_system_prompts`."""

from __future__ import annotations

from anthropic import AsyncAnthropicFoundry, Timeout
from pydantic_ai.models import anthropic as anthropic_models
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.providers.anthropic import AnthropicProvider

from src.config import FoundryConfig

# The Foundry endpoint host suffix (AsyncAnthropicFoundry interpolates the resource into
# `https://<resource>.services.ai.azure.com/anthropic/`) and the public host we must never hit.
_FOUNDRY_HOST_SUFFIX = ".services.ai.azure.com"
_PUBLIC_ANTHROPIC_HOST = "api.anthropic.com"

# The private exclusion this module widens, the profile flag derived from it, and a deployment
# name Anthropic publishes as honouring a mid-conversation `system` entry. The probe name is
# fixed rather than read from config: the check is about the LIBRARY's wiring, and a resource
# whose deployment is named something the profile does not recognise would make it unanswerable.
_INLINE_EXCLUSION_ATTR = "_INLINE_SYSTEM_PROMPT_UNSUPPORTED_CLIENTS"
_INLINE_PROFILE_FLAG = "supports_inline_system_prompts"
_INLINE_PROBE_DEPLOYMENT = "claude-opus-5"

# Entra bearer-token scope for Foundry (managed identity). UNVERIFIED — the official docs
# disagree between `ai.azure.com/.default` and `ai.cognitiveservices.com/.default`; confirm
# against the resource's RBAC before go-live. API-key auth sidesteps this.
FOUNDRY_ENTRA_SCOPE = "https://ai.azure.com/.default"


class FoundryOnlyError(RuntimeError):
    """Raised when the model wiring would reach anything other than Azure Foundry,
    or when the Foundry config is internally inconsistent."""


class InlineSystemPromptShapeError(RuntimeError):
    """Raised at import when the library's inline-system-prompt exclusion no longer has the
    shape this module patches, so the patch would be inert rather than wrong."""


def _foundry_probe_model() -> AnthropicModel:
    """A throwaway Foundry-client model, built only to read a profile flag. No socket is opened
    by construction, and the client is never used to make a request."""
    probe_client = AsyncAnthropicFoundry(resource="inline-system-probe", api_key="probe")
    return AnthropicModel(
        _INLINE_PROBE_DEPLOYMENT, provider=AnthropicProvider(anthropic_client=probe_client)
    )


def _widen_foundry_inline_system_prompts() -> None:
    """Let a turn-scoped system message reach Foundry as a `{'role': 'system'}` entry.

    pydantic-ai excludes the Foundry client from mid-conversation system entries through a
    private tuple, and `Model.prepare_messages` rewrites those parts as `<system>`-tagged text
    inside the preceding USER message when the exclusion bites. That rewrite lands ephemeral
    content inside a message the store persists — the one position this platform's cached prefix
    cannot survive — so the exclusion is emptied here, at import, before any model is built.

    THE ASSERTION IS IN TWO HALVES BECAUSE AN INERT PATCH IS SILENT. The first half is the
    symbol: present, a tuple, and naming the Foundry client. The second half is the
    consequence — a Foundry-client model reports the flag as `False` before the swap and `True`
    after it — because the tuple could survive under its own name while the decision moved
    somewhere else entirely, and a patch that changes nothing would leave the sentence riding
    the channel it was written to avoid. The flag is read off a FRESH model each time: it is
    memoized per instance, so an instance built before the swap keeps answering `False`.
    """
    excluded = getattr(anthropic_models, _INLINE_EXCLUSION_ATTR, None)
    if not isinstance(excluded, tuple) or AsyncAnthropicFoundry not in excluded:
        raise InlineSystemPromptShapeError(
            f"pydantic_ai.models.anthropic.{_INLINE_EXCLUSION_ATTR} is no longer a tuple "
            f"containing AsyncAnthropicFoundry (got {excluded!r}); the mid-conversation system "
            "entry is not reachable the way this module reaches it."
        )
    if _foundry_probe_model().profile.get(_INLINE_PROFILE_FLAG) is not False:
        raise InlineSystemPromptShapeError(
            f"{_INLINE_PROFILE_FLAG} was already true for a Foundry client before the exclusion "
            "was widened, so this patch no longer decides anything."
        )
    setattr(anthropic_models, _INLINE_EXCLUSION_ATTR, ())
    if _foundry_probe_model().profile.get(_INLINE_PROFILE_FLAG) is not True:
        setattr(anthropic_models, _INLINE_EXCLUSION_ATTR, excluded)
        raise InlineSystemPromptShapeError(
            f"widening {_INLINE_EXCLUSION_ATTR} did not turn {_INLINE_PROFILE_FLAG} on, so the "
            "flag is derived somewhere else now and the patch is inert."
        )


_widen_foundry_inline_system_prompts()


def _assert_foundry_only(base_url: str) -> None:
    """Fail closed unless `base_url` is an Azure Foundry endpoint."""
    if _PUBLIC_ANTHROPIC_HOST in base_url or _FOUNDRY_HOST_SUFFIX not in base_url:
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
