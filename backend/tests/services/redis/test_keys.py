"""Byte-stable key-namespace tests.

The key strings are a cross-track contract, so these assert the exact formats byte-for-byte,
and that the sandbox families are provably disjoint — a lock key can never be read as a
heartbeat, registry or lease key.

Two properties of the environment segment are asserted here rather than inferred: keys for the
SAME user never collide across environments, and no key can be built for anything that is not a
`uuid.UUID`. Everything about the dual-read window — the legacy prefix, migration-on-read, the
sweep's visibility across the cutover — lives in `test_key_migration.py`, because it needs a
Redis.
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
    lake_file_key,
    lake_index_key,
    lake_key_prefix,
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


# --- the frozen formats, character for character ----------------------------------------


def test_lock_key_format_is_byte_stable() -> None:
    assert lock_key(_U1) == f"bial:{_ENV}:sandbox:lock:{_U1}"


def test_heartbeat_key_format_is_byte_stable() -> None:
    assert heartbeat_key(_U1) == f"bial:{_ENV}:sandbox:heartbeat:{_U1}"


def test_registry_key_format_is_byte_stable() -> None:
    assert registry_key(_U1) == f"bial:{_ENV}:sandbox:registry:{_U1}"


def test_lease_key_format_is_byte_stable() -> None:
    assert lease_key(_U1) == f"bial:{_ENV}:sandbox:lease:{_U1}"


def test_key_prefix_is_the_environment_scoped_root() -> None:
    assert key_prefix() == f"bial:{_ENV}:sandbox:"


def test_legacy_prefix_matches_the_original_root_verbatim() -> None:
    # If this string drifts the dual-read reaches nothing. `keys.py` records why it is frozen.
    assert LEGACY_KEY_PREFIX == "bial:sandbox:"
    assert legacy_registry_key(_U1) == f"bial:sandbox:registry:{_U1}"


def test_all_sandbox_families_share_the_environment_scoped_root() -> None:
    for key in (lock_key(_U1), heartbeat_key(_U1), registry_key(_U1), lease_key(_U1)):
        assert key.startswith(key_prefix())


# --- disjointness ------------------------------------------------------------------------


def test_families_are_disjoint_for_one_user() -> None:
    keys = {lock_key(_U1), heartbeat_key(_U1), registry_key(_U1), lease_key(_U1)}
    assert len(keys) == 4


def test_a_family_key_never_collides_across_users() -> None:
    assert lock_key(_U1) != lock_key(_U2)
    assert heartbeat_key(_U1) != heartbeat_key(_U2)
    assert registry_key(_U1) != registry_key(_U2)
    assert lease_key(_U1) != lease_key(_U2)


def test_one_users_key_is_never_another_families_key() -> None:
    locks = {lock_key(_U1), lock_key(_U2)}
    beats = {heartbeat_key(_U1), heartbeat_key(_U2)}
    regs = {registry_key(_U1), registry_key(_U2)}
    leases = {lease_key(_U1), lease_key(_U2)}
    for a, b in ((locks, beats), (locks, regs), (locks, leases), (beats, regs), (beats, leases)):
        assert a.isdisjoint(b)
    assert regs.isdisjoint(leases)


# --- the connector data plane sits in its OWN domain ----------------------------------------
#
# `sandbox:` is scanned by a job that DELETES AZURE CONTAINERS on the strength of what it finds,
# and everything under it is read as a claim about a container. The lake families are file blobs,
# so they sit in a peer domain — and these assertions are what stop a later "tidy-up" from folding
# them back in.

# A sha256 hex digest of a blob name, as `transfer.py` builds it. Written out rather than computed
# so the format assertion below cannot be satisfied by whatever the production code happens to do.
_DIGEST = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_lake_key_formats_are_byte_stable() -> None:
    assert lake_key_prefix() == f"bial:{_ENV}:lake:"
    assert lake_index_key() == f"bial:{_ENV}:lake:index"
    assert lake_file_key(_DIGEST) == f"bial:{_ENV}:lake:file:{_DIGEST}"


def test_the_lake_domain_is_a_peer_of_the_sandbox_domain_not_a_family_inside_it() -> None:
    """They agree on everything above the domain segment and differ from there down, so neither
    can ever be a prefix of the other however the environment is spelled — and a fleet sweep's
    `bial:{env}:sandbox:*` literal cannot reach a parquet copy."""
    assert lake_key_prefix().removesuffix("lake:") == key_prefix().removesuffix("sandbox:")
    assert not lake_key_prefix().startswith(key_prefix())
    assert not key_prefix().startswith(lake_key_prefix())
    for pattern in registry_scan_patterns():
        assert not lake_index_key().startswith(pattern.rstrip("*"))
        assert not lake_file_key(_DIGEST).startswith(pattern.rstrip("*"))


@pytest.mark.parametrize(
    "not_a_digest",
    [
        "",
        "AOS/tb_flight_fact_report/2026/SEPTEMBER/a.parquet",  # the raw blob name
        "e3b0c442",  # too short
        _DIGEST.upper(),  # hex, but not the spelling we write
        _DIGEST + "0",  # too long
        _DIGEST[:-1] + ":",  # a separator smuggled in at the end
    ],
    ids=["empty", "raw-name", "too-short", "uppercase", "too-long", "smuggled-colon"],
)
def test_a_lake_file_key_refuses_anything_that_is_not_a_sha256_digest(not_a_digest: str) -> None:
    """The same guard `ns()`'s `uuid.UUID` check is, met from the other side. A blob name is
    arbitrary text from another system: it carries `/` by construction, may carry `:`, and is
    unbounded — so a raw name in a key could forge a different family or a different
    environment."""
    with pytest.raises(ValueError, match="sha256"):
        lake_file_key(not_a_digest)


# --- The environment segment is the whole point -------------------------------------------


def test_keys_never_collide_across_environments_for_the_same_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One user, three environments, no key in common — `keys.py` records why it is scoped."""
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


def test_registry_fields_are_the_frozen_set() -> None:
    """A CHOKE POINT, and it earns its keep: adding `stay_writer` turns it red on the full run,
    which is the only reason the contract's field table and this list do not drift apart.
    Update the contract and this literal in the same change, never one of them."""
    assert REGISTRY_FIELDS == frozenset(
        {
            "app_name",
            "fqdn",
            "token_ref",
            "created_at",
            "state",
            # THE ONLY FIELD ON THIS HASH THAT MEANS THE APP ANSWERED A REQUEST. `state` is a
            # reaper-lifecycle label — its two values say whether the container is being torn
            # down — and the platform reporting `state=ready` as "your app is running" is the
            # 2026-09-10 defect this field exists to end. Three readings, and `keys.py` carries
            # the contract: absent = pre-cutover (proven), "" = never served, ISO-8601 = the
            # instant of first serve.
            "serving_since",
            "preview_stay_until",
            "stay_writer",
            # Temporary: `keys.py` retires this one with the rest of the legacy arm.
            "adopted_from_legacy",
            # Present ONLY when this slot holds a shared-runtime view, never on an ordinary
            # build sandbox's record.
            "shared_project_id",
            "shared_served_count",
        }
    )


def test_registry_states_are_the_two_frozen_values() -> None:
    assert REGISTRY_STATE_READY == "ready"
    assert REGISTRY_STATE_ENDING == "ending"
