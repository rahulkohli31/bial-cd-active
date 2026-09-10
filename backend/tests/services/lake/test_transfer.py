"""The Redis copy: does it survive the codec, does it stay inside its budget, and can it reach
anything it must not.

THE FIRST TEST IN THIS FILE IS THE ONE THE UNIT WAS WRITTEN AROUND. The platform's Redis client is
built with `decode_responses=True`, which is right for every other family here — locks, heartbeats,
the registry hash, the lease, the start marker — and catastrophic for parquet: a reply is decoded
as UTF-8 before any caller sees it, so a file either raises or comes back as a `str` that
re-encodes to different bytes. That is silent corruption of the one thing this feature copies
verbatim, and it would survive every other assertion in this file.

THE SECOND THING THIS FILE IS FOR IS THE BLAST RADIUS. The trim deletes keys, in a namespace where
a scheduled job reads the registry hash and destroys Azure containers on the strength of it. So
every other family is seeded and asserted to survive a trim at and over budget — all six of them,
because four of six is not a blast-radius test: a bug that reached the start marker or the taskiq
stream would pass it while taking down a live build.

Two clients, one server, exactly as production has it: `fakeredis` with a shared `FakeServer`, a
decoded client for the other families and a binary one for this feature's own keys.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import date

import fakeredis.aioredis
import pytest
import redis.asyncio as aioredis
from redis.exceptions import ConnectionError as RedisConnectionError

from src.core.connectors import ResolvedWindow
from src.db.models.project_connector import ConnectorWindowKind
from src.services.lake.client import LakeClient
from src.services.lake.errors import LakeError
from src.services.lake.transfer import (
    LAKE_COPY_BUDGET_BYTES,
    LAKE_COPY_TTL_SECONDS,
    _digest,
    _evict_oldest,
    transfer_window,
    transfer_window_or_log,
)
from src.services.lake.window import SelectedFile, WindowSelection
from src.services.redis.keys import (
    heartbeat_key,
    lake_file_key,
    lake_index_key,
    lease_key,
    lock_key,
    registry_key,
    starting_key,
)

_ROOT = "AOS/tb_flight_fact_report/"
_USER = uuid.UUID("018f3f9c-0000-7000-8000-000000000001")


def _selected(day: int, size: int) -> SelectedFile:
    name = f"{_ROOT}2026/SEPTEMBER/tb_flight_fact_report_202609{day:02d}.parquet"
    return SelectedFile(name=name, size=size, day=date(2026, 9, day))


def _selection(*files: SelectedFile, skipped: int = 0) -> WindowSelection:
    ordered = sorted(files, key=lambda item: item.day, reverse=True)
    return WindowSelection(
        files=tuple(ordered),
        skipped=skipped,
        total_bytes=sum(item.size for item in ordered),
    )


def _window(start: date, end: date) -> ResolvedWindow:
    return ResolvedWindow(
        effectively_on=True,
        kind=ConnectorWindowKind.RELATIVE,
        start=start,
        end=end,
        days=(end - start).days + 1,
        clamped=False,
        earliest=start,
        latest=end,
    )


class _FakeLake:
    """A lake that hands back deterministic bytes for a name, and can be told to fail."""

    def __init__(self, *, raises: Exception | None = None) -> None:
        self.raises = raises
        self.downloaded: list[str] = []
        self.payloads: dict[str, bytes] = {}
        # Names that fail while the rest of the lake answers normally — a blob deleted or
        # re-ACL'd between the listing and the download, which is a different condition from
        # `raises` (the whole lake refusing) and has to be scriptable separately.
        self.unreadable: set[str] = set()

    async def download(self, name: str) -> bytes:
        if self.raises is not None:
            raise self.raises
        if name in self.unreadable:
            raise LakeError(f"the lake would not serve {name}")
        self.downloaded.append(name)
        return self.payloads.get(name, name.encode() * 4)


def _lake(fake: _FakeLake) -> LakeClient:
    """The fake, typed as the client. `transfer_window` only ever calls `download`."""
    from typing import cast

    return cast("LakeClient", fake)


@pytest.fixture
async def redis_pair() -> AsyncIterator[tuple[aioredis.Redis, aioredis.Redis]]:
    """`(text, binary)` over ONE fake server — the production shape, where two pools address the
    same instance and the same database. Sharing the server is what makes the blast-radius test
    meaningful: keys the decoded client writes are genuinely reachable by the binary one."""
    server = fakeredis.FakeServer()
    text = fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)
    binary = fakeredis.aioredis.FakeRedis(server=server, decode_responses=False)
    yield text, binary
    await text.flushall()
    await text.aclose()
    await binary.aclose()


# --- the codec ----------------------------------------------------------------------------------


async def test_bytes_written_are_bytes_read(redis_pair) -> None:
    """★ THE ASSERTION THE WHOLE UNIT HANGS ON.

    A payload spanning the full 8-bit range: a null byte, a real parquet magic number, a UTF-8
    continuation byte with no lead, and every value from 0 to 255. Written through the binary
    client and read back through it, unchanged. Do this on the decoded client and it raises
    `UnicodeDecodeError` — which is the GOOD outcome; the bad one is a payload that happens to
    decode and re-encodes to something else."""
    _text, binary = redis_pair
    payload = b"PAR1" + bytes(range(256)) + b"\x00\xff\xfe\x80PAR1"
    lake = _FakeLake()
    file = _selected(1, len(payload))
    lake.payloads[file.name] = payload

    await transfer_window(_lake(lake), binary, _selection(file))

    assert await binary.get(lake_file_key(_digest(file.name))) == payload


async def test_the_decoded_client_would_have_mangled_it(redis_pair) -> None:
    """The negative half, so the test above is not merely asserting that a fake round-trips.

    This is what the ordinary platform client does with the same bytes — and it is the reason
    `get_redis_bytes()` exists as a second pool rather than a flag on a call."""
    text, binary = redis_pair
    payload = b"PAR1" + bytes(range(256))
    await binary.set("bial:test:codec-probe", payload)

    with pytest.raises(UnicodeDecodeError):
        await text.get("bial:test:codec-probe")


# --- the happy path -----------------------------------------------------------------------------


async def test_a_window_writes_one_key_and_one_index_entry_per_file(redis_pair) -> None:
    _text, binary = redis_pair
    files = [_selected(day, 4_000) for day in (1, 2, 3)]

    report = await transfer_window(_lake(_FakeLake()), binary, _selection(*files))

    assert report.copied == 3
    assert report.already_held is False
    for file in files:
        assert await binary.exists(lake_file_key(_digest(file.name)))
    assert await binary.zcard(lake_index_key()) == 3


async def test_every_key_carries_the_seven_day_ttl(redis_pair) -> None:
    """★ Age is one of the only two things that removes a copy, and Redis owns it. A key without
    a TTL is one this code can never clean up, in a store nothing here monitors."""
    _text, binary = redis_pair
    file = _selected(1, 4_000)

    await transfer_window(_lake(_FakeLake()), binary, _selection(file))

    assert await binary.ttl(lake_file_key(_digest(file.name))) == LAKE_COPY_TTL_SECONDS
    assert await binary.ttl(lake_index_key()) == LAKE_COPY_TTL_SECONDS


async def test_the_index_ttl_is_refreshed_on_every_write(redis_pair) -> None:
    """An index that outlives what it points at over-counts the budget for as long as it survives.
    Refreshing it on each write keeps it at least as fresh as its newest member."""
    _text, binary = redis_pair
    await transfer_window(_lake(_FakeLake()), binary, _selection(_selected(1, 4_000)))
    await binary.expire(lake_index_key(), 5)

    await transfer_window(_lake(_FakeLake()), binary, _selection(_selected(2, 4_000)))

    assert await binary.ttl(lake_index_key()) == LAKE_COPY_TTL_SECONDS


async def test_the_stub_count_is_carried_through_to_the_report(redis_pair) -> None:
    """Days the window could not read are the selection's finding, not the transfer's, and they
    have to survive the hand-off — they are the only trace a bad stretch leaves."""
    _text, binary = redis_pair

    report = await transfer_window(
        _lake(_FakeLake()), binary, _selection(_selected(1, 4_000), skipped=3)
    )

    assert report.skipped_stubs == 3


# --- the skip -----------------------------------------------------------------------------------


async def test_a_window_already_held_downloads_nothing(redis_pair) -> None:
    """★ The owner's ruling: skipped when the same files are already held. Asserted on the LAKE
    rather than on Redis — the point is that nothing is downloaded, not that nothing is written."""
    _text, binary = redis_pair
    files = [_selected(day, 4_000) for day in (1, 2, 3)]
    lake = _FakeLake()
    await transfer_window(_lake(lake), binary, _selection(*files))
    lake.downloaded.clear()

    report = await transfer_window(_lake(lake), binary, _selection(*files))

    assert report.already_held is True
    assert report.copied == 0
    assert lake.downloaded == []


async def test_a_partly_held_window_copies_only_what_is_missing(redis_pair) -> None:
    """★ AND IS NEVER CLAIMABLE AS HELD. A marker key per window would have said "held" here,
    which is what makes a mid-window failure a permanent hole rather than something the next
    birth heals."""
    _text, binary = redis_pair
    lake = _FakeLake()
    await transfer_window(_lake(lake), binary, _selection(_selected(1, 4_000)))
    lake.downloaded.clear()
    files = [_selected(day, 4_000) for day in (1, 2, 3)]

    report = await transfer_window(_lake(lake), binary, _selection(*files))

    assert report.already_held is False
    assert report.copied == 2
    assert sorted(lake.downloaded) == sorted(
        f.name for f in files[1:] + files[:1] if f.day.day > 1
    )


# --- the budget ---------------------------------------------------------------------------------


async def test_writing_past_the_budget_evicts_the_oldest_copied_file_first(redis_pair) -> None:
    """★ OLDEST-COPIED, not oldest-dated and not whole-window. Eviction is at file granularity
    because a partial copy is exactly as unread as a whole one."""
    _text, binary = redis_pair
    third = LAKE_COPY_BUDGET_BYTES // 3 + 1
    first = _selected(1, third)
    second = _selected(2, third)
    third_file = _selected(3, third)
    lake = _FakeLake()
    lake.payloads = {f.name: b"x" * f.size for f in (first, second, third_file)}
    await transfer_window(_lake(lake), binary, _selection(first))
    await transfer_window(_lake(lake), binary, _selection(second))

    report = await transfer_window(_lake(lake), binary, _selection(third_file))

    assert report.evicted == 1
    assert not await binary.exists(lake_file_key(_digest(first.name))), "the oldest copy goes"
    assert await binary.exists(lake_file_key(_digest(second.name)))
    assert await binary.exists(lake_file_key(_digest(third_file.name)))


async def test_the_trim_stops_as_soon_as_the_incoming_file_fits(redis_pair) -> None:
    """It frees what it needs and no more. A trim that cleared the family to make room would
    throw away copies the client asked for, for nothing."""
    _text, binary = redis_pair
    tenth = LAKE_COPY_BUDGET_BYTES // 10
    lake = _FakeLake()
    held = [_selected(day, tenth) for day in range(1, 11)]
    lake.payloads = {f.name: b"x" * f.size for f in held}
    for file in held:
        await transfer_window(_lake(lake), binary, _selection(file))
    incoming = _selected(11, tenth)
    lake.payloads[incoming.name] = b"x" * tenth

    report = await transfer_window(_lake(lake), binary, _selection(incoming))

    assert report.evicted == 1
    assert await binary.zcard(lake_index_key()) == 10


async def test_a_file_larger_than_the_whole_budget_is_refused_before_anything_is_evicted(
    redis_pair,
) -> None:
    """★ Emptying the family to make room for something that still would not fit is the worst
    available outcome: the budget ends up spent on nothing."""
    _text, binary = redis_pair
    lake = _FakeLake()
    held = _selected(1, 4_000)
    await transfer_window(_lake(lake), binary, _selection(held))
    monster = _selected(2, LAKE_COPY_BUDGET_BYTES + 1)

    report = await transfer_window(_lake(lake), binary, _selection(monster))

    assert report.too_large_to_hold == 1
    assert report.evicted == 0
    assert report.copied == 0
    assert await binary.exists(lake_file_key(_digest(held.name))), "the held copy is untouched"


async def test_the_total_stays_honest_across_repeated_evictions(redis_pair) -> None:
    """★ THE DRIFT TEST. Write far past the budget and the accounted total must equal the sum of
    what is ACTUALLY still stored — not a counter that has wandered away from it.

    It cannot drift here by construction: the total is derived by summing the index, and a member
    is only ever removed by the same trim that deletes its file. That is the property being
    pinned, so a future rewrite to a counter has something to fail."""
    _text, binary = redis_pair
    chunk = LAKE_COPY_BUDGET_BYTES // 8
    lake = _FakeLake()
    for day in range(1, 25):
        file = _selected(day, chunk)
        lake.payloads[file.name] = b"x" * chunk
        await transfer_window(_lake(lake), binary, _selection(file))

    members = await binary.zrange(lake_index_key(), 0, -1)
    accounted = sum(int(m.decode().split(":")[0]) for m in members)
    stored = 0
    for member in members:
        digest = member.decode().split(":")[1]
        payload = await binary.get(lake_file_key(digest))
        stored += len(payload or b"")

    assert accounted == stored
    assert accounted <= LAKE_COPY_BUDGET_BYTES


# --- the blast radius ----------------------------------------------------------------------------


async def test_a_trim_at_and_over_budget_touches_no_other_family(redis_pair) -> None:
    """★ ALL SIX OTHER FAMILIES, not four. A bug that reached the start marker or the taskiq
    stream would pass a four-family test while taking down a live build — and the registry hash is
    the sole input to the sweep that DELETES AZURE CONTAINERS, so losing one strands a container
    permanently.

    The trim runs at the budget and then past it, so both the "nothing to free" and the "free
    repeatedly" paths are exercised against the same seeded neighbours."""
    text, binary = redis_pair
    neighbours = {
        lock_key(_USER): "a-lock-token",
        heartbeat_key(_USER): "2026-09-09T10:00:00+00:00",
        lease_key(_USER): "1789251600.0",
        starting_key(_USER): str(uuid.uuid4()),
    }
    for key, value in neighbours.items():
        await text.set(key, value)
    await text.hset(registry_key(_USER), mapping={"app_name": "sbx-abc", "state": "ready"})
    await text.xadd("bial:test:taskiq:stream", {"task": "a-queued-job"})

    quarter = LAKE_COPY_BUDGET_BYTES // 4
    lake = _FakeLake()
    for day in range(1, 9):
        file = _selected(day, quarter)
        lake.payloads[file.name] = b"x" * quarter
        await transfer_window(_lake(lake), binary, _selection(file))

    for key, value in neighbours.items():
        assert await text.get(key) == value, f"{key} was collateral damage"
    assert await text.hgetall(registry_key(_USER)) == {"app_name": "sbx-abc", "state": "ready"}
    assert await text.xlen("bial:test:taskiq:stream") == 1


