"""Role-scoped settings profiles — the boot matrix (U23).

WHAT THIS PROTECTS. `src/config.py` used to be one `Settings` carrying every field every
subsystem might need, behind seven production gates. A worker importing it had to satisfy the
union of everything, so the natural operator response was to narrow `ENVIRONMENT=development` to
dodge the gates — and that is the single most dangerous misconfiguration available to this
platform. With object storage unconfigured, `manager.py`'s bundle check answers *"CONFIRMED
absent"*, which the reclamation destroy path (U14) reads as *"nothing to preserve, safe to
delete"*. A worker booted that way would delete the entire fleet while believing it had verified
each container.

THE GUARANTEE. `WorkerSettings` cannot construct without object storage, sandbox/ARM access, or
Redis — **in every environment**, not merely in production. That is deliberately stronger than a
prod gate: a prod gate can be dodged by lying about `ENVIRONMENT`, and this cannot.

WHY THE model_config GUARD TESTS ARE NOT PARANOIA. pydantic merges `model_config` along the MRO
with a plain left-to-right `dict.update` (`pydantic._internal._config.ConfigWrapper.for_model`),
and *every* `BaseSettings` subclass owns a complete 37-key config dict with explicit `None`
defaults. A profile composed from several `BaseSettings` bases can therefore have `env_file` and
`env_nested_delimiter` silently reset to `None` — it then boots, reads no env file, and ignores
every nested `X__Y` variable. U24 made each manifest single-inheritance, so there is now no merge
to clobber it and the redeclaration was dropped; these tests stay because they pin the OUTCOME
(the delimiter and env file survive) rather than the mechanism, and they are what would catch a
manifest that later gains a second base without reasserting the config.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.settings import ApiSettings, WorkerSettings

_BACKEND_ROOT = Path(__file__).resolve().parent.parent

# --- Minimal env blocks, one per capability. Nested keys use the `__` delimiter on purpose:
# they are what proves the delimiter survived the MRO merge.
_CORE: dict[str, str] = {
    "ENVIRONMENT": "development",
    "DATABASE_URL": "postgresql+asyncpg://u:p@localhost:5432/citizen_one_test",
}
_STORE: dict[str, str] = {
    "OBJECT_STORE__ACCOUNT_URL": "https://acct.blob.core.windows.net",
    "OBJECT_STORE__CONTAINER": "bial",
    # StorageConfig requires exactly one auth mode; without it the block is invalid and the
    # "refuses without storage" tests would pass for the wrong reason.
    "OBJECT_STORE__ACCOUNT_KEY": "dGVzdC1hY2NvdW50LWtleQ==",
}
_REDIS: dict[str, str] = {"REDIS__URL": "redis://localhost:6379/0"}
_SANDBOX: dict[str, str] = {
    "SANDBOX__SUBSCRIPTION_ID": "00000000-0000-0000-0000-000000000000",
    "SANDBOX__RESOURCE_GROUP": "bial-dev-rg",
    "SANDBOX__REGION": "centralindia",
    "SANDBOX__MANAGED_ENVIRONMENT_NAME": "bial-citizen-dev-aca-env",
    "SANDBOX__IMAGE_REF": "acr.azurecr.io/sandbox:latest",
    "SANDBOX__ACR_SERVER": "acr.azurecr.io",
    "SANDBOX__ACR_USERNAME": "acr-user",
    "SANDBOX__ACR_PASSWORD": "acr-password",
}
_AUTH: dict[str, str] = {
    "AUTH__TENANT_ID": "11111111-1111-1111-1111-111111111111",
    "AUTH__CLIENT_ID": "22222222-2222-2222-2222-222222222222",
    "AUTH__SESSION_SECRET": "unit-test-session-secret-0123456789abcdef",
    "AUTH__REDIRECT_URI": "http://localhost:8000/api/v1/auth/callback",
}
_ADMINS: dict[str, str] = {"SUPERADMIN_EMAILS": "admin@bial.com"}
_APP_DB: dict[str, str] = {
    "APP_DB__MAINTENANCE_DSN": "postgresql+asyncpg://maint:p@localhost:5432/postgres",
    # Fernet wants 32 url-safe-base64-encoded bytes; any well-formed key satisfies construction.
    "APP_DB__ENCRYPTION_KEY": "dGVzdC1lbmNyeXB0aW9uLWtleS0zMi1ieXRlcy1sb25nISE=",
}

_WORKER_ENV = {**_CORE, **_STORE, **_REDIS, **_SANDBOX}
_API_ENV = {**_CORE, **_AUTH, **_ADMINS}


@contextmanager
def _environment_of(env: dict[str, str]) -> Iterator[None]:
    """Replace `os.environ` with EXACTLY `env` (plus PATH) for the duration.

    Two reasons this is not overkill. First, `__` nesting is a property of the ENVIRONMENT
    source — passing `OBJECT_STORE__CONTAINER=...` as a constructor kwarg goes through the init
    source instead, where it is an unknown top-level key rather than a nested one, so a test
    written that way asserts nothing about the delimiter. Second, without scrubbing, the
    developer's own exported variables quietly supply whatever a block omits, and every "refuses
    without X" assertion below would pass for the wrong reason.
    """
    saved = dict(os.environ)
    os.environ.clear()
    os.environ["PATH"] = saved.get("PATH", "")
    os.environ.update(env)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def _boot[P: (ApiSettings, WorkerSettings)](profile: type[P], env: dict[str, str]) -> P:
    """Construct `profile` from EXACTLY `env`, through the real environment source, with the env
    file disabled so nothing on disk can supply a missing block."""
    # Env-sourced fields are invisible to ty/pyright (mypy's pydantic plugin does see them), and
    # `_env_file` is a pydantic-settings init kwarg rather than a model field. Same suppression
    # pair `src/config.py` has always carried on `Settings()`.
    with _environment_of(env):
        return profile(  # ty: ignore[missing-argument]
            _env_file=None  # ty: ignore[unknown-argument]  # pyright: ignore[reportCallIssue]
        )


# ---------------------------------------------------------------- model_config integrity


@pytest.mark.parametrize("profile", [ApiSettings, WorkerSettings])
def test_the_profile_kept_its_env_config_through_the_mro_merge(
    profile: type[ApiSettings] | type[WorkerSettings],
) -> None:
    """If a mixin clobbers these, the profile still boots — and silently ignores every nested
    `X__Y` env var and the whole env file. Failure would be invisible without this test."""
    config = profile.model_config
    assert config.get("env_nested_delimiter") == "__", (
        f"{profile.__name__} lost its nested delimiter: every X__Y variable would be ignored"
    )
    assert config.get("extra") == "forbid", f"{profile.__name__} lost extra=forbid"
    assert config.get("env_file"), f"{profile.__name__} lost its env_file"


# ---------------------------------------------------------------- the worker guarantee


def test_the_worker_refuses_to_boot_without_object_storage() -> None:
    """THE test of this unit. Mutation-check: make `object_store` optional on WorkerSettings and
    this must go red — because that reversion is what lets a misconfigured worker answer U14's
    durable-copy gate with "confirmed absent" for every container and delete the fleet."""
    env = {k: v for k, v in _WORKER_ENV.items() if not k.startswith("OBJECT_STORE__")}
    with pytest.raises(ValidationError) as excinfo:
        _boot(WorkerSettings, env)
    assert "object_store" in str(excinfo.value)


def test_the_worker_refuses_without_object_storage_even_in_development() -> None:
    """The whole point of requiring it rather than prod-gating it: `ENVIRONMENT=development` is
    exactly the lever an operator reaches for to make a worker boot, and it must not work."""
    env = {k: v for k, v in _WORKER_ENV.items() if not k.startswith("OBJECT_STORE__")}
    env["ENVIRONMENT"] = "development"
    with pytest.raises(ValidationError):
        _boot(WorkerSettings, env)


@pytest.mark.parametrize("missing", ["REDIS__", "SANDBOX__"])
def test_the_worker_refuses_without_the_capabilities_it_uses(missing: str) -> None:
    """Redis is both the broker and the spare-list; sandbox/ARM is how it deletes. A worker
    without either consumes nothing or cannot act — better to refuse at boot than to run."""
    env = {k: v for k, v in _WORKER_ENV.items() if not k.startswith(missing)}
    with pytest.raises(ValidationError):
        _boot(WorkerSettings, env)


def test_the_worker_boots_on_its_own_block_alone() -> None:
    """No auth, no superadmin allowlist, no frontend URL — the worker has no RBAC surface and
    serves no browser, so requiring them would be the union-of-everything problem again."""
    settings = _boot(WorkerSettings, _WORKER_ENV)
    assert isinstance(settings, WorkerSettings)
    assert settings.object_store is not None
    assert settings.redis is not None
    assert settings.sandbox is not None


def test_the_worker_profile_does_not_declare_api_only_fields() -> None:
    """Structural, not behavioural: if `auth` or `superadmin_emails` ever appear on the worker,
    the union-of-everything problem is back and the next operator dodges a gate again."""
    api_only = {"auth", "superadmin_emails", "FRONTEND_URL", "GOTENBERG_URL", "spa_dist_dir"}
    leaked = api_only & set(WorkerSettings.model_fields)
    assert leaked == set(), f"API-only fields leaked onto the worker profile: {sorted(leaked)}"


# ---------------------------------------------------------------- the API profile is unchanged


def test_the_api_profile_still_boots_without_the_optional_integrations() -> None:
    """Today's behaviour, preserved exactly: dev and test boot with no storage, no Redis, no
    sandbox. This refactor must not tighten the API's own boot requirements."""
    settings = _boot(ApiSettings, _API_ENV)
    assert isinstance(settings, ApiSettings)
    assert settings.object_store is None
    assert settings.redis is None


