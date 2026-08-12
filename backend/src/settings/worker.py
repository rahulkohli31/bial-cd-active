"""Everything the Taskiq worker needs to boot, in one list (ADR-0011, ADR-0029 §9).

Read this file to answer "which environment variables does the worker need?". Tiers are the ones
defined in `__init__.py`, and a field's tier is spelled by its SHAPE, never by a class name.

WHY THREE FIELDS ARE REQUIRED HERE AND ONLY PRODUCTION-GATED IN `api.py`. A gate keyed on
`ENVIRONMENT` is dodged by setting `ENVIRONMENT=development` — which is precisely what an operator
does when a new container will not boot, and the cheapest thing to try. For the API that dodge
costs a broken feature. For this process it can delete the Azure fleet (see `object_store` below).
So these fail in EVERY environment and cannot be talked out of.

DELIBERATELY ABSENT, and each absence is a decision:
  auth, superadmin_emails   no request to authenticate, no admin gate to enforce
  the portal knobs          no browser to serve, no origin to scope, no token budget to enforce
  foundry                   runs no model
  app_db                    reclamation reads the PRODUCT database via DATABASE_URL (core) to ask
                            "does a matching app record exist"; it never touches a per-project
                            database. Requiring a maintenance credential in a process that cannot
                            use it is the union-of-everything problem this split exists to remove.
                            A future provisioning role declares it in its own manifest.
"""

from __future__ import annotations

from typing import Self

from pydantic import model_validator

from src.services.deploy.config import DeployConfig
from src.services.redis.config import RedisConfig
from src.services.sandbox.config import SandboxConfig
from src.services.storage.config import StorageConfig
from src.settings.core import CoreSettings


class WorkerSettings(CoreSettings):
    """The worker role's complete settings manifest.

    Inherits `CoreSettings` and only `CoreSettings` — see the note in `api.py` on why a single base
    makes the `model_config` MRO clobber structurally unreachable.
    """

    # ============================================================ REQUIRED
    # No default, so a missing block fails construction in EVERY environment. This is the whole
    # point of the role split: read the three docstrings below before making any of them optional.

    # THE SINGLE MISCONFIGURATION THAT COULD DELETE THE FLEET. The durable-copy precondition (U14)
    # is unsatisfiable without a bundle store, and worse, `manager.py` reports an unconfigured
    # store as "CONFIRMED absent" — correct for its original caller (do not offer a restore that
    # cannot work) and catastrophic for a destroy path, which reads it as "nothing to preserve,
    # safe to delete". A worker booted without storage would delete every container in the
    # subscription while believing it had verified each one.
    object_store: StorageConfig

    # Both the task broker AND the spare-list. Without it the worker consumes nothing and can prove
    # nothing about ownership — a process that looks healthy and does no work, whose only symptom
    # is the Azure bill. Refusing at boot is strictly better.
    redis: RedisConfig

    # ARM access: how the fleet is enumerated and how containers are deleted. It also carries
    # `region`, which is not incidental — the ARM tag PATCH body requires `location`.
    sandbox: SandboxConfig

    # ============================================================ FEATURE SWITCH
    # Unset means the feature is OFF, legitimately, in every environment including production.

    # Present because deploy reconciliation runs in this process; it reaches ARM through this
    # block. Shape must match `ApiSettings.deploy` — pinned by a test, since the two are now
    # declared separately and could otherwise drift.
    deploy: DeployConfig | None = None

    # ============================================================ VALIDATORS

    @model_validator(mode="after")
    def _require_redis_tls_in_production(self) -> Self:
        # Same instance and same rule as the API — and this process carries the task stream over it
        # as well as the coordination keys. The check lives on `RedisConfig` so the two roles
        # cannot drift into two opinions about one DSN.
        if self.is_production:
            self.redis.require_tls()
        return self
