"""The sandbox tool surface and its per-run dependencies.

THE DIRECTORY NAME IS HISTORICAL: everything here serves the live chat turn, not an orchestrator
of its own. The sandbox toolset (`tools.py`), the destructive-SQL sentinel (`sql_guard.py`), the
self-heal loop (`selfheal.py`), the redacted error surface (`errors.py`), the shared budgets
(`constants.py`), the generated app's own error reporter (`client_errors.py`), the repair prompt
(`prompt.build_repair_prompt`) and the per-run sandbox dependency below.

It reaches the sandbox only through the `SandboxClient` ABC, imported read-only. Budgets/knobs are
in-module constants — the config surface is never touched.
"""

# A plain re-export, not a side-effect import: `tools.sandbox_toolset` is the FACTORY the chat
# agent's Build arm composes its workspace tools from (`agent/toolsets.py`), over its own deps.
from src.services.orchestrator import tools as tools
from src.services.orchestrator.deps import SandboxSession as SandboxSession
