"""Everything the Taskiq worker needs to boot, in one list. Tiers are defined in `__init__.py`,
spelled by a field's SHAPE, never a class name.

THREE FIELDS ARE REQUIRED HERE AND ONLY PRODUCTION-GATED IN `api.py`: an `ENVIRONMENT` gate is
dodged by setting `ENVIRONMENT=development` — the cheapest thing an operator tries when a
container won't boot. That costs the API a broken feature; here it costs containers, so these
fail in EVERY environment and cannot be talked out of.

DELIBERATELY ABSENT: auth (nothing to authenticate), portal knobs (no browser to serve), foundry
(runs no model), and app_db — reclamation reads the product DB via `DATABASE_URL`, never a
per-project one, so a maintenance credential here is the union-of-everything problem this split
exists to remove; a future provisioning role declares it in its own manifest."""

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

    # Required in EVERY environment, unlike the API's production gate: the reclamation pass runs
    # in this process, and nothing it destroys can be checked against a recovery bundle without
    # this store. A worker booted without one deletes containers blind.
    object_store: StorageConfig

    # Both the task broker AND the spare-list. Without it the worker consumes nothing and can prove
    # nothing about ownership — a process that looks healthy and does no work, whose only symptom
    # is the Azure bill. Refusing at boot is strictly better.
    redis: RedisConfig

    # ARM access: how the fleet is enumerated and how containers are deleted. It also carries
    # `region`, which is not incidental — the ARM tag PATCH body requires `location`.
    sandbox: SandboxConfig

    # ============================================================ REQUIRED, AND A SWITCH
    # A PLAIN FIELD RATHER THAN A NESTED BLOCK. The nested blocks above are subsystems with
    # credentials and connections; this is a policy the operator turns on. It has NO DEFAULT for
    # the same reason the three above do not: a deployment that has not decided whether it deletes
    # its citizens' conversations must refuse to boot rather than pick an answer for them.
    #
    # It ships OFF. Nothing has ever deleted a conversation here, so the first enabled pass is not
    # a weekly increment — its candidate set is the whole historical backlog, Plan and Build chats
    # included. Turning it on is a decision somebody makes once, having read that.
    conversation_retention_enabled: bool

    # ============================================================ FEATURE SWITCH
    # Unset means the feature is OFF, legitimately, in every environment including production.

    # Present because deploy reconciliation runs in this process; it reaches ARM through this
    # block. Shape must match `ApiSettings.deploy` — pinned by a test, since the two are now
    # declared separately and could otherwise drift.
    deploy: DeployConfig | None = None

    # ============================================================ KNOBS
    # Defaults that are correct answers rather than placeholders — each says what it means.

    #: How long a conversation may sit untouched before the pass condemns it. Seven days, which
    #: is the policy; it is a field rather than a constant so an operator can widen it without a
    #: deploy while the backlog drains.
    conversation_retention_days: int = 7

    #: How many conversations ONE pass may remove. The whole pass is one transaction, so a run
    #: cancelled by a deploy drain rolls back entirely and starts from zero on the next tick —
    #: without a ceiling, a first pass over the historical backlog could do that indefinitely and
    #: never commit anything. What the cap leaves behind is counted and recorded, not dropped.
    conversation_retention_per_pass: int = 500

    # ============================================================ VALIDATORS

    @model_validator(mode="after")
    def _require_redis_tls_in_production(self) -> Self:
        # Same instance and same rule as the API — and this process carries the task stream over it
        # as well as the coordination keys. The check lives on `RedisConfig` so the two roles
        # cannot drift into two opinions about one DSN.
        if self.is_production:
            self.redis.require_tls()
        return self
