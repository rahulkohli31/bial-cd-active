"""The Taskiq broker's non-default arguments (U4, ADR-0011 §4).

Every assertion here is a regression guard against a LIBRARY DEFAULT, not a preference. Each
default this file pins away from causes a silent failure — a hot reconnect loop, an unbounded
stream in a shared Redis, a permanently wedged autoclaim key, or two environments consuming each
other's messages. None of them raises; they all just quietly misbehave, which is why they are
asserted rather than trusted.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import SecretStr
from taskiq import InMemoryBroker
from taskiq_redis import RedisStreamBroker

from src.broker import STREAM_MAXLEN, UNACKED_LOCK_TIMEOUT_S, XREAD_BLOCK_MS, broker
from src.services.redis.config import RedisConfig

_BACKEND_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def redis_broker(monkeypatch: pytest.MonkeyPatch) -> RedisStreamBroker:
    """A real `RedisStreamBroker`, built the way production builds it.

    The suite's own broker is an `InMemoryBroker`: `.env.test` carries no `REDIS__*` block, and
    `build_broker()` falling back rather than raising is exactly the total-construction property
    `test_the_broker_falls_back_to_in_memory_without_redis` pins. So the arguments under test
    have to be asserted against a deliberately-built instance.

    Constructing the broker opens no socket — redis-py connects lazily — so no live Redis is
    needed to read back what was passed.
    """
    from src.broker import build_broker
    from src.config import settings

    monkeypatch.setattr(settings, "redis", RedisConfig(url=SecretStr("redis://localhost:6379/0")))
    built = build_broker()
    assert isinstance(built, RedisStreamBroker)
    return built


def test_the_blocking_read_cannot_outlast_the_socket_timeout(
    redis_broker: RedisStreamBroker,
) -> None:
    """THE invariant that makes `RedisStreamBroker` safe on redis-py 8.

    redis-py 8 introduced a 5-second default `socket_timeout`. A blocking read that out-waits it
    raises `TimeoutError` and reconnects forever — upstream taskiq-redis #127, which is why an
    earlier plan draft wanted to pin `redis>=7,<8`. That pin was unimplementable (the `api` group
    already requires redis>=8) and unnecessary: the stream broker is safe precisely because its
    block sits under the timeout.

    Both values are passed explicitly so neither a library default change nor a config edit can
    silently cross them. `socket_timeout` must be read off the POOL's connection kwargs — it is a
    `Connection` default in redis-py, so a broker that failed to pass it would show no key here
    at all rather than showing 5.
    """
    connection_kwargs = redis_broker.connection_pool.connection_kwargs

    assert "socket_timeout" in connection_kwargs, (
        "pass socket_timeout explicitly — redis-py's 5s default is a module constant, not a pool "
        "kwarg, so relying on it leaves this invariant unassertable"
    )
    assert redis_broker.block == XREAD_BLOCK_MS
    assert redis_broker.block / 1000 < connection_kwargs["socket_timeout"], (
        f"blocking read ({redis_broker.block}ms) must stay under the socket timeout "
        f"({connection_kwargs['socket_timeout']}s) or the worker reconnect-loops forever"
    )


def test_the_stream_and_group_names_carry_the_environment(
    redis_broker: RedisStreamBroker,
) -> None:
    """Taskiq defaults both to the bare string "taskiq", in a Redis shared with other BIAL GenAI
    applications — two deployments would consume each other's messages.

    The consumer group is doubly load-bearing: the library derives an
    `autoclaim:<group>:<stream>` key whose literal prefix sits OUTSIDE the `bial:` namespace and
    cannot be moved under it, so the group name is the only thing keeping that key distinct
    between environments (C5).
    """
    assert redis_broker.queue_name.startswith("bial:"), redis_broker.queue_name
    assert redis_broker.consumer_group_name.startswith("bial:"), redis_broker.consumer_group_name
    assert redis_broker.queue_name != "taskiq"
    assert redis_broker.consumer_group_name != "taskiq"
    # The two must not collide with each other either.
    assert redis_broker.queue_name != redis_broker.consumer_group_name


def test_the_stream_is_length_capped(redis_broker: RedisStreamBroker) -> None:
    """`XACK` does NOT trim: acknowledging a message leaves it in the stream forever. An
    untrimmed stream carries no TTL, so it cannot be evicted under a `volatile-*` policy either,
    and it would grow without bound and crowd the coordination keys reclamation depends on."""
    assert redis_broker.maxlen == STREAM_MAXLEN
    assert redis_broker.maxlen is not None


def test_the_autoclaim_lock_has_a_ttl(redis_broker: RedisStreamBroker) -> None:
    """Left at its default (`None`) the autoclaim lock is a `SET NX` with NO expiry, so a
    SIGKILL taken while holding it wedges the key permanently and `XAUTOCLAIM` silently never
    runs again — unacknowledged messages would then never reach a surviving consumer.

    NOTE the limit of what this buys: under taskiq-redis 1.2.3 the autoclaim path runs on a
    buffered pipeline, so `acquire()` returns truthy WITHOUT executing and grants no cross-worker
    exclusion at all. This is set for forward-compatibility, not relied upon.
    """
    assert redis_broker.unacknowledged_lock_timeout == UNACKED_LOCK_TIMEOUT_S
    assert redis_broker.unacknowledged_lock_timeout is not None


def test_there_is_no_result_backend(redis_broker: RedisStreamBroker) -> None:
    """Nothing awaits a reclamation result, and the Redis result backend defaults to an
    arbitrary-object binary serializer reading unprefixed keys from a database shared with other
    applications — a remote-code-execution surface in exchange for results nothing reads."""
    from taskiq.result_backends.dummy import DummyResultBackend

    assert isinstance(redis_broker.result_backend, DummyResultBackend)


def test_no_retry_middleware_is_installed() -> None:
    """These tasks delete Azure resources. taskiq installs no retry middleware by default and
    none may be added: taskiq-redis has no delivery-count cap and no dead-letter, so a message
    that crashes the worker would be an unbounded destructive-retry loop.

    Guarding the middleware list rather than a comment, because the dangerous form
    (`SimpleRetryMiddleware(default_retry_label=True)`) enables retries for EVERY task in the
    process, not just the one someone meant to make retryable.
    """
    installed = [type(m).__name__ for m in broker.middlewares]
    assert not any("Retry" in name for name in installed), installed


def test_the_broker_falls_back_to_in_memory_without_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    """Construction must be TOTAL. `settings.redis` is optional, `.env.test` carries no `REDIS__*`
    block, and `conftest.py` imports the app at module scope — so a factory that raised without
    Redis would make the entire suite uncollectable.

    A real worker cannot reach this branch: `WorkerSettings` requires Redis (U23).
    """
    from src.broker import build_broker
    from src.config import settings

    monkeypatch.setattr(settings, "redis", None)
    assert isinstance(build_broker(), InMemoryBroker)


def test_the_broker_imports_without_the_fastapi_app() -> None:
    """The worker imports this module and never builds a FastAPI app. A cold interpreter is the
    only honest check — conftest has already imported `src.main` in-process."""
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-B", "-c", "import src.broker; print('ok')"],
        cwd=_BACKEND_ROOT,
        env={"PATH": os.environ["PATH"], "ENV_FILE": ".env.test"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "ok" in result.stdout
