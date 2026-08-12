"""Per-project database provisioning configuration (`APP_DB__*`).

`Settings.app_db` is typed `AppDatabaseSettings | None` — the genuinely-optional
integration shape (`.claude/rules/fail-first-python.md`): dev/test boot with no substrate
at all and every project simply works without a database, while the single prod gate in
`src.config` refuses to start production without one. Sibling of
`services/storage/config.py` and `services/sandbox/config.py`.

Required inner fields carry NO default; knobs keep defaults whose value has a defined
meaning the code branches on. Every credential is a `SecretStr` so it never renders into a
`ValidationError` (and thus a log line).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, PositiveInt, SecretStr


class AppDatabaseSettings(BaseModel):
    """The maintenance identity + policy knobs for per-project databases (ADR-0028)."""

    # `extra="forbid"` makes a mistyped APP_DB__* nested key fail at startup instead of
    # being silently ignored and falling back to a default (fail-first). Not inherited
    # from `Settings` — every sub-model in this repo sets it explicitly.
    model_config = ConfigDict(extra="forbid")

    # The DEDICATED maintenance role's DSN (CREATEDB + CREATEROLE, NOT the control-plane
    # `DATABASE_URL` identity), pointed at a NEUTRAL maintenance database — `postgres`,
    # never `citizen_one`. `CREATE DATABASE` / `DROP DATABASE` cannot run inside a
    # transaction block, so this DSN drives its own AUTOCOMMIT engine (`engine.py`) and
    # must never be handed to the ORM session factory. Required, no default: a control
    # plane that thinks it can provision but has no maintenance credential would fail at
    # the first project create instead of at startup.
    maintenance_dsn: SecretStr

    # urlsafe-base64 32-byte Fernet key (`Fernet.generate_key()`), used to encrypt each
    # app role's password at rest (`secrets.py`). Required, no default: a default would be
    # a fleet-wide shared key, and an empty one would mean storing the passwords in clear.
    encryption_key: SecretStr

    # The `host:port` the SANDBOX container can reach the database server on. None = reuse
    # the maintenance DSN's own host, which is correct whenever the control plane and the
    # sandbox see the same address (real Azure). It is NOT correct for local dev, where the
    # control plane's `localhost:5432` resolves to the sandbox's OWN localhost — the exact
    # class of bug `SandboxConfig.blob_base_url` (services/sandbox/config.py:69-76) exists
    # to solve, and this knob mirrors it deliberately. It lives here, not on
    # `SandboxConfig`, because the DSN is assembled in this package and splitting one
    # connection descriptor across two config blocks would be strictly worse.
    sandbox_dsn_host: str | None = None

    # Per-role, per-database `statement_timeout`. Applied with `ALTER ROLE ... IN DATABASE
    # ... SET`, so it binds every session the app opens and an agent-authored runaway query
    # cannot pin a connection forever. A defined default (30s) is the right POC policy.
    statement_timeout_ms: PositiveInt = 30_000
    # Per-role, per-database `idle_in_transaction_session_timeout`: an app that opens a
    # transaction and wanders off releases its locks instead of blocking the kill-switch.
    idle_in_transaction_timeout_ms: PositiveInt = 60_000
