"""The Taskiq broker (ADR-0011).

One module-level singleton, `broker`, that task modules decorate against and `worker_main`
consumes from. Every non-default argument below closes a specific silent failure; none of them
is style.

IMPORT DISCIPLINE. This module imports only stdlib, structlog, taskiq and the settings front
door — never the ARM SDK, never the ORM, never FastAPI. It is imported by every task module, and
a task whose flag is off must cost nothing. On Python 3.14 the POSIX multiprocessing start
method is `forkserver`, so a child inherits nothing and each process builds its own settings
from the environment.

CONSTRUCTION IS TOTAL. `settings.redis` is optional and the test environment carries no
`REDIS__*` block at all, while `conftest.py` imports the app at module scope — so a broker
factory that raised without Redis would make the whole suite uncollectable. `build_broker()`
returns an `InMemoryBroker` in that case. Note this cannot be fixed after the fact by
monkeypatching the singleton: `AsyncTaskiqDecoratedTask.__init__` binds `self.broker` at
DECORATION time, so rebinding this module's global after import changes nothing for tasks that
are already decorated.
"""

from __future__ import annotations

import structlog
from taskiq import AsyncBroker, InMemoryBroker
from taskiq_redis import RedisStreamBroker

from src.config import settings

_log = structlog.get_logger()

# How long `XREADGROUP` blocks per poll, in MILLISECONDS.
#
# This is half of a safety invariant, not a tuning knob. redis-py 8 introduced a default
# `socket_timeout` of 5 seconds, and a blocking read that out-waits the socket timeout raises
# `TimeoutError` and reconnects forever — upstream taskiq-redis #127. The invariant is:
#
#     XREAD_BLOCK_MS / 1000  <  SOCKET_TIMEOUT_S
#
# Both are passed EXPLICITLY below rather than inherited, so neither a library default change nor
# a config edit can silently cross them, and `tests/test_broker.py` asserts the inequality holds.
# (#127 itself is scoped to `ListQueueBroker`, which is rejected below for independent reasons —
# but the invariant is what makes the stream broker safe, so it is stated rather than assumed.)
XREAD_BLOCK_MS: int = 2000
SOCKET_TIMEOUT_S: float = 5.0

# Trim the stream to roughly this many entries. `XACK` does NOT trim — acknowledging a message
# leaves it in the stream forever. An untrimmed stream carries no TTL, so it cannot be evicted
# under a `volatile-*` policy either, and it would grow without bound in a Redis shared with
# other BIAL applications, crowding out the coordination keys reclamation depends on.
STREAM_MAXLEN: int = 1000

# The autoclaim lock's TTL. Left to its default (`None`) it is a `SET NX` with NO expiry, so a
# SIGKILL taken while holding it wedges the key permanently and `XAUTOCLAIM` silently never runs
# again — unacknowledged messages would then never be redelivered to a surviving consumer.
#
# Set for forward-compatibility, but do NOT rely on it for exclusion: under taskiq-redis 1.2.3
# the autoclaim path is built on a buffered pipeline, so `acquire()` returns truthy WITHOUT
# executing and the SET result is discarded. It grants no cross-worker mutual exclusion at all.
# If the worker ever runs more than one replica, that is the thing to solve — not this kwarg.
UNACKED_LOCK_TIMEOUT_S: int = 600

# The blocking connection pool defaults to an effectively unbounded size.
MAX_POOL_SIZE: int = 10


