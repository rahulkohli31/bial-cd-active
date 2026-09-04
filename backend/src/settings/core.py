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
from urllib.parse import urlsplit

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.services.sandbox.base import base_path_for

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

    # WHERE A GENERATED APP IS REACHED FROM A BROWSER, e.g. https://citizenapps.bialairport.com.
    #
    # Every generated app — preview and published alike — is served from this ONE hostname, with
    # the app's key in the path (`/a/sbx-<28 hex>/`, `/a/pub-<28 hex>/`). Per-app subdomains would
    # need a wildcard certificate, which BIAL refused; that refusal is why this is a single value
    # rather than a domain to compose names under.
    #
    # IT LIVES IN CORE, WHICH IS A DELIBERATE CALL AGAINST THIS FILE'S OWN BAR — "only what is
    # universally required", because a field here binds every future process permanently. It
    # clears that bar: both roles need it today, for different reasons, and any future role that
    # touches an app will need it too. The API hands the browser a preview address and injects
    # the hostname into every sandbox so Server Actions accept a form post that arrived through
    # the router; the worker's deploy reconciliation writes a published app's real, shared
    # address. A process that could not compose an app address would write an unreachable link
    # and only find out when somebody clicked it.
    #
    # The alternative — declaring it once per manifest — was rejected: it duplicates the field
    # AND its validator, and the type gates forbid a shared base being overridden per role, so
    # the duplication would be permanent rather than transitional.
    #
    # REQUIRED, NO DEFAULT. There is no deployment where a guess is correct, and every wrong value
    # here fails the same way — a link that resolves to nothing, discovered by the person it was
    # sent to rather than by the platform.
    APPS_BASE_URL: str

    @field_validator("APPS_BASE_URL")
    @classmethod
    def _apps_base_url_is_an_origin(cls, v: str) -> str:
        """Reject a shape that would compose a wrong address rather than fail.

        A trailing slash yields `…com//a/<key>/`, a path yields `…/x/a/<key>/`, and a bare
        hostname yields a relative link. None of the three raises anywhere downstream: they all
        produce a plausible-looking URL that goes nowhere, and the first report comes from a
        colleague who could not open a shared app.
        """
        v = v.strip()
        parsed = urlsplit(v)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(
                f"APPS_BASE_URL must be an absolute origin like "
                f"https://citizenapps.bialairport.com (got: {v!r})"
            )
        if parsed.path or parsed.query or parsed.fragment:
            raise ValueError(
                f"APPS_BASE_URL must carry no path, query or trailing slash — the app key is "
                f"appended to it as /a/<key>/ (got: {v!r})"
            )
        return v

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"

    @property
    def apps_hostname(self) -> str:
        """The bare hostname of `APPS_BASE_URL`, with no scheme and no port.

        Next's `serverActions.allowedOrigins` wants a host, not an origin, and injecting a value
        with a scheme there fails CLOSED and SILENTLY: the Server Action's origin comparison never
        matches, so every form post in every generated app is aborted as a CSRF attempt with no
        other symptom. Derived here so the one place that has to strip it is the one place that
        knows the shape.
        """
        return urlsplit(self.APPS_BASE_URL).hostname or ""

    def app_url(self, app_name: str) -> str:
        """The browser-facing address of the app whose container is called `app_name`.

        `app_name` is `sbx-`/`pub-` plus 28 hex — the container app's own name — which is what
        makes this a string composition rather than a lookup, and is why the router needs no
        registry.

        NO TRAILING SLASH, and this is measured rather than chosen. Against a real Next 16 dev
        server, `/<base>/` answers 308 and redirects to `/<base>`; only the unslashed form
        answers 200. A slash here would put a redirect in front of every framed preview and
        every published link somebody shares — the opposite of what an earlier draft of this
        docstring claimed it was avoiding.
        """
        return f"{self.APPS_BASE_URL}{base_path_for(app_name)}"
