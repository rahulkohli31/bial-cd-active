"""`BuildDeps` — the per-run dependency bundle every `@agent.tool` receives via
`RunContext[BuildDeps]` (KD-4 / KD-9 / KD-13).

Holds the injected C2 client + handle, the single `ProgressEmitter` (so tools and the harness
share ONE seq source, KD-12), the owner `user_id`, the `app_id` (from the KD-13 run-context, so
`BuildResult.app_id` is populated), and a mutable done-signal the `declare_done` tool sets and the
harness verify gate reads (KD-6). There are deliberately NO caches (KD-10): an uncached `view` is
always correct; a cache without invalidation risks stale content mid-self-heal.

SECRET-SAFETY RULE (KD-9). `handle.token` is the LIVE supervisor bearer. Never `log()`, `repr()`,
or return `BuildDeps` or `SandboxHandle` wholesale, and never render `handle.token` into an error
message or a model-visible tool result. Exception logging binds only `session_id` / `user_id` /
`app_id` — never `handle` / `handle.token`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from src.services.orchestrator.progress import ProgressEmitter
from src.services.sandbox import SandboxClient, SandboxHandle


@dataclass
class BuildDeps:
    """Per-run agent dependencies. Mutable so `declare_done` can flip the done-signal; the
    harness resets it at the start of each run (KD-6)."""

    sandbox_client: SandboxClient
    handle: SandboxHandle
    emitter: ProgressEmitter
    user_id: uuid.UUID
    app_id: uuid.UUID
    done_requested: bool = False
    done_summary: str = ""