def _namespaced(base: str, environment: str) -> str:
    """Segment a broker key by environment.

    The Redis instance is shared with other BIAL GenAI applications, and taskiq's defaults for
    both the stream and the consumer group are the bare string `"taskiq"` — two deployments would
    silently consume each other's messages.

    Two environments sharing one instance MUST also differ in consumer group, because the
    library derives an `autoclaim:<group>:<stream>` key whose literal prefix sits OUTSIDE our
    `bial:` namespace and cannot be moved under it. That key is therefore the one part of the
    broker's footprint the environment-scoping guarantee does not cover, and the group name is
    what keeps it distinct (C5).

    THE BRACES AROUND `environment` ARE A REDIS HASH TAG AND ARE LOAD-BEARING, not decoration.
    Redis hashes only the substring between the first `{` and the following `}`, so this pins
    the stream and the derived autoclaim lock to the SAME slot:

        stream  bial:{production}:taskiq:stream                          -> tag 'production'
        lock    autoclaim:bial:{production}:taskiq:group:bial:{production}:taskiq:stream
                          ^^^^^^^^^^^^ the FIRST brace pair wins       -> tag 'production'

    WITHOUT IT THE WORKER CONSUMES NOTHING, and the failure is invisible from the config.
    `taskiq_redis.RedisStreamBroker.listen` wraps a lock `SET NX`, an `XAUTOCLAIM` and a Lua
    lock-release in ONE `MULTI` (redis-py pipelines are transactional by default). On a sharded
    Redis the two keys land in different slots, the transaction is rejected at queue time, and
    the receiver dies with `EXECABORT: Transaction discarded because of previous errors` — a
    message that names neither the command nor the reason. The scheduler keeps enqueuing behind
    it, so the container stays up and healthy while no task ever runs.

    Observed on Azure Managed Redis (`Microsoft.Cache/redisEnterprise`) on 2026-08-18, and NOTE
    THE TRAP: that instance reports `clusteringPolicy = EnterpriseCluster`, which is what made
    this look impossible. The policy governs only the client-facing protocol — one endpoint, no
    `MOVED` redirects. The database is still sharded, and `MULTI` still requires one slot:

        ClusterCrossSlotError: Keys in request don't hash to the same slot (context='within
        MULTI', command='xautoclaim', first-key='autoclaim:...', violating-key='...:stream')

    Do not "simplify" the braces away because a single-node dev Redis does not need them; a
    single node hashes every key to the same slot and cannot reproduce this.
    """
    return f"bial:{{{environment}}}:{base}"


def build_broker() -> AsyncBroker:
    """Construct the broker for this process, or an in-memory stand-in when Redis is absent.

    `RedisStreamBroker`, not `ListQueueBroker`: the latter issues an unbounded blocking pop, and
    its reconnect guard catches the BUILTIN `ConnectionError`, which `redis.exceptions.
    ConnectionError` does not subclass — so the guard is dead code, against an upstream issue
    literally titled "Occasional endless blocking dispatching tasks using Azure Redis".

    And not `PubSubBroker`, outright: it broadcasts, so a delete-capable task would execute once
    per subscriber.

    No result backend. Nothing awaits a reclamation result, and the Redis result backend defaults
    to an arbitrary-object binary serializer reading unprefixed keys from a database shared with
    other applications — a remote-code-execution surface in exchange for results nothing reads.
    (`RedisStreamBroker.__init__` cannot accept a `result_backend` kwarg at all; the default
    `DummyResultBackend` is what we want anyway.)
    """
    redis_config = settings.redis
    if redis_config is None:
        # A defined, correct state: no Redis means no queue. Dev and test run this way, and the
        # worker profile (`WorkerSettings`) requires Redis, so a real worker cannot land here.
        _log.info(
            "taskiq_broker_in_memory",
            detail=(
                "REDIS__URL is unset, so the task broker is in-memory: tasks execute inline and "
                "nothing is scheduled. Correct for dev/test; a worker cannot reach this branch "
                "because WorkerSettings requires Redis."
            ),
        )
        return InMemoryBroker()

    environment = settings.ENVIRONMENT
    return RedisStreamBroker(
        url=redis_config.url.get_secret_value(),
        queue_name=_namespaced("taskiq:stream", environment),
        consumer_group_name=_namespaced("taskiq:group", environment),
        xread_block=XREAD_BLOCK_MS,
        socket_timeout=SOCKET_TIMEOUT_S,
        maxlen=STREAM_MAXLEN,
        approximate=True,
        unacknowledged_lock_timeout=UNACKED_LOCK_TIMEOUT_S,
        max_connection_pool_size=MAX_POOL_SIZE,
    )


# The singleton. Task modules do `from src.broker import broker` and decorate against it.
#
# Lifecycle handlers, if any are ever added, are registered HERE at module scope on this object —
# never as a side effect of `build_broker()`. Registering inside the factory yields duplicate
# handlers if it is ever called twice, which matters far more here than in the reference
# implementation: a startup handler in taskiq can spawn tasks.
broker: AsyncBroker = build_broker()
