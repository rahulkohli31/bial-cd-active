"""Byte-stable C5 key-namespace tests.

The key strings are a cross-track contract, so these assert the exact formats recorded in
`docs/engineering/contracts/C5-redis-key-namespace.md` and that the sandbox families are provably
disjoint — a lock key can never be read as a heartbeat, registry or lease key.

R22 (ADR-0029) added the environment segment. The two properties that earns are asserted here
rather than inferred: keys for the SAME user never collide across environments, and no key can be
built for anything that is not a `uuid.UUID`. Everything about the dual-read WINDOW — the legacy
prefix, migration-on-read, the sweep's visibility across the cutover — lives in
`tests/services/build_sessions/test_key_migration.py`, because it needs a Redis.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import pytest

from src.config import settings
from src.services.redis.keys import (
    LEGACY_KEY_PREFIX,
    REGISTRY_FIELDS,
    REGISTRY_STATE_ENDING,
    REGISTRY_STATE_READY,
    heartbeat_key,
    key_prefix,
    lease_key,
    legacy_registry_key,
    lock_key,
    ns,
    registry_key,
    registry_scan_patterns,
)

# Two DISTINCT fixed UUIDs so format + disjointness assertions are deterministic.
_U1 = uuid.UUID("019f1c00-0000-7000-8000-000000000001")
_U2 = uuid.UUID("019f1c00-0000-7000-8000-000000000002")

# The suite runs under ENVIRONMENT=development (.env.test), so this is the live segment.
_ENV = "development"


# --- the frozen formats, character for character (C5 §"The five key families") ----------


def test_lock_key_format_is_byte_stable() -> None:
    assert lock_key(_U1) == f"bial:{_ENV}:sandbox:lock:{_U1}"


def test_heartbeat_key_format_is_byte_stable() -> None:
    assert heartbeat_key(_U1) == f"bial:{_ENV}:sandbox:heartbeat:{_U1}"


def test_registry_key_format_is_byte_stable() -> None:
    assert registry_key(_U1) == f"bial:{_ENV}:sandbox:registry:{_U1}"


def test_lease_key_format_is_byte_stable() -> None:
    # Family 4, RESERVED: U12 writes it. The format is pinned now so U12 inherits a decision
    # rather than re-opening one.
    assert lease_key(_U1) == f"bial:{_ENV}:sandbox:lease:{_U1}"


def test_key_prefix_is_the_environment_scoped_root() -> None:
    assert key_prefix() == f"bial:{_ENV}:sandbox:"


def test_legacy_prefix_is_the_pre_r22_root_verbatim() -> None:
    # Frozen as history, not as taste: it is what the live fleet was registered under, and
    # release B deletes it. If this string is wrong the dual-read reaches nothing.
    assert LEGACY_KEY_PREFIX == "bial:sandbox:"
    assert legacy_registry_key(_U1) == f"bial:sandbox:registry:{_U1}"


def test_all_sandbox_families_share_the_environment_scoped_root() -> None:
    for key in (lock_key(_U1), heartbeat_key(_U1), registry_key(_U1), lease_key(_U1)):
        assert key.startswith(key_prefix())


# --- disjointness ------------------------------------------------------------------------


def test_families_are_disjoint_for_one_user() -> None:
    # For the SAME user, no two family keys can ever be equal (the discriminator segment
    # differs), so a lock is never misread as a heartbeat, registry or lease.
    keys = {lock_key(_U1), heartbeat_key(_U1), registry_key(_U1), lease_key(_U1)}
    assert len(keys) == 4


def test_a_family_key_never_collides_across_users() -> None:
    assert lock_key(_U1) != lock_key(_U2)
    assert heartbeat_key(_U1) != heartbeat_key(_U2)
    assert registry_key(_U1) != registry_key(_U2)
    assert lease_key(_U1) != lease_key(_U2)


def test_one_users_key_is_never_another_families_key() -> None:
    # No lock key equals any heartbeat/registry/lease key for ANY pair of users — the families
    # cannot alias even across different owners.
    locks = {lock_key(_U1), lock_key(_U2)}
    beats = {heartbeat_key(_U1), heartbeat_key(_U2)}
    regs = {registry_key(_U1), registry_key(_U2)}
    leases = {lease_key(_U1), lease_key(_U2)}
    for a, b in ((locks, beats), (locks, regs), (locks, leases), (beats, regs), (beats, leases)):
        assert a.isdisjoint(b)
    assert regs.isdisjoint(leases)


# --- R22: the environment segment is the whole point --------------------------------------


def test_keys_never_collide_across_environments_for_the_same_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE R22 PROPERTY. Production reuses a Redis shared with other BIAL applications, so a
    process pointed at the wrong instance must not be able to read — later, delete — another
    environment's containers. One user, three environments, nine keys, no overlap."""
    built: list[str] = []
    for environment in ("development", "staging", "production"):
        monkeypatch.setattr(settings, "ENVIRONMENT", environment)
        built.extend([lock_key(_U1), heartbeat_key(_U1), registry_key(_U1), lease_key(_U1)])
    assert len(set(built)) == len(built) == 12


