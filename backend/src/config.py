"""The front door to application configuration.

The settings themselves live in `src/settings/` (U23, ADR-0029 §9): a `CoreSettings` of what every
process needs, one mixin per capability, and two role profiles. This module stays because 105
modules import `settings` from it, and it resolves WHICH profile this process gets.

WHY `settings` IS LAZY, AND WHY THAT IS LOAD-BEARING
----------------------------------------------------
It used to be a module-level `settings = Settings()`. That is what made the whole settings object
the union of every subsystem's needs: twelve modules in the *worker's* import closure —
`db/base.py`, `redis/client.py`, `sandbox/client.py`, `storage/accessor.py` among them — do
`from src.config import settings`, so an eager construction would build the API profile inside the
worker and demand an Entra client id, a super-admin allowlist and a frontend URL in a process that
has no request to authenticate and serves no browser.

The operator's response to a container that will not boot is to trim environment variables until
it does, and the cheapest trim is `ENVIRONMENT=development` — which switches off every production
gate at once. That is the most dangerous configuration this platform can be in: with object
storage unconfigured, the durable-copy check answers "CONFIRMED absent" for every container, and
the reclamation destroy path (U14) reads that as "nothing to preserve, safe to delete".

So construction is deferred to first attribute access, by which time the process knows its role
(`BIAL_ROLE`). The worker gets `WorkerSettings`, which **requires** object storage, Redis and ARM
access in every environment — a guarantee no `ENVIRONMENT` value can dodge.

TYPING
------
`settings` is annotated as `ApiSettings` because that is what it is in the API process, which is
every call site that reads an API-only field. In a worker process the underlying object is a
`WorkerSettings`, so reading an API-only field there raises `AttributeError` at runtime rather
than failing a type check. That is a deliberate, single-line trade: the alternative was migrating
105 call sites off the global. Worker code should read its own profile via
`src.settings.profiles.WorkerSettings` when it needs to be explicit.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from src.settings.capabilities import FoundryConfig as FoundryConfig
from src.settings.profiles import ApiSettings, WorkerSettings, build_profile

if TYPE_CHECKING:  # pragma: no cover - typing only
    pass

# Back-compat alias: four modules do `from src.config import Settings`. The API profile IS the
# settings object those call sites mean.
Settings = ApiSettings

_profile: ApiSettings | WorkerSettings | None = None


def resolve_settings() -> ApiSettings | WorkerSettings:
    """Build this process's profile once, then hand back the same object.

    Not `functools.cache`d: the memo is a plain module global so a test can reset it, and so the
    resolution point is obvious to a reader chasing a boot failure.
    """
    global _profile
    if _profile is None:
        _profile = build_profile()
    return _profile


class _SettingsProxy:
    """Forwards every attribute to the role's profile, constructing it on first access.

    `__getattr__` runs only when normal lookup fails, and this class defines no instance state, so
    every read goes through it. Deliberately NOT a `__getattr__`-on-the-module trick: `from
    src.config import settings` binds the object at the consumer's import time, so a module-level
    hook would resolve just as eagerly as the old global did.
    """

    __slots__ = ()

    def __getattr__(self, name: str) -> Any:
        return getattr(resolve_settings(), name)

    def __setattr__(self, name: str, value: Any) -> None:
        # Forwarded, not stored. `monkeypatch.setattr(settings, "spa_dist_dir", ...)` is used
        # widely across the suite, and a proxy that swallowed the write would leave every such
        # test asserting against the unpatched value — a silent false pass, which is worse than
        # the AttributeError a read-only proxy would raise.
        setattr(resolve_settings(), name, value)

    def __delattr__(self, name: str) -> None:
        delattr(resolve_settings(), name)

    def __dir__(self) -> list[str]:
        # So `dir(settings)` and interactive completion show the profile's fields rather than the
        # proxy's empty surface.
        return dir(resolve_settings())

    def __repr__(self) -> str:
        # Never eagerly resolve just to render a repr — a logger touching this must not be able to
        # trigger a boot failure.
        state = "unresolved" if _profile is None else type(_profile).__name__
        return f"<settings proxy: {state}>"


# The one type assertion this design costs. See TYPING above.
settings = cast(ApiSettings, _SettingsProxy())
