"""What a JUST-CREATED container's registry hash says about serving — which is: nothing yet.

`_write_registry` is the record's birth certificate, and the one field this file exists for is
`serving_since`. It is seeded with the EMPTY SENTINEL, meaning "this container was scheduled and
has never answered a request", and every citizen-visible consequence of the 2026-09-10 defect
follows from getting that one write wrong:

  * write nothing at all, and the field is ABSENT — which the rollout grandfathers as
    PRE-CUTOVER/PROVEN, so every new container is reported as running the instant ACA schedules
    it. That is the shipped bug, re-shipped green and silent.
  * write it into the `hdel` on the next line instead of into the mapping — the design's own
    first instructions said to do both — and the delete runs SECOND and removes what the write
    just put there. Same absence, same bug, and nothing anywhere goes red.

So the assertions below are about the FIELD'S PRESENCE as much as its value, and the hash is
pre-seeded with a standing proof so that "empty" proves the write disowned an inherited value
rather than that there was never anything there.
"""

from __future__ import annotations

import uuid

import redis.asyncio as aioredis
from pydantic import SecretStr

from src.services.redis import REGISTRY_STATE_READY, registry_key
from src.services.redis.keys import (
    REGISTRY_FIELD_APP_NAME,
    REGISTRY_FIELD_CREATED_AT,
    REGISTRY_FIELD_PREVIEW_STAY_UNTIL,
    REGISTRY_FIELD_SERVING_SINCE,
    REGISTRY_FIELD_STATE,
    REGISTRY_FIELD_WAITING_SINCE,
)
from src.services.sandbox.client import AcaSandboxClient
from src.services.sandbox.config import SandboxConfig
from tests.fakes import a_sandbox_name

_PREDECESSOR = a_sandbox_name("gone")
_SUCCESSOR = a_sandbox_name("fresh")


def _a_client() -> AcaSandboxClient:
    """A real client whose ARM handle is never reached: `_write_registry` talks to Redis only."""

    class _NoArm:
        pass

    return AcaSandboxClient(
        SandboxConfig(
            subscription_id="sub",
            resource_group="rg",
            region="westeurope",
            managed_environment_name="aca-env",
            acr_server="acr.azurecr.io",
            acr_username="acr-user",
            acr_password=SecretStr("acr-pass"),
            image_ref="acr.azurecr.io/sandbox:latest",
        ),
        aca=_NoArm(),  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]  # pyright: ignore[reportArgumentType]
    )


async def _the_previous_occupant(redis: aioredis.Redis, user: uuid.UUID) -> None:
    """A hash left behind by the container that held this user's one slot before — carrying both
    inheritable facts: a standing serving proof and a standing stay of execution."""
    await redis.hset(
        registry_key(user),
        mapping={
            REGISTRY_FIELD_APP_NAME: _PREDECESSOR,
            REGISTRY_FIELD_STATE: REGISTRY_STATE_READY,
            REGISTRY_FIELD_SERVING_SINCE: "2026-09-10T09:41:04+00:00",
            REGISTRY_FIELD_PREVIEW_STAY_UNTIL: "2026-09-10T10:41:04+00:00",
        },
    )


async def test_a_new_container_says_it_has_never_served(fake_redis: aioredis.Redis) -> None:
    """★ THE ONE THAT CATCHES THE SILENT NO-OP. The field must be PRESENT and EMPTY.

    Both halves are load-bearing and neither implies the other: `!= "<the old instant>"` would
    pass on a field that had been deleted, and `hexists` alone would pass on a field carrying
    the predecessor's proof.

    Mutation-check: move `REGISTRY_FIELD_SERVING_SINCE: ""` out of the `hset` mapping and into
    the `hdel` beside `preview_stay_until`, exactly as the design first said to, and the
    presence assertion goes red — while a suite that asserted only "not the old instant" would
    stay green and ship the bug."""
    user = uuid.uuid4()
    await _the_previous_occupant(fake_redis, user)

    await _a_client()._write_registry(
        user, app_name=_SUCCESSOR, fqdn=f"{_SUCCESSOR}.example", token_ref="ref-fresh"
    )

    assert await fake_redis.hexists(registry_key(user), REGISTRY_FIELD_SERVING_SINCE) == 1, (
        "the serving proof is ABSENT on a brand-new container — and absence is the pre-cutover "
        "reading, which is grandfathered as PROVEN. Every new container would be reported as "
        "running the moment it was scheduled: the exact defect this field was added to end."
    )
    assert await fake_redis.hget(registry_key(user), REGISTRY_FIELD_SERVING_SINCE) == ""