async def test_the_lake_keys_sit_outside_the_sandbox_domain(redis_pair) -> None:
    """The prefixes differ at the segment after the environment, which is the earliest place they
    could. A fleet sweep scanning `bial:{env}:sandbox:*` cannot see a parquet copy, and this
    feature's own trim cannot see a container record."""
    from src.services.redis.keys import key_prefix, lake_key_prefix

    assert not lake_index_key().startswith(key_prefix())
    assert not lake_key_prefix().startswith(key_prefix())
    # They agree on everything ABOVE the domain segment and differ from there down, so neither
    # prefix can ever be a prefix of the other however the environment is spelled.
    assert lake_key_prefix().removesuffix("lake:") == key_prefix().removesuffix("sandbox:")


# --- failure ------------------------------------------------------------------------------------


async def test_a_download_failure_part_way_leaves_no_claimable_window(redis_pair) -> None:
    """★ The window must not be claimable as held after a partial copy — the next birth has to
    finish the job rather than skip it."""
    _text, binary = redis_pair
    files = [_selected(day, 4_000) for day in (1, 2, 3)]
    lake = _FakeLake(raises=LakeError("the lake stopped answering"))

    with pytest.raises(LakeError):
        await transfer_window(_lake(lake), binary, _selection(*files))

    healthy = _FakeLake()
    report = await transfer_window(_lake(healthy), binary, _selection(*files))
    assert report.already_held is False
    assert report.copied == 3


