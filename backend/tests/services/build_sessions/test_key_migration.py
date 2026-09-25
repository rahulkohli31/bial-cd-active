"""The dual-read window: a fleet registered before the environment segment existed must stay
visible across the cutover deploy.

What a moved registry prefix costs is set out in `services/redis/keys.py`. What this file covers
is the half-measure that looks like the fix: widening only the SCAN pattern changes nothing,
because `sweep_all` extracts the `user_id` from the key name and then issues a FRESH point read.
Every assertion below that says "the sweep still sees it" is load-bearing against exactly that
change, which would read as correct in review.

The mutation-check for this file is: delete the legacy arm from `locks.read_registry` and watch
the fleet disappear from both `sweep_all` and `take_sandbox_inventory`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
import redis.asyncio as aioredis
from pydantic import SecretStr

from src.config import settings
from src.services.build_sessions import locks, reaper
from src.services.build_sessions.inventory import take_sandbox_inventory
from src.services.redis.keys import (
    REGISTRY_FIELD_ADOPTED_FROM_LEGACY,
    REGISTRY_FIELD_APP_NAME,
    REGISTRY_FIELD_CREATED_AT,
    REGISTRY_FIELD_FQDN,
    REGISTRY_FIELD_PREVIEW_STAY_UNTIL,
    REGISTRY_FIELD_SERVING_SINCE,
    REGISTRY_FIELD_STATE,
    REGISTRY_FIELD_TOKEN_REF,
    REGISTRY_STATE_ENDING,
    REGISTRY_STATE_READY,
    heartbeat_key,
    lease_key,
    legacy_registry_key,
    lock_key,
    registry_key,
)
from src.services.sandbox import SandboxGoneError
from src.services.sandbox.base import FleetMember
from src.services.sandbox.client import AcaSandboxClient
from src.services.sandbox.config import SandboxConfig
from tests.fakes import FakeSandboxClient, a_fleet_member, a_sandbox_name

_LEGACY_APP = "sbx-019f74300c9f747db10b73b6dcdd"  # a real name from the pre-cutover fleet


class _Fleet:
    """A control plane that lists whatever it is told to."""

    def __init__(self, names: list[str]) -> None:
        self.names = names

    async def list_sandbox_fleet(self) -> list[FleetMember]:
        return [a_fleet_member(n) for n in self.names]


_BORN = "2026-07-22T04:11:00+00:00"


def _record(app_name: str) -> dict[str, str]:
    """A COMPLETE PRE-CUTOVER registry hash — every field the fleet carried before the prefix
    moved, so "without losing a field" is a real assertion rather than a spot check on the two
    the reaper happens to read.

    NO `serving_since`, and that absence is the historical truth this fixture stands for: the
    field did not exist when these records were written. What the adoption DOES about that is
    `_adopted` below."""
    return {
        REGISTRY_FIELD_APP_NAME: app_name,
        REGISTRY_FIELD_FQDN: f"{app_name}.westeurope.azurecontainerapps.io",
        REGISTRY_FIELD_TOKEN_REF: "ref-from-before-the-cutover",
        REGISTRY_FIELD_CREATED_AT: _BORN,
        REGISTRY_FIELD_STATE: REGISTRY_STATE_READY,
    }


def _adopted(app_name: str) -> dict[str, str]:
    """The same record as an adoption hands it BACK: every legacy field, plus a serving proof
    stamped at the record's own `created_at`.

    ONE FIELD IS ADDED RATHER THAN COPIED, and it has to be. A verbatim copy would carry no
    `serving_since`, and an adoption can land at ANY time — a pre-cutover container attached
    months from now still arrives here — so "absent can only mean pre-cutover" would never
    become true and the grandfather arm that reads absence as PROVEN could never be retired.
    Writing a real instant is exactly what makes absence impossible on any record written after
    the cutover.

    PROVEN, not the `""` sentinel: this container was scheduled before the stamp existed and is
    very likely serving a citizen right now. Seeding it unproven would unframe a live preview on
    the spot — the single thing the grandfather arm exists to prevent."""
    return _record(app_name) | {REGISTRY_FIELD_SERVING_SINCE: _BORN}


async def _write(redis: aioredis.Redis, key: str, record: dict[str, str]) -> None:
    """Field by field rather than `mapping=`: redis-py types `mapping` as
    `Mapping[FieldT, EncodableT]`, whose key parameter is invariant, so a `dict[str, str]`
    variable fails every type gate while the identical inline literal passes."""
    for field, value in record.items():
        await redis.hset(key, field, value)


async def _seed_legacy(redis: aioredis.Redis, user: uuid.UUID, app_name: str) -> None:
    """Register a sandbox the way the fleet was registered BEFORE this unit — under
    `bial:sandbox:registry:{user_id}`, with no environment segment."""
    await _write(redis, legacy_registry_key(user), _record(app_name))


def _sandbox_config() -> SandboxConfig:
    return SandboxConfig(
        subscription_id="sub",
        resource_group="rg",
        region="westeurope",
        managed_environment_name="aca-env",
        acr_server="acr.azurecr.io",
        acr_username="acr-user",
        acr_password=SecretStr("acr-pass"),
        image_ref="acr.azurecr.io/sandbox:latest",
    )


def _client_with_no_arm() -> AcaSandboxClient:
    """A real `AcaSandboxClient` whose ARM handle is never reached on the paths below. The point
    reads are the subject; the ACA calls are not."""

    class _NoArm:
        pass

    return AcaSandboxClient(
        _sandbox_config(),
        aca=_NoArm(),  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]  # pyright: ignore[reportArgumentType]
    )


# --- THE headline: the live fleet does not vanish -------------------------------------------


async def test_a_fleet_registered_under_the_old_prefix_is_still_swept(
    fake_redis: aioredis.Redis,
) -> None:
    """AE-shaped: three containers registered before the cutover, no lock and no heartbeat (the
    deploy that changed the prefix also ended their sessions). All three must still be reaped.

    Break dual-read and this goes to `reaped == 0` — and every one of those containers bills
    forever with nothing in the platform able to name it."""
    users = [uuid.uuid4() for _ in range(3)]
    for i, user in enumerate(users):
        await _seed_legacy(fake_redis, user, a_sandbox_name(f"legacy{i}"))
    client = FakeSandboxClient()

    result = await reaper.sweep_all(fake_redis, client)

    assert result.reaped == 3
    assert result.failed == 0
    assert sorted(client.torn_down) == [
        a_sandbox_name("legacy0"),
        a_sandbox_name("legacy1"),
        a_sandbox_name("legacy2"),
    ]


async def test_a_fleet_registered_under_the_old_prefix_is_still_inventoried(
    fake_redis: aioredis.Redis,
) -> None:
    """The report-only Azure inventory is the platform's ONLY view of containers nothing tracks.
    If the cutover made pre-cutover records unreadable, every one of them would be reported as
    `unregistered` — an alarm listing the entire fleet, which is the same as no alarm at all."""
    user = uuid.uuid4()
    await _seed_legacy(fake_redis, user, _LEGACY_APP)

    inv = await take_sandbox_inventory(
        fake_redis, _Fleet([_LEGACY_APP, a_sandbox_name("genuinely-orphaned")])
    )

    assert inv.registered == (_LEGACY_APP,)
    assert inv.unregistered == (a_sandbox_name("genuinely-orphaned"),)  # and ONLY the real orphan


async def test_the_sweep_reaps_a_legacy_record_whose_container_teardown_fails_only_once(
    fake_redis: aioredis.Redis,
) -> None:
    """Termination, not visibility. A legacy key that the reaper reads but never clears would be
    re-read, re-torn-down and re-logged on every pass for the life of the deployment. After one
    successful reap, nothing under EITHER prefix survives for that user."""
    user = uuid.uuid4()
    await _seed_legacy(fake_redis, user, a_sandbox_name("legacy-solo"))

    assert (await reaper.sweep_all(fake_redis, FakeSandboxClient())).reaped == 1
    second = await reaper.sweep_all(fake_redis, FakeSandboxClient())

    assert second.reaped == 0
    assert await fake_redis.exists(legacy_registry_key(user)) == 0
    assert await fake_redis.exists(registry_key(user)) == 0


# --- migration on read ----------------------------------------------------------------------


async def test_the_read_migrates_a_legacy_hash_without_losing_a_field(
    fake_redis: aioredis.Redis,
) -> None:
    user = uuid.uuid4()
    await _seed_legacy(fake_redis, user, _LEGACY_APP)

    reg = await locks.read_registry(fake_redis, user)

    # The serving proof rides back to the CALLER too, not just into the hash: it is exactly a
    # field consumers branch on, and a caller handed a record whose stamp it cannot see would
    # have to re-read the key to learn what this write just put there.
    assert reg == _adopted(_LEGACY_APP)
    # Rewritten under the environment-scoped key, field for field, plus the adoption marker —
    # which is what later authorises `delete_registry` to clear the legacy key. The marker is
    # bookkeeping between the migration and the delete, not a field any consumer should start
    # branching on, so it is the one thing the returned record above does NOT carry.
    assert await fake_redis.hgetall(registry_key(user)) == {
        **_adopted(_LEGACY_APP),
        REGISTRY_FIELD_ADOPTED_FROM_LEGACY: "1",
    }
    # THE LEGACY KEY SURVIVES THE READ, DELIBERATELY: retiring it here would let a process
    # pointed at the WRONG Redis relocate another environment's legacy record under its own
    # prefix and delete the original, leaving the owning environment with a running container
    # and no record — the exact orphan class environment scoping exists to prevent, manufactured
    # by the mitigation meant to close it. `delete_registry` is where the legacy key actually
    # goes, once the session ends.
    assert await fake_redis.exists(legacy_registry_key(user)) == 1


async def test_a_field_added_after_the_cutover_migrates_too(fake_redis: aioredis.Redis) -> None:
    """The migration copies the hash it FINDS, not a hard-coded field list — so a field this unit
    has never heard of survives. `preview_stay_until` is the live example: it is optional, absent
    from `_record`, and written by a different subsystem."""
    user = uuid.uuid4()
    await _seed_legacy(fake_redis, user, _LEGACY_APP)
    await fake_redis.hset(
        legacy_registry_key(user), REGISTRY_FIELD_PREVIEW_STAY_UNTIL, "2026-07-22T04:41:00+00:00"
    )

    reg = await locks.read_registry(fake_redis, user)

    assert reg is not None
    assert reg[REGISTRY_FIELD_PREVIEW_STAY_UNTIL] == "2026-07-22T04:41:00+00:00"


async def test_a_current_record_wins_over_a_stale_legacy_one(fake_redis: aioredis.Redis) -> None:
    """Precedence is not arbitrary. The current key is where every WRITE goes, so it is by
    definition the newer claim; reading the legacy one over it would hand the caller a superseded
    `app_name` and point a teardown at the wrong container."""
    user = uuid.uuid4()
    await _seed_legacy(fake_redis, user, a_sandbox_name("the-one-that-is-gone"))
    await _write(fake_redis, registry_key(user), _record(a_sandbox_name("the-live-one")))

    reg = await locks.read_registry(fake_redis, user)

    assert reg is not None
    assert reg[REGISTRY_FIELD_APP_NAME] == a_sandbox_name("the-live-one")


async def test_delete_registry_clears_both_prefixes_for_a_record_we_adopted(
    fake_redis: aioredis.Redis,
) -> None:
    """The interrupted-migration case: a rewrite that landed and a delete that did not. Without
    this, the surviving legacy key makes the sweep loop on a container that is already gone.

    Driven through the real adoption path rather than by hand-writing the current key, because
    adoption is what marks the record as ours — and that mark is now what authorises the legacy
    delete (see the sibling test)."""
    user = uuid.uuid4()
    await _seed_legacy(fake_redis, user, _LEGACY_APP)
    assert await locks.read_registry(fake_redis, user) is not None  # adopts it

    await locks.delete_registry(fake_redis, user)

    assert await fake_redis.exists(registry_key(user)) == 0
    assert await fake_redis.exists(legacy_registry_key(user)) == 0


async def test_ending_our_session_does_not_delete_another_environments_legacy_record(
    fake_redis: aioredis.Redis,
) -> None:
    """THE ONE NAMESPACE WITH NO ENVIRONMENT SEGMENT. `bial:sandbox:registry:{user}` names
    different containers per deployment on one shared Redis, so deleting it unconditionally
    let a session-end also destroy another environment's record — the orphan class
    environment scoping exists to prevent, manufactured by its own cleanup. The record here
    was born post-cutover, never adopted, so the legacy key beside it is somebody else's.

    Mutation-check: delete the legacy key unconditionally and this goes red while its
    sibling above stays green."""
    user = uuid.uuid4()
    await _write(fake_redis, legacy_registry_key(user), _record(a_sandbox_name("theirs")))
    await _write(fake_redis, registry_key(user), _record(a_sandbox_name("ours")))

    await locks.delete_registry(fake_redis, user)

    assert await fake_redis.exists(registry_key(user)) == 0
    assert await fake_redis.exists(legacy_registry_key(user)) == 1, (
        "a record this environment never adopted is not ours to delete"
    )


# --- the two point reads must not drift apart -----------------------------------------------


async def test_both_point_reads_agree_on_a_legacy_record(fake_redis: aioredis.Redis) -> None:
    """`locks.read_registry` and `SandboxClient._read_registry` are separate implementations by
    design (`services/sandbox/` must not import `services/build_sessions/`). This is the only
    thing stopping them from drifting: they are handed the same legacy hash and must answer
    identically, migration and all."""
    user_a, user_b = uuid.uuid4(), uuid.uuid4()
    await _seed_legacy(fake_redis, user_a, _LEGACY_APP)
    await _seed_legacy(fake_redis, user_b, _LEGACY_APP)

    from_locks = await locks.read_registry(fake_redis, user_a)
    from_client = await _client_with_no_arm()._read_registry(user_b)

    # INCLUDING THE SERVING PROOF, which is the newest way these two can drift and the most
    # expensive: one mirror stamping and the other not would leave half the adopted fleet
    # reading as PRE-CUTOVER forever, so the grandfather arm could never be retired — and which
    # mirror ran would depend on nothing more meaningful than which caller attached first.
    assert from_locks == from_client == _adopted(_LEGACY_APP)
    assert await fake_redis.hgetall(registry_key(user_b)) == _adopted(_LEGACY_APP)
    # The legacy key SURVIVES the read — see the sibling test below for why that is the safe
    # behaviour, and `delete_registry` for where it is actually removed.
    assert await fake_redis.exists(legacy_registry_key(user_b)) == 1


# --- the serving proof an adopted record cannot have carried ---------------------------------
#
# "`_write_registry` is the only writer, so an absent `serving_since` can only mean pre-cutover"
# is the sentence the whole rollout rests on — and it is FALSE unless adoption stamps too. This
# path copies a legacy hash under the current prefix at any time, months after the deploy, and a
# verbatim copy would keep manufacturing absent-stamped records forever: the grandfather arm
# could never be retired, and the comment shipped beside it saying it is deletable would be
# misleading whoever eventually acted on it.


async def test_adoption_stamps_the_legacy_records_own_birthday_as_its_serving_proof(
    fake_redis: aioredis.Redis,
) -> None:
    """PROVEN, and dated to the record itself rather than to the adoption. The instant matters:
    `ms_since_container_created` is read off the gap between `created_at` and this field, so
    stamping NOW on a container born in July would report a first serve weeks after its birth.

    Mutation-check: copy the legacy hash verbatim and this goes red on the first assertion —
    which is the shape the design originally shipped."""
    user = uuid.uuid4()
    await _seed_legacy(fake_redis, user, _LEGACY_APP)

    reg = await locks.read_registry(fake_redis, user)

    assert reg is not None
    assert reg[REGISTRY_FIELD_SERVING_SINCE] == _BORN
    assert reg[REGISTRY_FIELD_SERVING_SINCE] != "", (
        "the empty sentinel would read as UNPROVEN and unframe a preview that is very likely "
        "serving a citizen at this exact moment — the one thing the grandfather arm exists to "
        "prevent"
    )


async def test_a_truncated_legacy_hash_with_no_birthday_is_still_stamped_proven(
    fake_redis: aioredis.Redis,
) -> None:
    """No `created_at` to inherit. The fallback is the adoption instant — the earliest moment
    this platform can honestly claim to have known the container was there — and NOT the empty
    sentinel, because "we cannot date it" is not evidence that it never served.

    A truncated record still has to leave the field PRESENT, or this one record goes on reading
    as pre-cutover after every other one has been closed."""
    user = uuid.uuid4()
    await fake_redis.hset(legacy_registry_key(user), REGISTRY_FIELD_APP_NAME, _LEGACY_APP)

    reg = await locks.read_registry(fake_redis, user)

    assert reg is not None
    stamped = reg[REGISTRY_FIELD_SERVING_SINCE]
    assert stamped != ""
    # Parses, and is not in some far-off year: a value the reader cannot parse resolves to
    # "cannot say", which would make the stamp decorative.
    assert abs((datetime.fromisoformat(stamped) - datetime.now(UTC)).total_seconds()) < 60


async def test_both_mirrors_stamp_an_adopted_record_identically(
    fake_redis: aioredis.Redis,
) -> None:
    """★ THE DRIFT THAT WOULD BE INVISIBLE. `locks._adopt_a_pre_cutover_record` and
    `SandboxClient._adopt_a_pre_cutover_record` are separate implementations by design
    (`services/sandbox/` must not import `services/build_sessions/`), and this is the only thing
    stopping them from disagreeing about the newest field on the hash.

    Handed the same legacy record they must write the same proof — asserted as an EXACT value,
    not merely "both non-empty", because `""` and an instant are both non-absent and mean
    opposite things."""
    user_a, user_b = uuid.uuid4(), uuid.uuid4()
    await _seed_legacy(fake_redis, user_a, _LEGACY_APP)
    await _seed_legacy(fake_redis, user_b, _LEGACY_APP)

    await locks.read_registry(fake_redis, user_a)
    await _client_with_no_arm()._read_registry(user_b)

    from_locks = await fake_redis.hget(registry_key(user_a), REGISTRY_FIELD_SERVING_SINCE)
    from_client = await fake_redis.hget(registry_key(user_b), REGISTRY_FIELD_SERVING_SINCE)

    assert from_locks == from_client == _BORN


async def test_attach_reaches_a_legacy_record_instead_of_calling_it_gone(
    fake_redis: aioredis.Redis,
) -> None:
    """Attach across the cutover. `SandboxGoneError("no live sandbox registered for user")` is the
    dangerous answer — the caller responds by RESTORING, which tears the live container down and
    rolls the builder back to their last save. Proving attach got past that refusal is enough
    here: this record is marked `ending`, so it stops at the SECOND refusal, which is the one
    that only exists if the registry was actually read."""
    user = uuid.uuid4()
    await _seed_legacy(fake_redis, user, _LEGACY_APP)
    await fake_redis.hset(legacy_registry_key(user), REGISTRY_FIELD_STATE, REGISTRY_STATE_ENDING)

    with pytest.raises(SandboxGoneError, match="sandbox is ending"):
        await _client_with_no_arm().attach_existing(str(user))


async def test_reconcile_on_start_reaps_a_legacy_record(fake_redis: aioredis.Redis) -> None:
    """The other half of "a builder comes back after the cutover": reconcile-on-start must clear
    their pre-cutover sandbox so the incoming build can take the one-per-user slot. Blind to the
    legacy record, it would leave the container standing and register a second one over it."""
    user = uuid.uuid4()
    await _seed_legacy(fake_redis, user, _LEGACY_APP)
    client = FakeSandboxClient()

    reaped = await reaper.reconcile_user(fake_redis, user, client, certified_dead=True)

    assert reaped is True
    assert client.torn_down == [_LEGACY_APP]


# --- Another environment's keys are not ours -------------------------------------------------


async def test_a_sweep_ignores_another_environments_keys_entirely(
    fake_redis: aioredis.Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """From the sweep's end: pointed at a Redis holding production's records, a
    development process must find NOTHING — not "records it does not understand", nothing at all.

    Note what the correct answer looks like downstream: an empty spare-list against a live fleet.
    That is deliberately the input the store-fault guard trips on, so the wrong-Redis case
    escalates to a human instead of reading as a fleet of orphans to destroy."""
    user = uuid.uuid4()
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    await _write(fake_redis, registry_key(user), _record(a_sandbox_name("production-container")))
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")

    client = FakeSandboxClient()
    result = await reaper.sweep_all(fake_redis, client)
    inv = await take_sandbox_inventory(
        fake_redis, _Fleet([a_sandbox_name("production-container")])
    )

    assert result == reaper.SweepResult(reaped=0, failed=0)
    assert client.torn_down == []
    assert inv.registered == ()  # the empty spare-list this guard fails closed on
    assert inv.unregistered == (a_sandbox_name("production-container"),)


async def test_the_scan_never_reaches_across_environments_through_the_legacy_pattern(
    fake_redis: aioredis.Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The legacy pattern is a literal, not a widened glob. `bial:*:sandbox:registry:*` would have
    been the tempting one-liner, and it would have swept production from development."""
    theirs, ours = uuid.uuid4(), uuid.uuid4()
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    await _write(fake_redis, registry_key(theirs), _record(a_sandbox_name("theirs")))
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    await _write(fake_redis, registry_key(ours), _record(a_sandbox_name("ours")))

    client = FakeSandboxClient()
    await reaper.sweep_all(fake_redis, client)

    assert client.torn_down == [a_sandbox_name("ours")]


