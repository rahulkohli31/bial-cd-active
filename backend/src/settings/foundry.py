"""The Azure AI Foundry nested env block.

A nested block normally lives beside the service that owns it (`src/services/<service>/config.py`)
— see `AuthConfig`, `RedisConfig`, `SandboxConfig`, `StorageConfig`, `DeployConfig`,
`AppDatabaseSettings`. `FoundryConfig` is the one exception, because `src/services/foundry/` does
not exist: the model client is built inline by the harness rather than by a service package. It
moves the day that package appears; nothing else about it is special.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    NonNegativeInt,
    PositiveFloat,
    SecretStr,
    model_validator,
)


class FoundryConfig(BaseModel):
    """Azure AI Foundry (Claude) model access — the ONLY sanctioned path to the model; the public
    Anthropic API is never used (R11 / AE5). Populated from one `FOUNDRY__*` env block, mirroring
    `services/auth/config.py`.

    `extra="forbid"` fails a mistyped nested key at startup; `SecretStr` masks the key; required
    inner fields carry NO default (fail-first). Exactly one auth mode: a static API key
    (`auth_mode="api_key"`, needs `api_key`) or an Entra token provider (`auth_mode="entra"`,
    managed identity — no static secret).

    Re-exported from `src/config.py`, which five call sites import it from.
    """

    # `extra="forbid"` makes a mistyped FOUNDRY__* nested key fail at startup instead of silently
    # defaulting (fail-first). This — not the profile's own extra="forbid" — is what actually
    # catches an env-var typo.
    model_config = ConfigDict(extra="forbid")

    # The Foundry resource (account) name — the `resource=` handed to AsyncAnthropicFoundry.
    resource: str
    # The model DEPLOYMENT name inside the resource — AnthropicModel(deployment).
    deployment: str
    # Auth-mode knob: a default is fine — it names a defined behaviour the wiring branches on.
    auth_mode: Literal["api_key", "entra"] = "api_key"
    # Present only in api_key mode; the validator enforces the pairing.
    api_key: SecretStr | None = None

    # Anti-hang socket timeouts for the SHARED model client (the planning-chat relay AND the build
    # harness both build it via `build_foundry_client`). These bound one SDK request, NOT the whole
    # build turn — the harness owns that budget (`RUN_WALL_CLOCK_DEADLINE_S`, checked BETWEEN
    # iterations, never mid-stream), which is exactly why a finite `read` is needed: without it a
    # dead socket hangs mid-stream forever. `read_timeout_s` is httpx's per-CHUNK idle timeout on a
    # streamed response (it resets on every received byte), so it must out-wait the longest
    # legitimate GAP between model chunks in a build turn — Anthropic keeps the stream alive with
    # periodic pings, so real gaps are small; 120s is generous enough that a bursty build turn
    # never false-fails, yet finite enough that a genuinely dead socket surfaces as a catchable
    # APITimeoutError instead of a hang. Sized to the harness, not the snappy relay, because ONE
    # client serves both.
    read_timeout_s: PositiveFloat = 120.0
    # `connect` stays tight — pure anti-hang, no legitimate reason to wait long to open a socket.
    connect_timeout_s: PositiveFloat = 10.0
    # Built-in SDK retries for connection errors + 408/409/429/5xx (honouring `Retry-After`). Kept
    # at the SDK's own default (2): the harness fires up to `MODEL_TURN_CEILING` requests per run
    # under `RUN_WALL_CLOCK_DEADLINE_S`, so a larger N would multiply worst-case wall-clock under
    # Foundry pressure and eat into the planning chat's stall-watchdog window. 0 disables retries
    # (a defined, valid deployment choice).
    max_retries: NonNegativeInt = 2

    @model_validator(mode="after")
    def _api_key_required_in_key_mode(self) -> Self:
        # STATIC message only — never echo the secret (pydantic reflects validator messages into
        # ValidationError, and thus into logs).
        if self.auth_mode == "api_key" and self.api_key is None:
            raise ValueError(
                "FOUNDRY__API_KEY is required when FOUNDRY__AUTH_MODE=api_key; "
                "set FOUNDRY__AUTH_MODE=entra for managed-identity auth instead."
            )
        return self