def test_the_environment_segment_is_read_per_call_not_frozen_at_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A module-level constant would bake the environment in at import time, and import order is
    not something a deployment controls. The builders resolve it on every call."""
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    assert registry_key(_U1) == f"bial:production:sandbox:registry:{_U1}"
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    assert registry_key(_U1) == f"bial:development:sandbox:registry:{_U1}"


def test_the_scan_patterns_are_literals_and_never_wildcard_the_environment() -> None:
    """A single widened glob (`bial:*:sandbox:registry:*`) would reach into OTHER environments —
    the exact hazard R22 closes — so the sweep enumerates literal patterns instead. During the
    dual-read window that is two: the current one and the legacy one."""
    patterns = registry_scan_patterns()
    assert patterns == (f"bial:{_ENV}:sandbox:registry:*", "bial:sandbox:registry:*")
    for pattern in patterns:
        head = pattern.removesuffix("registry:*")
        assert "*" not in head, f"{pattern} wildcards a segment above the user id"


# --- the type IS the boundary --------------------------------------------------------------


@pytest.mark.parametrize(
    "builder", [lock_key, heartbeat_key, registry_key, lease_key, legacy_registry_key]
)
def test_a_builder_refuses_anything_that_is_not_a_uuid(
    builder: Callable[[uuid.UUID], str],
) -> None:
    """A `str` user id is the one input that could forge a different family or a different
    ENVIRONMENT — `"…:registry:evil"` crosses a segment boundary, and so would a leading
    `production:`. The annotation already says `uuid.UUID`; this makes the runtime say it too,
    because the caller on the far side of a JSON body or an ARM tag is not type-checked.

    The suppressions below are the test: it exists precisely to call the function wrongly."""
    for forged in (
        str(_U1),
        "019f1c00-0000-7000-8000-000000000001:registry:evil",
        "production:sandbox:registry:019f1c00-0000-7000-8000-000000000001",
    ):
        with pytest.raises(TypeError):
            builder(forged)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]  # pyright: ignore[reportArgumentType]


def test_ns_is_the_choke_point_every_builder_goes_through() -> None:
    assert ns("lock", _U1) == lock_key(_U1)
    assert ns("heartbeat", _U1) == heartbeat_key(_U1)
    assert ns("registry", _U1) == registry_key(_U1)
    assert ns("lease", _U1) == lease_key(_U1)


# --- the registry hash's own frozen surface -------------------------------------------------


def test_registry_fields_are_the_frozen_c5_set() -> None:
    """A CHOKE POINT, and it earned its keep in U13: adding `stay_writer` turned it red on the
    full run, which is the only reason C5's field table and this list did not drift apart. Update
    the contract and this literal in the same change, never one of them."""
    assert REGISTRY_FIELDS == frozenset(
        {
            "app_name",
            "fqdn",
            "token_ref",
            "created_at",
            "state",
            "preview_stay_until",
            # U13/R13 — which named writer last moved the stay. Provenance only; nothing
            # branches on it. A deadline no operator can attribute is the state R13 removes.
            "stay_writer",
        }
    )


def test_registry_states_are_the_two_frozen_values() -> None:
    assert REGISTRY_STATE_READY == "ready"
    assert REGISTRY_STATE_ENDING == "ending"