async def test_one_unreadable_file_does_not_cost_the_rest_of_the_window(redis_pair) -> None:
    """★ ONE BAD BLOB MUST NOT TRUNCATE THE COPY. A file can be deleted, or have its ACL changed,
    between the listing and the download. Without a per-file clause that single failure abandons
    every remaining file in the window — and does so again on the next birth, and the one after,
    because the listing keeps offering the same file. The window would be permanently short by
    everything older than the bad day, with nothing saying so.

    The count is asserted, not just the survival: a window quietly missing days must not read in
    the log like a window that got everything."""
    _text, binary = redis_pair
    files = [_selected(day, 4_000) for day in (1, 2, 3)]
    lake = _FakeLake()
    lake.unreadable = {files[1].name}

    report = await transfer_window(_lake(lake), binary, _selection(*files))

    assert report.copied == 2
    assert report.unreadable == 1
    # Newest first, with the bad day stepped over rather than ending the loop.
    assert lake.downloaded == [files[2].name, files[0].name]
    # The bad day is genuinely absent, which is what makes a later birth retry it instead of
    # finding a whole window present and claiming `already_held`.
    assert not await binary.exists(lake_file_key(_digest(files[1].name)))
    assert await binary.exists(lake_file_key(_digest(files[0].name)))


async def test_a_lake_that_serves_nothing_at_all_still_raises_rather_than_reporting_a_tidy_zero(
    redis_pair,
) -> None:
    """★ THE OTHER SIDE OF THE SAME CLAUSE, and the reason it is not just `continue`. A window
    where every file failed is one condition — the lake unreachable, the identity refused, the
    container gone — not thirty. Swallowing each one individually would turn a total outage into
    thirty warnings and a report saying `copied=0`, which reads exactly like an empty window.

    Re-raising hands it to `transfer_window_or_log`, which logs it once with the coordinates."""
    _text, binary = redis_pair
    files = [_selected(day, 4_000) for day in (1, 2, 3)]
    lake = _FakeLake()
    lake.unreadable = {file.name for file in files}

    with pytest.raises(LakeError):
        await transfer_window(_lake(lake), binary, _selection(*files))