# --- the lock deliberately does NOT dual-read ------------------------------------------------


async def test_a_legacy_lock_is_not_honoured(fake_redis: aioredis.Redis) -> None:
    """Deliberate, and the opposite of the registry decision. After the cutover every
    legacy-prefix lock is holder-less, so honouring one would hand a returning builder up to
    `LOCK_TTL_SECONDS` of phantom 409 on a session that does not exist — the lockout `reap_lock`
    was written to prevent. The registry is dual-read because losing it leaks a container; the
    lock is not, because keeping it locks a person out."""
    user = uuid.uuid4()
    await fake_redis.set(f"bial:sandbox:lock:{user}", "a-token-from-before-the-cutover", ex=900)

    assert await locks.lock_is_held(fake_redis, user) is False
    assert await locks.acquire_lock(fake_redis, user) is not None


def test_no_module_builds_a_sandbox_key_by_hand() -> None:
    """The choke point is only a choke point while it has no bypass, and "everyone uses the
    builders" is exactly the kind of claim that is true right up until it quietly is not. So this
    greps the source rather than trusting review.

    Two files may write the root: `keys.py`, which owns families 1-4, and `broker.py`, which owns
    family 5. Anywhere else, a literal `bial:` with a key segment after it is a hand-typed
    key that the environment scoping cannot reach — precisely the drift that scoping forbids.
    """
    import ast
    import pathlib
    import re

    # `bial:` NOT followed by whitespace — so the golden template's `git commit -m 'bial: …'`
    # message does not read as a key.
    a_key_shaped_literal = re.compile(r"bial:\S")
    owns_the_root = {("redis", "keys.py"), ("src", "broker.py")}

    src = pathlib.Path(__file__).resolve().parents[3] / "src"
    offenders: list[str] = []
    for path in src.rglob("*.py"):
        if (path.parent.name, path.name) in owns_the_root:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        # A docstring quoting a key format is DOCUMENTATION and must stay welcome — half the
        # modules here explain the namespace they participate in. Only a string that is actually
        # used as a value can be a hand-built key, so bare expression statements are excluded.
        prose = {
            id(node.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
        }

        # F-STRINGS MUST BE REASSEMBLED BEFORE MATCHING, or this test is blind to the only key
        # shape anyone would write from now on. `f"bial:{env}:sandbox:lock:{u}"` parses to a
        # JoinedStr whose first Constant is exactly `"bial:"` — nothing follows the colon in that
        # fragment, so `bial:\S` does not match it and the probe returns zero offenders.
        # Interpolations collapse to a non-space sentinel so the reassembled text reads like the
        # key it will become at runtime.
        interpolated: set[int] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.JoinedStr):
                continue
            reassembled = ""
            for part in node.values:
                if isinstance(part, ast.Constant) and isinstance(part.value, str):
                    interpolated.add(id(part))
                    reassembled += part.value
                else:
                    reassembled += "\x00"  # an interpolation: non-space, so `\S` matches it
            if a_key_shaped_literal.search(reassembled):
                offenders.append(f"{path.relative_to(src)}:{node.lineno} (f-string)")

        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if id(node) in prose or id(node) in interpolated:
                    continue
                if not a_key_shaped_literal.search(node.value):
                    continue
                offenders.append(f"{path.relative_to(src)}:{node.lineno}")
    assert offenders == [], (
        f"a sandbox Redis key is being built by hand, outside the keys.py choke point: "
        f"{offenders}. Environment scoping cannot reach it, which is exactly the drift R22 "
        f"forbids."
    )


