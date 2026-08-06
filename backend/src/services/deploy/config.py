"""Auto-deploy configuration — the V4 Part 3 kill switch.

`Settings.deploy` is ALWAYS present (unlike `SandboxConfig`/`RedisConfig`/etc.,
which are `| None` genuinely-optional integrations gated in production) because
this config has no required fields and a safe default: `auto_deploy_enabled`
defaults to `False`, so a deployment that never sets `DEPLOY__AUTO_DEPLOY_ENABLED`
gets the same "nothing auto-deploys" behavior it had before this feature existed.
Flipping it on is a deliberate, separate operational decision — never a side
effect of upgrading this codebase.

Auto-deploy reuses `SandboxConfig`'s Azure targeting (subscription/resource
group/managed environment/image/ACR credentials) rather than duplicating a
second full `DEPLOY__*` block — same identity, same golden image, same
Managed Environment as the interactive build sandboxes. `services/deploy` is
therefore only meaningful when `Settings.sandbox` is ALSO configured; the
reconcile endpoint's own guard covers that (a `None` sandbox config makes it a
503, not a 500 from an unguarded attribute access).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class DeployConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # DEPLOY__AUTO_DEPLOY_ENABLED. False until someone deliberately turns this on —
    # the ONLY thing standing between an auto-approved app and real, billable,
    # publicly-reachable Azure infrastructure is this flag.
    auto_deploy_enabled: bool = False