async def test_a_lake_failure_is_swallowed_and_logged_by_the_guarded_entry_point(
    redis_pair,
) -> None:
    """★ Nothing reads this copy, so a citizen must never lose a build to it. `None` says the copy
    did not happen; no caller has to look."""
    _text, binary = redis_pair
    lake = _FakeLake(raises=LakeError("the lake refused"))

    assert await transfer_window_or_log(_lake(lake), binary, _selection(_selected(1, 1))) is None


async def test_redis_being_unreachable_is_swallowed_too(redis_pair) -> None:
    """The other half of the same rule, and the one that is easy to miss: a Redis blip on this
    path must not fail the build either."""
    _text, binary = redis_pair
    await binary.aclose()

    class _Dead:
        async def exists(self, *_: object) -> int:
            raise RedisConnectionError("connection refused")

    from typing import cast

    dead = cast("aioredis.Redis", _Dead())
    assert (
        await transfer_window_or_log(_lake(_FakeLake()), dead, _selection(_selected(1, 1))) is None
    )


async def test_an_empty_selection_writes_nothing_and_is_not_an_error(redis_pair) -> None:
    """A window whose days the lake has not loaded is a legitimate product state."""
    _text, binary = redis_pair

    report = await transfer_window(_lake(_FakeLake()), binary, _selection())

    assert report.copied == 0
    assert report.already_held is False
    assert await binary.zcard(lake_index_key()) == 0