async def test_a_read_does_not_strand_another_environments_container(
    fake_redis: aioredis.Redis,
) -> None:
    """The environment-scoping mitigation must not manufacture the failure it exists to
    prevent: during the dual-read window BOTH environments still read the legacy prefix, so
    a process pointed at the wrong Redis can reach another environment's record — accepted,
    unchanged exposure. What is NOT acceptable is the read RELOCATING it, which would leave
    the owning environment (which scans only its own prefix and the legacy one) with a
    running container and no record — an anonymous, forever-billing ghost.

    Asserted from the owning environment's side, the side that gets hurt."""
    user = uuid.uuid4()
    await _seed_legacy(fake_redis, user, _LEGACY_APP)

    # A process in ANOTHER environment reads it (here: this process, standing in for it — the
    # point is only that a read happened under a different prefix).
    migrated = await locks.read_registry(fake_redis, user)
    assert migrated == _adopted(_LEGACY_APP)

    # The legacy record is still there, so the owning environment's sweep still finds it.
    assert await fake_redis.exists(legacy_registry_key(user)) == 1
    assert await fake_redis.hgetall(legacy_registry_key(user)) == _record(_LEGACY_APP)


async def test_a_second_read_does_not_migrate_again(fake_redis: aioredis.Redis) -> None:
    """Termination. Leaving the legacy key behind must not make migration-on-read a treadmill:
    once the current key exists, `read_registry` finds it first and the legacy arm is never
    reached. Proven by mutating the legacy record between reads — the second read must return
    the CURRENT value, not re-adopt the legacy one."""
    user = uuid.uuid4()
    await _seed_legacy(fake_redis, user, _LEGACY_APP)

    first = await locks.read_registry(fake_redis, user)
    assert first is not None and first[REGISTRY_FIELD_APP_NAME] == _LEGACY_APP

    await fake_redis.hset(
        legacy_registry_key(user), REGISTRY_FIELD_APP_NAME, a_sandbox_name("should-lose")
    )
    second = await locks.read_registry(fake_redis, user)

    assert second is not None
    assert second[REGISTRY_FIELD_APP_NAME] == _LEGACY_APP, (
        "the second read re-adopted the legacy hash — migration-on-read is not terminating"
    )


