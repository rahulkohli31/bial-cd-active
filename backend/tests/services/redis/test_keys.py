"""Byte-stable C5 key-namespace tests. The key strings are a cross-track contract,
so these assert the exact frozen formats and that the three families are provably
disjoint — a lock key can never be read as a heartbeat or registry key.
"""

from __future__ import annotations

import uuid

from src.services.redis.keys import (
    KEY_PREFIX,
    REGISTRY_FIELDS,
    REGISTRY_STATE_ENDING,
    REGISTRY_STATE_READY,
    heartbeat_key,
    lock_key,
    registry_key,
)

# Two DISTINCT fixed UUIDs so format + disjointness assertions are deterministic.
_U1 = uuid.UUID("019f1c00-0000-7000-8000-000000000001")
_U2 = uuid.UUID("019f1c00-0000-7000-8000-000000000002")


def test_lock_key_format_is_byte_stable() -> None:
    assert lock_key(_U1) == f"bial:sandbox:lock:{_U1}"


def test_heartbeat_key_format_is_byte_stable() -> None:
    assert heartbeat_key(_U1) == f"bial:sandbox:heartbeat:{_U1}"


def test_registry_key_format_is_byte_stable() -> None:
    assert registry_key(_U1) == f"bial:sandbox:registry:{_U1}"


def test_all_families_share_the_reserved_prefix() -> None:
    for key in (lock_key(_U1), heartbeat_key(_U1), registry_key(_U1)):
        assert key.startswith(KEY_PREFIX)


def test_families_are_disjoint_for_one_user() -> None:
    # For the SAME user, no two family keys can ever be equal (the discriminator
    # segment differs), so a lock is never misread as a heartbeat or registry.
    keys = {lock_key(_U1), heartbeat_key(_U1), registry_key(_U1)}
    assert len(keys) == 3


def test_a_family_key_never_collides_across_users() -> None:
    # Distinct users get distinct keys within every family (the user_id segment).
    assert lock_key(_U1) != lock_key(_U2)
    assert heartbeat_key(_U1) != heartbeat_key(_U2)
    assert registry_key(_U1) != registry_key(_U2)


def test_one_users_key_is_never_another_families_key() -> None:
    # No lock key equals any heartbeat/registry key for ANY pair of users — the
    # families cannot alias even across different owners.
    locks = {lock_key(_U1), lock_key(_U2)}
    beats = {heartbeat_key(_U1), heartbeat_key(_U2)}
    regs = {registry_key(_U1), registry_key(_U2)}
    assert locks.isdisjoint(beats)
    assert locks.isdisjoint(regs)
    assert beats.isdisjoint(regs)


def test_registry_fields_are_the_frozen_c5_set() -> None:
    assert REGISTRY_FIELDS == frozenset(
        {"app_name", "fqdn", "token_ref", "created_at", "state", "preview_stay_until"}
    )


def test_registry_states_are_the_two_frozen_values() -> None:
    assert REGISTRY_STATE_READY == "ready"
    assert REGISTRY_STATE_ENDING == "ending"