def test_the_api_production_gates_still_fire() -> None:
    """The seven prod gates are the API's existing contract and must survive the split."""
    env = {**_API_ENV, "ENVIRONMENT": "production", "FRONTEND_URL": "https://portal.example"}
    with pytest.raises(ValidationError) as excinfo:
        _boot(ApiSettings, env)
    assert "object storage must be configured in production" in str(excinfo.value)


# Each `REQUIRED IN PRODUCTION` field, in validator-declaration order, with the message its gate
# raises. `mode="after"` validators run in declaration order and the first raise wins, so each
# case supplies every block BEFORE it — which is what makes this prove the gates fire
# INDIVIDUALLY. The previous test only ever reached the first one; a gate deleted anywhere below
# it would not have shown up.
_PROD_GATES: list[tuple[str, dict[str, str], str]] = [
    ("object_store", {}, "object storage must be configured in production"),
    ("redis", {**_STORE}, "redis must be configured in production"),
    (
        "redis_tls",
        {**_STORE, **_REDIS},  # _REDIS is plaintext redis:// — that is the point
        "REDIS__URL must use the TLS scheme rediss:// in production",
    ),
    (
        "sandbox",
        {**_STORE, "REDIS__URL": "rediss://localhost:6380/0"},
        "sandbox must be configured in production",
    ),
    (
        "app_db",
        {**_STORE, "REDIS__URL": "rediss://localhost:6380/0", **_SANDBOX},
        "per-project databases must be configured in production",
    ),
]


