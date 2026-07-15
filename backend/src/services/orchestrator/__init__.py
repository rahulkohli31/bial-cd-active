"""Track BRAIN — the Pydantic AI agentic build harness (contracts C2 / C6 / C7).

BRAIN owns this package and adds files ONLY here. It reaches everything else through frozen
contracts imported read-only: the C2 `SandboxClient` ABC, the C7 progress envelope + `run_build`
Protocol + `BuildResult` (rendered in `build_sessions/schemas.py`), the `gate.py` metering
surface, and `agent/model.py`'s Foundry client. Budgets/knobs are in-module constants — the
config surface is never touched (rule §5.9).

Public surface via explicit `from .x import Y as Y` re-exports (`.claude/rules/modules.md` — never
`__all__`).
"""

# `tools` is imported for its side effect: the `@build_agent.tool` decorators register the tool
# surface — five file tools + `run_command` (tools.py imports agent.py, so build_agent is fully
# built first — the order here is immaterial). Importing the package guarantees registration.
from src.services.orchestrator import tools as tools
from src.services.orchestrator.agent import build_agent as build_agent
from src.services.orchestrator.deps import BuildDeps as BuildDeps
from src.services.orchestrator.harness import BuildOrchestrator as BuildOrchestrator
from src.services.orchestrator.harness import BuildSpec as BuildSpec
from src.services.orchestrator.harness import RunContextProvider as RunContextProvider
from src.services.orchestrator.progress import ProgressEmitter as ProgressEmitter