async def test_the_namespace_smoke_check_is_exactly_two_globs(
    fake_redis: aioredis.Redis,
) -> None:
    """The key namespace publishes this as a contract: a smoke check is `SCAN MATCH bial:*` plus
    `SCAN MATCH autoclaim:*`, and nothing else. If a later unit adds a key family under a third
    root, the operator runbook silently stops covering it.

    The `autoclaim:` root is not ours to move — `RedisStreamBroker` derives it — which is exactly
    why it needs naming rather than assuming everything lives under `bial:`.
    """
    user = uuid.uuid4()
    await _seed_legacy(fake_redis, user, _LEGACY_APP)
    await _write(fake_redis, registry_key(user), _record(_LEGACY_APP))
    await fake_redis.set(lock_key(user), "tok")
    await fake_redis.set(heartbeat_key(user), "2026-08-11T00:00:00+00:00")
    await fake_redis.set(lease_key(user), "2026-08-11T00:05:00+00:00")

    seen = {str(k) async for k in fake_redis.scan_iter(match="*")}
    covered = {str(k) async for k in fake_redis.scan_iter(match="bial:*")} | {
        str(k) async for k in fake_redis.scan_iter(match="autoclaim:*")
    }

    assert seen - covered == set(), (
        f"a key family escapes the two-glob smoke check C5 documents: {sorted(seen - covered)}. "
        f"Either move it under `bial:`, or amend C5 — the runbook is only as good as its globs."
    )