@pytest.mark.parametrize(
    ("field", "supplied", "message"), _PROD_GATES, ids=[case[0] for case in _PROD_GATES]
)
def test_each_api_production_gate_fires_on_its_own(
    field: str, supplied: dict[str, str], message: str
) -> None:
    """One gate per case, each reached by satisfying the ones declared before it.

    A deleted gate is invisible without this: nothing fails, production simply boots
    misconfigured. Pydantic runs `mode="after"` validators in declaration order and the first
    raise wins, so a single-case test can only ever reach the first gate.
    """
    env = {
        **_API_ENV,
        **supplied,
        "ENVIRONMENT": "production",
        "FRONTEND_URL": "https://portal.example",
    }
    with pytest.raises(ValidationError) as excinfo:
        _boot(ApiSettings, env)
    assert message in str(excinfo.value), f"the {field} production gate did not fire"


def test_the_frontend_url_gate_rejects_the_dev_default_in_production() -> None:
    """FRONTEND_URL is the one field that is a KNOB whose production SHAPE is gated — it has a
    working default, so nothing else would catch a production boot still pointing at localhost.
    It feeds the sandbox frame-ancestors CSP and postMessage origins."""
    env = {
        **_API_ENV,
        **_STORE,
        "REDIS__URL": "rediss://localhost:6380/0",
        **_SANDBOX,
        **_APP_DB,
        "ENVIRONMENT": "production",
        # FRONTEND_URL deliberately omitted -> the http://localhost:5173 default applies.
    }
    with pytest.raises(ValidationError) as excinfo:
        _boot(ApiSettings, env)
    assert "FRONTEND_URL must be set to the portal's real https:// origin" in str(excinfo.value)


def test_both_roles_agree_on_the_shape_of_deploy() -> None:
    """`deploy` is the ONE field both manifests declare, so it is the one that can drift.

    The mixin layer guaranteed this structurally by construction; with a manifest per role it is
    two separate declarations, and a divergence (one gaining a default, a gate, or a different
    type) would be silent. Everything else in the worker's manifest is worker-only and has no twin.
    """
    api_field = ApiSettings.model_fields["deploy"]
    worker_field = WorkerSettings.model_fields["deploy"]
    assert api_field.annotation == worker_field.annotation
    assert api_field.is_required() == worker_field.is_required() is False


def test_the_api_still_requires_its_own_superadmin_allowlist() -> None:
    """`superadmin_emails` moved onto an API-only mixin; it must stay required THERE."""
    env = {k: v for k, v in _API_ENV.items() if k != "SUPERADMIN_EMAILS"}
    with pytest.raises(ValidationError) as excinfo:
        _boot(ApiSettings, env)
    assert "superadmin_emails" in str(excinfo.value).lower()


# ---------------------------------------------------------------- typo catching