async def test_a_member_this_code_did_not_write_cannot_stall_the_trim(redis_pair) -> None:
    """★ THE TRIM MUST ALWAYS MAKE PROGRESS. Production shares one Redis instance with other BIAL
    applications, so a member in an unrecognised format is a thing that can genuinely appear —
    and the eviction loop re-reads the total and evicts again until the incoming file fits. A
    member the trim could not remove would spin that loop forever, inside a detached task nothing
    is watching, on a code path whose failures are deliberately swallowed.

    Asserted with a timeout rather than by inspection, because "it terminates" is the property.

    A CAVEAT WORTH KNOWING BEFORE YOU DEBUG A WEDGED SUITE: `fakeredis` answers in-process, so a
    spinning loop here never yields to the event loop and `asyncio.timeout` cannot deliver its
    cancellation. Against real Redis this fails in ten seconds; against the fake it HANGS. The
    timeout is kept because it is free and correct where it can fire, and
    `test_every_evict_call_makes_progress` below is the one that fails fast and says why."""
    _text, binary = redis_pair
    lake = _FakeLake()
    half = LAKE_COPY_BUDGET_BYTES // 2 + 1
    await binary.zadd(lake_index_key(), {"not-a-member-this-code-wrote": 1.0})
    await binary.zadd(lake_index_key(), {f"{half}:{'a' * 64}": 2.0})
    incoming = _selected(9, half)
    lake.payloads[incoming.name] = b"x" * half

    async with asyncio.timeout(10):
        report = await transfer_window(_lake(lake), binary, _selection(incoming))

    assert report.copied == 1
    members = {m.decode() for m in await binary.zrange(lake_index_key(), 0, -1)}
    assert "not-a-member-this-code-wrote" not in members


