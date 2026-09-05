"""One-click publish configuration model.

`Settings.deploy` is typed `DeployConfig | None`; pydantic-settings validates one
`DEPLOY__*` env block against it (the single config funnel). Publishing is a
genuinely-optional integration — `| None` means "publishing is off", which is the correct
dev/test posture and a legitimate staging one.

**No production gate, deliberately** — this follows the `foundry` precedent
(`src/config.py`), not the `sandbox`/`redis`/`object_store` one. A prod gate would make the
running production backend fail to boot the moment this merges, for a capability nobody has
turned on yet. Add `_require_deploy_in_production` in the same commit that makes the portal
show a Deploy control unconditionally; until then a gate would fail-first for a feature that
is not wired.

WHY THE ACR FIELDS ARE DUPLICATED FROM `SandboxConfig` RATHER THAN HOISTED INTO A SHARED
BLOCK. Three reasons, in order of weight:

1. Hoisting is a breaking config change with no safe intermediate. `SandboxConfig` sets
   `extra="forbid"`, so a leftover `SANDBOX__ACR_SERVER` during a rollout is a startup
   `ValidationError` — production down at boot. The code deploy and the App Service settings
   edit would have to be atomic. Duplication is purely additive and rollout-safe.
2. It makes "deploy must never read `settings.sandbox.*`" structural rather than a
   discipline. `settings.sandbox` is `None` in dev and test — a supported posture — so any
   cross-read would crash exactly where it is least expected.
3. The two consumers want different credentials in the end state: the sandbox needs
   pull-only (and should move to AcrPull via managed identity), while publish needs ARM
   rights to schedule a build plus pull creds to hand to ACA.

The cost is the ACR password configured twice. That is one extra line in the deployment's
env block, and it buys a rollout that cannot break the sandbox.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    PositiveFloat,
    PositiveInt,
    SecretStr,
    model_validator,
)

# The Next.js standalone server binds this port inside the published container. It is NOT
# the sandbox's 8080: a published container runs `node server.js` directly, with no Caddy
# and no supervisor in front of it.
DEFAULT_TARGET_PORT = 3000


class DeployConfig(BaseModel):
    """Where published apps are built (ACR) and where they run (ACA).

    Targeting fields are required with no default (fail-first); sizing, scale and timeout
    knobs carry defined defaults.
    """

    # `extra="forbid"` makes a mistyped DEPLOY__* nested key fail at startup instead of
    # silently defaulting (fail-first).
    model_config = ConfigDict(extra="forbid")

    # --- ACR: where the image is built ------------------------------------------------
    #
    # `acr_server` and `acr_name` are DIFFERENT things and conflating them is the single
    # most likely misconfiguration here: `acr_server` is the login HOST that goes into an
    # image reference and an ACA registry credential (`bialgenaicr.azurecr.io`), while
    # `acr_name` is the ARM RESOURCE name that ACR Tasks calls address
    # (`bialgenaicr`). The validator below catches the swap.
    acr_server: str
    acr_name: str
    # The registry frequently lives in a DIFFERENT resource group and even a different
    # subscription from the ACA environment (at BIAL: the registry is in the AI/ML group
    # while the runtime is in the dev group), so these are separate fields rather than
    # reusing the ACA targeting below.
    acr_resource_group: str
    acr_subscription_id: str
    # Pull credentials handed to ACA as a registry secret so it can pull the private image.
    # NOT the credentials used to schedule a build — that is managed identity, and ACR
    # admin credentials deliberately do not authorize it.
    acr_username: str
    acr_password: SecretStr

    # WHICH BUILDER runs. `acr_tasks` is the shipping path (`images.py`): the registry's own
    # agent builds and pushes, so the control plane needs no docker daemon. `local_docker`
    # (`local_images.py`) exists for one reason — a subscription that answers every
    # `scheduleRun` with `TasksOperationsNotAllowed`, which makes the entire publish half of
    # the product untestable. It defaults to the shipping path and is refused in production
    # (`settings/api.py`), because a BIAL host quietly shelling out to docker would be a
    # worse outcome than a failed publish.
    image_builder: Literal["acr_tasks", "local_docker"] = "acr_tasks"

    # --- ACA: where the app runs ------------------------------------------------------
    subscription_id: str
    resource_group: str
    region: str
    managed_environment_name: str

    # --- image naming -----------------------------------------------------------------
    # Repository namespace inside the registry: `{prefix}/{app_id}:{tag}`. A defined
    # default — a deployment may want its own namespace, but there is a correct value.
    image_repository_prefix: str = "citizen-apps"
    # The build's base image. A knob so ops can pin a digest (or move off Debian) without a
    # code change; it is interpolated into the platform Dockerfile as a build arg.
    #
    # THIS is the value that ships — `images.py` sends it as NODE_IMAGE on every build, so it
    # always beats the Dockerfile's own ARG default. A test pins the two to the identical
    # string; they are only allowed to drift on purpose.
    #
    # Digest-pinned to the INDEX (not a per-architecture child), so it resolves per host: the
    # same string builds arm64 on a developer machine and amd64 in the registry. Moved off
    # Debian 12, which left regular security support on 12 July 2026; matches the sandbox base
    # deliberately, so the environment citizens build in and the one their apps run in do not
    # diverge. Resolved 2026-08-13 — ops/CVE-REMEDIATION-ROLLBACK-ANCHOR.md §1.2 records what
    # this moved from and the commands to reproduce it.
    #
    # As an env var this is DEPLOY__NODE_BASE_IMAGE; a value set on the deployed backend wins
    # over this default, so a code change alone does not reach a running environment.
    node_base_image: str = (
        "node:24-trixie-slim@sha256:"
        "0711b541c1c33a8a530ac4f0d391baa9a15b3d804695b1b24a47daa5fb60e74d"
    )

    # --- published-app runtime shape ---------------------------------------------------
    cpu: PositiveFloat = 0.5
    memory: str = "1Gi"
    # SCALE TO ZERO is the decision that makes this affordable and, more importantly,
    # survivable: a sleeping app costs nothing AND consumes no environment cores and no
    # node pressure on an infrastructure subnet that is at ACA's documented floor. The
    # price is a cold start on the first request after an idle period.
    min_replicas: int = 0
    # Capped low on purpose. Every replica opens its own database pool against a shared
    # PostgreSQL server with a fixed connection budget (the template pins its pool small
    # for the same reason) — an uncapped fan-out spends every other app's headroom.
    max_replicas: PositiveInt = 2
    # `external` at the APP level, same as sandbox/config.py's identical default on the
    # same managed environment (`bial-citizen-dev-aca-env`) — verified: this repo's own
    # config, reachable outside the Container Apps environment itself.
    #
    # UNCONFIRMED: whether the managed ENVIRONMENT is itself VNet-integrated with an
    # internal load balancer, which would restrict this to the corporate network
    # regardless of the app-level setting — ACA's `external: true` means "reachable
    # outside the environment", not "reachable from the public internet unconditionally".
    # Settle it against the live resource before relying on either posture for a security
    # decision:
    #   az containerapp env show -n bial-citizen-dev-aca-env -g <rg> \
    #     --query "{internal: properties.vnetConfiguration.internal}"
    # Until confirmed, treat published apps as POTENTIALLY reachable on the public
    # internet by anyone with the URL, not just staff — the safer assumption.
    ingress: Literal["external", "internal"] = "external"
    target_port: PositiveInt = DEFAULT_TARGET_PORT

    # --- scheduled reconciliation ------------------------------------------------------
    # Whether the SCHEDULED deploy reconciliation runs on the Taskiq worker (U6, ADR-0011).
    #
    # SHIPS ON, because the thing it replaced was never optional. This defaulted to False while
    # `_reconcile_deploys_periodically` was still looping in the API lifespan — correct then: the
    # loop was doing the work and this flag was how an operator promoted the scheduled pass after
    # watching it run. U15 deleted the loop, and at that moment an off default stopped meaning
    # "not yet promoted" and started meaning "nobody reconciles a deploy that straddled a
    # restart". A pipeline runs for minutes and every platform deploy kills it, so that is the
    # expected case during a rollout — the exact leak U6 was written to close.
    #
    # An optional knob with a defined meaning (the fail-first exception): False = the cron tick
    # logs `deploy_reconcile_pass_disabled` and returns, having imported nothing. It stays a real
    # kill switch; what changed is which way it points when nobody has said anything.
    #
    # It gates the CLOCK only. The boot one-shot (`main.py::_reconcile_interrupted_deploys`) and
    # the operator endpoint (`POST /v1/admin/apps/reconcile-deploys`) are deliberately
    # unaffected: an operator who has switched the timer off must still be able to settle a
    # wedged deploy by hand, which is the whole reason the endpoint exists.
    reconcile_enabled: bool = True

    # --- timeouts ---------------------------------------------------------------------
    # Every one of these bounds a remote call that has no timeout of its own. The ARM
    # long-running-operation poller in particular blocks a worker thread from a small
    # shared pool, so an unbounded wait there stalls unrelated work process-wide.
    build_timeout_s: PositiveInt = 600
    build_poll_interval_s: PositiveFloat = 5.0
    provision_timeout_s: PositiveInt = 300
    ready_timeout_s: PositiveInt = 180

    @model_validator(mode="after")
    def _acr_host_matches_the_resource_name(self) -> Self:
        # Catches the swap described above. STATIC message only — never interpolate a
        # configured value (pydantic reflects validator messages into ValidationError, and
        # thus into logs).
        if self.acr_server.split(".", 1)[0] != self.acr_name:
            raise ValueError(
                "DEPLOY__ACR_SERVER and DEPLOY__ACR_NAME disagree: the server is the login "
                "HOST (<registry>.azurecr.io) and the name is the ARM RESOURCE name "
                "(<registry>). The host's first label must equal the name."
            )
        return self