@pytest.mark.parametrize(
    ("profile", "env"), [(ApiSettings, _API_ENV), (WorkerSettings, _WORKER_ENV)]
)
def test_a_mistyped_nested_key_fails_on_every_profile(
    profile: type[ApiSettings] | type[WorkerSettings], env: dict[str, str]
) -> None:
    """The teeth are the nested `ConfigDict(extra="forbid")` on each capability model, NOT the
    profile's own `extra="forbid"` — `EnvSettingsSource` only ever emits declared fields, so an
    unknown top-level environment variable never reaches the profile's extra check at all.

    Constructed inline rather than through `_boot`, whose constrained type parameter cannot take
    the union this test is parametrized over.
    """
    with (
        _environment_of({**env, **_STORE, "OBJECT_STORE__ACCOUNT_URLL": "typo"}),
        pytest.raises(ValidationError),
    ):
        profile(  # ty: ignore[missing-argument]
            _env_file=None  # ty: ignore[unknown-argument]  # pyright: ignore[reportCallIssue]
        )


def test_a_missing_required_field_names_the_field() -> None:
    """Fail-first is only useful if the message says what to set."""
    env = {k: v for k, v in _WORKER_ENV.items() if k != "DATABASE_URL"}
    with pytest.raises(ValidationError) as excinfo:
        _boot(WorkerSettings, env)
    assert "DATABASE_URL" in str(excinfo.value)


# ---------------------------------------------------------------- process reality


def test_the_worker_profile_builds_from_os_environ_in_a_fresh_interpreter() -> None:
    """On Python 3.14 the POSIX multiprocessing start method is `forkserver`, so a child
    inherits NOTHING — a settings object built in a lifespan or memoized in a parent does not
    survive into it. Every process must build its own profile from the environment alone, which
    only a fresh interpreter with a scrubbed environment can prove."""
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-B",
            "-c",
            "from src.settings import WorkerSettings;"
            " s = WorkerSettings();"
            " print('OK', s.object_store is not None, s.sandbox is not None)",
        ],
        cwd=_BACKEND_ROOT,
        env={"PATH": os.environ["PATH"], "ENV_FILE": "/nonexistent-on-purpose", **_WORKER_ENV},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "OK True True" in result.stdout


# ---------------------------------------------------------------- the compatibility shim


def test_the_config_shim_still_yields_a_working_api_settings_object() -> None:
    """105 modules do `from src.config import settings`. That import must keep working, keep
    resolving to the API profile by default, and keep type-checking as one."""
    from src.config import settings

    assert settings.ENVIRONMENT in {"development", "staging", "production"}
    assert settings.DATABASE_URL.get_secret_value()
    # Reached through the proxy's __getattr__ — an API-only field, proving the default role.
    assert settings.auth is not None


def test_the_shim_still_exports_the_names_the_codebase_imports() -> None:
    """`Settings` (4 call sites) and `FoundryConfig` (5) are imported from `src.config` today."""
    from src.config import FoundryConfig, Settings

    assert Settings is ApiSettings
    assert FoundryConfig.__name__ == "FoundryConfig"


def test_the_role_env_var_selects_the_worker_profile() -> None:
    """The proxy resolves its profile from BIAL_ROLE at first attribute access. Proven in a
    fresh interpreter because the profile is cached once resolved."""
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-B",
            "-c",
            # `resolve_settings()`, not `type(settings)` — `settings` is the proxy, and
            # `__class__` resolves by ordinary lookup rather than through its `__getattr__`.
            "from src.config import resolve_settings;"
            " print('PROFILE:' + type(resolve_settings()).__name__)",
        ],
        cwd=_BACKEND_ROOT,
        env={
            "PATH": os.environ["PATH"],
            "ENV_FILE": "/nonexistent-on-purpose",
            "BIAL_ROLE": "worker",
            **_WORKER_ENV,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "PROFILE:WorkerSettings" in result.stdout, result.stdout


def test_importing_the_shim_does_not_construct_settings() -> None:
    """The laziness is the whole reason the worker can have a narrower profile: 12 modules in
    the worker's import closure do `from src.config import settings`, so an eager module-level
    construction would build the API profile and demand auth in a process that has none.

    Proven with an environment too empty for ANY profile to construct: the import must still
    succeed, and the first attribute access must be what fails.
    """
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-B",
            "-c",
            "from src.config import settings\n"
            "print('IMPORTED')\n"
            "try:\n"
            "    settings.ENVIRONMENT\n"
            "    print('RESOLVED')\n"
            "except Exception as exc:\n"
            "    print('FAILED_ON_ACCESS:' + type(exc).__name__)\n",
        ],
        cwd=_BACKEND_ROOT,
        env={"PATH": os.environ["PATH"], "ENV_FILE": "/nonexistent-on-purpose"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert "IMPORTED" in result.stdout, (
        f"importing src.config constructed settings eagerly.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "FAILED_ON_ACCESS:ValidationError" in result.stdout, result.stdout
