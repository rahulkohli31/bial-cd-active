"""Track BRAIN — the Pydantic AI agentic build harness (contracts C2 / C6 / C7).

BRAIN owns this package and adds files ONLY here. It reaches everything else through frozen
contracts imported read-only: the C2 `SandboxClient` ABC, the C7 progress envelope + `run_build`
Protocol + `BuildResult` (rendered in `build_sessions/schemas.py`), the `gate.py` metering
surface, and `agent/model.py`'s Foundry client. Budgets/knobs are in-module constants — the
config surface is never touched (rule §5.9).

Public surface via explicit `from .x import Y as Y` re-exports (`.claude/rules/modules.md` — never
`__all__`).
"""

# The tool surface is no longer decorator-registered, so this is a plain re-export, not a
# side-effect import: `tools.sandbox_toolset` is the FACTORY `agent.py` constructs `build_agent`
# with (and a Write chat turn builds over its own deps). The dependency runs agent.py → tools.py.
from src.services.orchestrator import tools as tools
from src.services.orchestrator.agent import build_agent as build_agent
from src.services.orchestrator.deps import BuildDeps as BuildDeps
from src.services.orchestrator.deps import SandboxSession as SandboxSession
from src.services.orchestrator.harness import BuildOrchestrator as BuildOrchestrator
from src.services.orchestrator.harness import BuildSpec as BuildSpec
from src.services.orchestrator.harness import RunContextProvider as RunContextProvider
from src.services.orchestrator.progress import ProgressEmitter as ProgressEmitter