async def test_a_new_container_inherits_neither_the_last_occupants_proof_nor_its_stay(
    fake_redis: aioredis.Redis,
) -> None:
    """The registry key is per USER and survives a container swap, so every field on it is
    INHERITED by the replacement unless the write actively disowns it. Two fields carry that
    hazard and they are disowned by different mechanisms — the stay by the `hdel`, the proof by
    the mapping being a MERGE that overwrites — so both are asserted here rather than one
    standing in for the other."""
    user = uuid.uuid4()
    await _the_previous_occupant(fake_redis, user)

    await _a_client()._write_registry(
        user, app_name=_SUCCESSOR, fqdn=f"{_SUCCESSOR}.example", token_ref="ref-fresh"
    )

    reg = await fake_redis.hgetall(registry_key(user))
    assert reg[REGISTRY_FIELD_APP_NAME] == _SUCCESSOR
    assert reg[REGISTRY_FIELD_SERVING_SINCE] == "", (
        "the new container inherited the last occupant's serving proof — the pane would frame "
        "it as running on evidence about a container that no longer exists"
    )
    assert REGISTRY_FIELD_PREVIEW_STAY_UNTIL not in reg


async def test_the_record_is_born_ready_and_unproven_at_the_same_instant(
    fake_redis: aioredis.Redis,
) -> None:
    """The two fields say DIFFERENT things and this is the moment they diverge: `state=ready` is
    a reaper-lifecycle label meaning "not being torn down", and it was `ready` — from this exact
    write — throughout the whole measured window in which the app served nobody. `serving_since`
    is the one field that means the app answered something.

    A future editor deriving one from the other collapses the distinction this change exists to
    draw, so it is pinned in the same breath."""
    user = uuid.uuid4()

    await _a_client()._write_registry(
        user, app_name=_SUCCESSOR, fqdn=f"{_SUCCESSOR}.example", token_ref="ref-fresh"
    )

    reg = await fake_redis.hgetall(registry_key(user))
    assert (reg[REGISTRY_FIELD_STATE], reg[REGISTRY_FIELD_SERVING_SINCE]) == (
        REGISTRY_STATE_READY,
        "",
    )
    assert reg[REGISTRY_FIELD_CREATED_AT], "no birthday, so `ms_since_container_created` is blind"


async def test_a_new_container_waits_from_its_own_birth_not_the_last_occupants_wait(
    fake_redis: aioredis.Redis,
) -> None:
    """The pane dates a wait from this field, so a predecessor's value surviving the MERGE would
    tell the citizen their brand-new container has been starting since the last one's restart.

    Mutation-check: drop `REGISTRY_FIELD_WAITING_SINCE` from the `hset` mapping and the
    predecessor's instant survives."""
    user = uuid.uuid4()
    await _the_previous_occupant(fake_redis, user)
    await fake_redis.hset(
        registry_key(user), REGISTRY_FIELD_WAITING_SINCE, "2026-09-10T09:50:00+00:00"
    )

    await _a_client()._write_registry(
        user, app_name=_SUCCESSOR, fqdn=f"{_SUCCESSOR}.example", token_ref="ref-fresh"
    )

    reg = await fake_redis.hgetall(registry_key(user))
    assert reg[REGISTRY_FIELD_WAITING_SINCE] == reg[REGISTRY_FIELD_CREATED_AT]
