"""`DeployConfig` — the DEPLOY__* block.

Two things are worth a test rather than a comment. The ACR host/name validator catches the
single most likely misconfiguration in this block (they look interchangeable and are not).
And the absence of a production gate is a DECISION, not an omission — pinned here so that
adding one is deliberate, with a failing test pointing at the reason.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from src.services.deploy.config import DeployConfig

_VALID: dict[str, Any] = {
    "acr_server": "bialgenaicr.azurecr.io",
    "acr_name": "bialgenaicr",
    "acr_resource_group": "BIAL-GENAI-AIML-RG",
    "acr_subscription_id": "00000000-0000-0000-0000-000000000000",
    "acr_username": "bialgenaicr",
    "acr_password": "shh",
    "subscription_id": "00000000-0000-0000-0000-000000000000",
    "resource_group": "BIAL-GENAI-DEV-RG",
    "region": "centralindia",
    "managed_environment_name": "bial-citizen-dev-aca-env",
}


def _cfg(**overrides: Any) -> DeployConfig:
    """Build a config from the valid block plus overrides.

    The dict is materialized into an explicitly-typed local first: spread inline at the call
    site, the type checkers infer each value's literal type and then reject the very
    substitutions these tests exist to make."""
    values: dict[str, Any] = {**_VALID, **overrides}
    return DeployConfig(**values)


def _cfg_without(missing: str) -> DeployConfig:
    values: dict[str, Any] = {k: v for k, v in _VALID.items() if k != missing}
    return DeployConfig(**values)


def test_a_complete_block_validates() -> None:
    config = _cfg()
    assert config.acr_name == "bialgenaicr"
    assert config.acr_password.get_secret_value() == "shh"


def test_the_acr_host_and_resource_name_must_agree() -> None:
    """The login HOST (`bialgenaicr.azurecr.io`) and the ARM RESOURCE name
    (`bialgenaicr`) are different things used by different APIs, and swapping them produces
    an authorization failure hours later rather than a startup error."""
    with pytest.raises(ValidationError) as caught:
        _cfg(acr_name="someotherregistry")
    assert "DEPLOY__ACR_SERVER and DEPLOY__ACR_NAME disagree" in str(caught.value)


def test_the_validator_message_never_echoes_a_configured_value() -> None:
    """pydantic reflects validator messages into ValidationError, and thus into logs."""
    with pytest.raises(ValidationError) as caught:
        _cfg(acr_name="leaky-registry-name")
    assert "leaky-registry-name" not in str(caught.value)


def test_a_mistyped_key_fails_at_startup() -> None:
    """`extra="forbid"` — a typo must crash the boot, never silently default."""
    with pytest.raises(ValidationError):
        _cfg(acr_serverr="typo")


@pytest.mark.parametrize("missing", sorted(_VALID))
def test_every_targeting_field_is_required(missing: str) -> None:
    with pytest.raises(ValidationError):
        _cfg_without(missing)


# --- the defaults that are decisions ------------------------------------------------


def test_published_apps_scale_to_zero_by_default() -> None:
    """A sleeping app costs nothing AND holds no environment cores or node pressure on an
    infrastructure subnet that is already at ACA's documented floor."""
    assert _cfg().min_replicas == 0


def test_the_replica_ceiling_is_low() -> None:
    """Every replica opens its own pool against a shared PostgreSQL server with a fixed
    connection budget. An uncapped fan-out spends every other app's headroom."""
    assert _cfg().max_replicas == 2


def test_the_published_port_is_not_the_sandbox_port() -> None:
    """A published container runs `node server.js` directly — there is no Caddy and no
    supervisor in front of it, so 8080 would point at nothing."""
    assert _cfg().target_port == 3000


def test_every_remote_call_has_a_bound() -> None:
    config = _cfg()
    assert config.build_timeout_s > 0
    assert config.provision_timeout_s > 0
    assert config.ready_timeout_s > 0


# --- the deliberate absence of a production gate -------------------------------------


def test_production_boots_without_a_deploy_block() -> None:
    """DECISION, following the `foundry` precedent rather than sandbox/redis/storage: a
    prod gate would make the production backend fail to boot the moment this merges, for a
    capability nobody has enabled. Add `_require_deploy_in_production` in the same commit
    that makes the portal show a Deploy control unconditionally."""
    from src.config import Settings

    gates = [name for name in dir(Settings) if "require_deploy" in name]
    assert gates == [], (
        "a production gate was added for `deploy` — that is a real decision, so update this "
        "test and the comment in src/config.py together"
    )
