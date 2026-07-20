"""Redis coordination configuration model.

`Settings.redis` is typed `RedisConfig | None`; pydantic-settings validates one
`REDIS__*` env block against it (the single config funnel — no hand-written
`TypeAdapter` on the env path). Redis is the genuinely-optional coordination
integration: `| None` keeps dev/test booting without it, and the single prod gate
in `src.config` requires it in production (fail-first-python.md).

Redis coordinates the one-sandbox-per-user lock, idle heartbeat, and sandbox
registry (contract C5); the single-replica POC uses in-process asyncio for
progress (C7), so there are NO pub/sub channels.

`url` is a `SecretStr` (a Redis DSN may embed a password); it is unwrapped only at
the pool boundary in `client.py` (per security.md).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, PositiveFloat, PositiveInt, SecretStr


class RedisConfig(BaseModel):
    """Redis connection + pool knobs. `url` is required (no default — fail-first);
    the pool knobs keep POC-sensible defaults."""

    # `extra="forbid"` makes a mistyped REDIS__* nested key fail at startup instead
    # of silently defaulting (fail-first).
    model_config = ConfigDict(extra="forbid")

    # Redis DSN, e.g. redis://:pass@host:6379/0 or rediss://… — may embed a
    # password, so it is masked; unwrapped only in the pool factory (U6).
    url: SecretStr
    # Connection-pool ceiling (the POC bounds concurrency at one sandbox per user,
    # so a small pool is enough). `PositiveInt` rejects a nonsensical zero/negative.
    max_connections: PositiveInt = 10
    # Per-operation socket timeout in seconds — a hung Redis must not wedge a
    # coordination call forever. `PositiveFloat` rejects zero/negative.
    socket_timeout_seconds: PositiveFloat = 5.0
