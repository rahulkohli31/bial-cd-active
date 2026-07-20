"""Redis coordination service (contract C5).

`RedisConfig` (the typed config, U5), the async pool factory + lifecycle
(`client.py`, U6), and the frozen C5 key namespace (`keys.py`, U6). Reaper /
lock / registry LOGIC is SESSION-API's (Wave 1); Stage 0 freezes only the pool
lifecycle and the key builders. Public surface is re-exported explicitly
(`X as X`) so ty / mypy --strict / pyright read it as an intentional re-export —
never an `__all__` list (`.claude/rules/modules.md`).
"""

from __future__ import annotations

from src.services.redis.client import (
    RedisNotConfiguredError as RedisNotConfiguredError,
)
from src.services.redis.client import (
    aclose_redis as aclose_redis,
)
from src.services.redis.client import (
    create_redis as create_redis,
)
from src.services.redis.client import (
    get_redis as get_redis,
)
from src.services.redis.client import (
    reset_redis_for_tests as reset_redis_for_tests,
)
from src.services.redis.config import RedisConfig as RedisConfig
from src.services.redis.errors import (
    BUILD_COORDINATION_UNAVAILABLE_MSG as BUILD_COORDINATION_UNAVAILABLE_MSG,
)
from src.services.redis.errors import (
    build_coordination_or_503 as build_coordination_or_503,
)
from src.services.redis.keys import (
    KEY_PREFIX as KEY_PREFIX,
)
from src.services.redis.keys import (
    REGISTRY_FIELDS as REGISTRY_FIELDS,
)
from src.services.redis.keys import (
    REGISTRY_STATE_ENDING as REGISTRY_STATE_ENDING,
)
from src.services.redis.keys import (
    REGISTRY_STATE_READY as REGISTRY_STATE_READY,
)
from src.services.redis.keys import (
    heartbeat_key as heartbeat_key,
)
from src.services.redis.keys import (
    lock_key as lock_key,
)
from src.services.redis.keys import (
    registry_key as registry_key,
)
