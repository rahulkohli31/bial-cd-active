"""Health domain response schema."""

from __future__ import annotations

from typing import Literal

from src.schemas import CamelModel


class HealthStatus(CamelModel):
    """The `/v1/health` body. Every field is single-word, so the camel base is a
    no-op and the wire shape `{status, database, redis}` is byte-identical.

    ⚠️ `degraded` NO LONGER IMPLIES 503. Before the Redis field existed, `degraded`
    and HTTP 503 were the same event, so either could be read for the other. They
    have since come apart:

    * Postgres unreachable → `degraded` + **503**. Postgres is always-on; the API
      cannot do useful work without it, so the probe still fails CLOSED and a
      platform health gate correctly drains the instance.
    * Redis unreachable → `degraded` + **200**. Redis coordinates build sessions
      only; every other route still works. Restarting or draining the instance
      would fix nothing (the fault is in Redis, not here) and would take working
      functionality away from users, so this reports the truth at a healthy status
      code and leaves the call to an operator.

    The consequence, stated because it is easy to get wrong: **key automation on the
    HTTP status code, never on the word `degraded`.** A dashboard that pages on the
    status string alone will page for a Redis outage as if the API were down.

    `redis` carries a third state on purpose. Redis is genuinely optional outside
    production (`settings.redis: RedisConfig | None`), so `not_configured` is a
    CERTAIN answer — a defined, supported dev deployment — and is reported as `ok`
    overall. Folding it into `unreachable` would 503 every storage-off dev box, which
    is the exact collapse-two-tiers-into-one bug this codebase already shipped once
    (`docs/solutions/best-practices/green-tests-that-prove-nothing-fixture-and-passthrough-coverage-gaps-2026-07-17.md`).
    """

    status: Literal["ok", "degraded"]
    database: Literal["ok", "unreachable"]
    redis: Literal["ok", "unreachable", "not_configured"]
