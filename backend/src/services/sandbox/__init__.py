"""Sandbox runtime service (contracts C2 / C4).

U5 seeds this package with `SandboxConfig`; U7 extends it with the frozen C2
sandbox-client ABC + `SandboxHandle` + typed exceptions (`base.py`). Public surface
is re-exported explicitly (`X as X`) so ty / mypy --strict / pyright read it as an
intentional re-export — never an `__all__` list (`.claude/rules/modules.md`).
"""

from __future__ import annotations

from src.services.sandbox.config import SandboxConfig as SandboxConfig