async def test_every_evict_call_makes_progress(redis_pair) -> None:
    """★ THE TERMINATION PROPERTY, STATED DIRECTLY AND FAILING FAST. The caller's loop is
    `while total + size > budget: _evict_oldest(...)`, which terminates if and only if every call
    removes something. The two tests around this one drive that loop and are the honest end-to-end
    check — but a regression there manifests as a HANG (see the caveat above), and a hang tells a
    reader nothing about which member the trim could not shift.

    So the invariant is asserted one level down, against the three member shapes the index can
    actually contain: one this code wrote, one another deployment wrote in ASCII, and one that is
    not text at all. The cardinality must strictly decrease on every call, whatever the shape.

    Reverting `_as_text` to `errors="ignore"` fails this in under a second."""
    _text, binary = redis_pair
    await binary.zadd(lake_index_key(), {b"\xff\xfe not ascii": 1.0})
    await binary.zadd(lake_index_key(), {"some-other-app:whatever": 2.0})
    await binary.zadd(lake_index_key(), {f"{4000}:{'a' * 64}": 3.0})

    seen = [await binary.zcard(lake_index_key())]
    for _ in range(3):
        await _evict_oldest(binary)
        seen.append(await binary.zcard(lake_index_key()))

    assert seen == [3, 2, 1, 0], f"the trim stalled on a member it could not remove: {seen}"


async def test_a_sixty_four_character_member_that_is_not_hex_is_declined(redis_pair) -> None:
    """★ THE CASE THE HEX CHECK EXISTS FOR, WHICH NOTHING WAS REACHING. Every other foreign member
    in this file is either valid hex or fails the `isdigit()` gate on the size half first — so the
    digest predicate itself had never been exercised by a single test.

    It is the one that matters, because a 64-character non-hex digest is the ONLY input that gets
    past the size gate and reaches `lake_file_key`, whose own guard would then raise `ValueError`
    from inside a detached task. `_split_member` declining it is what makes that raise unreachable,
    which is the claim its comment makes and commit 42e47bdf made in its message.

    Asserted through the trim rather than by calling the private helper, so it pins the CONSEQUENCE
    — the member is removed and the loop advances — rather than the implementation."""
    _text, binary = redis_pair
    not_hex = f"4000:{'g' * 64}"
    await binary.zadd(lake_index_key(), {not_hex: 1.0})

    freed = await _evict_oldest(binary)

    assert freed == 0, "a member this code did not write frees no accounted bytes"
    assert await binary.zcard(lake_index_key()) == 0, "and it is gone, so the trim can advance"


async def test_a_member_that_cannot_round_trip_cannot_stall_the_trim_either(redis_pair) -> None:
    """★ THE SAME PROPERTY, AGAINST THE MEMBER THAT ACTUALLY BREAKS IT. The test above seeds an
    ASCII member, which decodes to itself — so `ZREM` matches it and the ordinary arm clears it.
    That never exercises the `_as_text is None` arm at all.

    A member with a NON-ASCII byte is the one that does. Decoded leniently it comes back as a
    DIFFERENT string, `ZREM` matches nothing, the member survives, and the loop re-reads the same
    oldest member forever — inside a detached task whose failures are swallowed. `_as_text`
    therefore decodes strictly and returns `None`, which routes the member to the by-rank
    fallback, and by-rank always advances because it names a POSITION rather than a value.

    Seeded through the binary client so the bytes reach Redis unmangled."""
    _text, binary = redis_pair
    lake = _FakeLake()
    half = LAKE_COPY_BUDGET_BYTES // 2 + 1
    undecodable = b"\xff\xfe not ascii"
    await binary.zadd(lake_index_key(), {undecodable: 1.0})
    await binary.zadd(lake_index_key(), {f"{half}:{'a' * 64}": 2.0})
    incoming = _selected(9, half)
    lake.payloads[incoming.name] = b"x" * half

    async with asyncio.timeout(10):
        report = await transfer_window(_lake(lake), binary, _selection(incoming))

    assert report.copied == 1
    assert undecodable not in set(await binary.zrange(lake_index_key(), 0, -1))
