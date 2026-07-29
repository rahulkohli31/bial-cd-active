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
    # W1 / KTD-5e — file mutations since the model's last `git commit`, so the commit reminder
    # can fire on the CADENCE the requirement asks for ("after a coherent slice") rather than on
    # every single write. An unconditional reminder becomes wallpaper and stops being read.
    # Reset by `run_command` when it sees a commit go through, which is what makes the count mean
    # *uncommitted* rather than merely *recent*.
    uncommitted_writes: int = 0
    # F8/U5 — the SHARED "preview is framed" guard, hoisted out of the `_run_loop` local it used to
    # be so ALL THREE initial-frame emit sites consult ONE flag: (a) the warm-resume immediate
    # emit, (b) the decoupled early readiness watcher, (c) the between-steps verify. Seeded from
    # `handle.ready` in `__post_init__` so a warm/resumed sandbox that emits `preview_ready`
    # immediately never double-fires with the watcher's first poll. The watcher exclusively owns
    # the later crash→reconnect→reframe cycle (verify never re-claims), so this is claim-once.
    preview_framed: bool = False

    def __post_init__(self) -> None:
        # A warm/resumed sandbox is already serving — treat the frame as claimed at construction so
        # the warm-resume emit (gated on `handle.ready`) fires once and the watcher/verify see it
        # taken. A cold sandbox starts unframed; the watcher or verify claims it on first serve.
        if self.handle.ready:
            self.preview_framed = True

    def claim_preview_frame(self) -> bool:
        """Synchronously claim the one-time preview-framed transition — True for EXACTLY ONE caller
        across the initial-frame emit sites. The test-and-set has NO `await` between the "is it
        set?" check and the "set it" write, so the early watcher and the between-steps loop can
        never both see it unset and both emit `preview_ready` with two different seqs (a
        double-frame). This is what makes a second concurrent emitter safe (KD-12)."""
        if self.preview_framed:
            return False
        self.preview_framed = True
        return True
