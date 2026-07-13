"""Redis coordination service (contract C5).

U5 seeds this package with `RedisConfig`; U6 extends it with the async pool
factory (`client.py`) and the frozen C5 key namespace (`keys.py`). Public surface
is re-exported explicitly (`X as X`) so ty / mypy --strict / pyright read it as an
intentional re-export — never an `__all__` list (`.claude/rules/modules.md`).
"""

from __future__ import annotations

from src.services.redis.config import RedisConfig as RedisConfig
