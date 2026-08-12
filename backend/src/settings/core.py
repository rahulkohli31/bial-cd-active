"""What EVERY process needs, and the one env-source config they all share.

Split out of `src/config.py` by U23 (ADR-0029 §9). The old single `Settings` carried every field
every subsystem might need, so a worker importing it had to satisfy the union of everything —
and the natural operator response to that is to narrow `ENVIRONMENT=development` to dodge the
production gates. That is the most dangerous misconfiguration this platform has: with object
storage unconfigured, the durable-copy gate answers "confirmed absent" for every container, and
a destroy path reads that as "nothing to preserve".

`CoreSettings` therefore holds ONLY what is universally required. Anything a role might not use
belongs in that role's own manifest (`api.py`, `worker.py`) — because a field placed here is
required of every future process, permanently.

That permanence is a TYPE-GATE rule, not a pydantic one, and the distinction matters if you are
tempted to work around it. Pydantic will happily let a subclass re-declare an inherited field to
change its requiredness, in either direction. What refuses is the type checkers: overriding a
mutable attribute's type is unsound, so mypy reports `[assignment]` and pyright reports
`reportIncompatibleVariableOverride` whether you widen `X` to `X | None` or narrow it back. Both
directions are closed, and the only way through is a suppression — which is why each role declares
its own fields instead of overriding a shared base's.
"""

from __future__ import annotations

import os
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# The single env-source contract, shared verbatim by every profile.
#
# `SettingsConfigDict` rather than a bare dict literal ON PURPOSE: it is a TypedDict, so all four
# type gates (ADR-0003) catch a misspelled config key that a plain dict would silently swallow.
#
# Declared here and INHERITED by each role manifest, which is safe only because every manifest has
# exactly ONE base. pydantic merges `model_config` along the MRO with a plain left-to-right
# `dict.update`, and every `BaseSettings` subclass owns a complete config dict with explicit `None`
# defaults — so with multiple bases the wrong composition order silently resets `env_file` and
# `env_nested_delimiter` to `None`, and the profile boots, reads no env file, and ignores every
# nested `X__Y` variable. One base means there is no merge to clobber it. If a manifest ever gains
# a second base, it must redeclare `model_config = SETTINGS_CONFIG`;
# `tests/test_settings_profiles.py` pins the outcome per role either way.
SETTINGS_CONFIG = SettingsConfigDict(
    # ENV_FILE selects the source file so tests can load .env.test (a separate database) without
    # a second config variable.
    env_file=os.getenv("ENV_FILE", ".env"),
    env_file_encoding="utf-8",
    # `__` nests sub-models (OBJECT_STORE__CONTAINER -> object_store.container).
    env_nested_delimiter="__",
    # Catches a mistyped key in the ENV FILE. It does NOT catch a mistyped environment variable:
    # `EnvSettingsSource` only ever emits declared fields, so an unknown `os.environ` key never
    # reaches this check. The real teeth against a typo are the nested `ConfigDict(extra="forbid")`
    # on each capability model plus no-default required fields.
    extra="forbid",
)

# Which role this process plays. Read from the environment by `src/config.py` to pick a profile;
# a deployment sets it on the worker's container and nowhere else, so the default is the API.
Role = Literal["api", "worker"]
ROLE_ENV_VAR = "BIAL_ROLE"


class CoreSettings(BaseSettings):
    """Fields with no role for which they are optional.

    Required settings carry NO default, so pydantic-settings raises at construction when they are
    missing — the process fails at startup in every environment rather than booting in dev and
    exploding in prod (`.claude/rules/fail-first-python.md`). `ENVIRONMENT` is a closed `Literal`
    for the same reason: a default would silently disable every `is_production` gate.
    """

    model_config = SETTINGS_CONFIG

    ENVIRONMENT: Literal["development", "staging", "production"]

    # `SecretStr` masks the embedded password in repr/str/validation errors; reads unwrap it via
    # `.get_secret_value()`, which is a grep-able audit trail of where the plaintext is used. It
    # is masking, not encryption.
    DATABASE_URL: SecretStr

    # How DATABASE_URL authenticates. "password" (default) = the password embedded in the DSN
    # (local Docker Postgres, tests, and — per ADR-0027 as amended — the deployment too).
    # "entra" = Azure Flexible Server with Microsoft Entra: no static password, a short-lived
    # token fetched per new connection via managed identity (db/base.py::attach_entra_token).
    # The default is correct everywhere it is used, so it stays a plain knob with no prod gate.
    DB_AUTH_MODE: Literal["password", "entra"] = "password"

    # User-assigned managed-identity client id for DB_AUTH_MODE=entra. None -> the system-assigned
    # identity / DefaultAzureCredential chain. Ignored in password mode.
    DB_ENTRA_CLIENT_ID: str | None = None

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"
