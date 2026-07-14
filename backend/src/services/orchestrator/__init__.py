"""Track BRAIN — the Pydantic AI agentic build harness (contracts C2 / C6 / C7).

BRAIN owns this package and adds files ONLY here. It reaches everything else through frozen
contracts imported read-only: the C2 `SandboxClient` ABC, the C7 progress envelope + `run_build`
Protocol + `BuildResult` (rendered in `build_sessions/schemas.py`), the `gate.py` metering
surface, and `agent/model.py`'s Foundry client. Budgets/knobs are in-module constants — the
config surface is never touched (rule §5.9).

Public surface via explicit `from .x import Y as Y` re-exports (`.claude/rules/modules.md` — never
`__all__`).
"""

from src.services.orchestrator.deps import BuildDeps as BuildDeps
from src.services.orchestrator.progress import ProgressEmitter as ProgressEmitter
