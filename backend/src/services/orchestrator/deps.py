"""The per-run dependency bundles the sandbox tools and the build harness receive (KD-4 / KD-9 /
KD-13).

Two dataclasses, split along the seam U5 needs:

* `SandboxSession` — EVERYTHING the six sandbox tools touch, and nothing else. It is held by
  `BuildDeps.sandbox` (the legacy `/build-sessions` harness) and, from U5, by a Write chat turn's
  own deps, so ONE tool body serves both consumers (`tools.sandbox_toolset`).
* `BuildDeps` — the harness-only surround: the owner `user_id`, the single `ProgressEmitter` (so
  tools and the harness share ONE seq source, KD-12), and the claim-once preview-frame guard.

There are deliberately NO caches (KD-10): an uncached `view` is always correct; a cache without
invalidation risks stale content mid-self-heal.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from src.services.orchestrator.progress import ProgressEmitter
from src.services.sandbox import SandboxClient, SandboxHandle


@dataclass
class SandboxSession:
    """Everything the six sandbox tools touch, and nothing else. Mutable so `declare_done` can flip
    the done-signal; the harness resets it at the start of each run (KD-6).

    SECRET-SAFETY RULE (KD-9). `handle.token` is the LIVE supervisor bearer. Never `log()`,
    `repr()`, or return `SandboxSession` or `SandboxHandle` wholesale, and never render
    `handle.token` into an error message or a model-visible tool result. Exception logging binds
    only `session_id` / `user_id` / `app_id` — never `handle` / `handle.token`.
    """

    sandbox_client: SandboxClient
    handle: SandboxHandle
    # From the KD-13 run-context, so `BuildResult.app_id` is populated and the BRAIN trace binds.
    app_id: uuid.UUID
    # ── Mutable per-run signals the tools set and the loop reads ──────────────────────────────
    done_requested: bool = False
    done_summary: str = ""
    # W1 / KTD-5e — file mutations since the model's last `git commit`, so the commit reminder
    # can fire on the CADENCE the requirement asks for ("after a coherent slice") rather than on
    # every single write. An unconditional reminder becomes wallpaper and stops being read.
    # Reset by `run_command` when it sees a commit go through, which is what makes the count mean
    # *uncommitted* rather than merely *recent*.
    uncommitted_writes: int = 0
    # Did this turn MUTATE the tree? Set by `write_file` / `edit_file` / `insert_lines` and by
    # `declare_done`, never reset mid-turn — "did anything change in this whole turn" is the
    # question. A Write turn that only read files is an ordinary chat turn and must not pay for a
    # verify pass or a self-heal nudge.
    workspace_touched: bool = False
    # The legacy C7 build feed. `None` on a chat turn, where the turn ENGINE emits the step frames
    # from the run's own tool events — see `tools._step` for why emitting both would double-render.
    emitter: ProgressEmitter | None = None


@dataclass
class BuildDeps:
    """The legacy build harness's per-run agent dependencies: the sandbox session the tools resolve
    through, plus the harness-only surround (owner scope, the C7 emitter, the preview-frame
    guard)."""

    sandbox: SandboxSession
    emitter: ProgressEmitter
    user_id: uuid.UUID
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
        if self.sandbox.handle.ready:
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
