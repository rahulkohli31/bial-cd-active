"""The per-run dependency bundle the sandbox tools receive.

ONE dataclass now. `SandboxSession` holds EVERYTHING the eight sandbox tools touch, and nothing
else; a Write chat turn carries it on its own `ChatDeps.sandbox`, so one tool body serves the
whole surface (`tools.sandbox_toolset`).

`BuildDeps` — the harness-only surround (owner `user_id`, the single `ProgressEmitter`, the
claim-once preview-frame guard) — was deleted with the standalone build harness. The turn engine
keeps its own equivalents on the turn state (`turns/engine.py::claim_preview_frame`); they were
never shared with this file, only mirrored.

There are deliberately NO caches: an uncached `view` is always correct; a cache without
invalidation risks stale content mid-self-heal.
"""

from __future__ import annotations

import uuid
from collections import OrderedDict
from dataclasses import dataclass, field

from src.services.orchestrator.constants import (
    OUTPUT_SLICE_HANDLES_PER_TURN,
    REPEATED_COMMAND_MEMORY,
)
from src.services.orchestrator.progress import ProgressEmitter
from src.services.sandbox import SandboxClient, SandboxHandle


@dataclass(frozen=True)
class HeldOutput:
    """One command's output, held for this turn so `fetch_output_slice` can read the middle the
    cap removed.

    `lines` IS ALREADY REDACTED — THE WHOLE SECURITY PROPERTY OF THIS CLASS: it is
    `scrub_untrusted`'s output (capped, de-escaped, masked) split on newlines, nothing else may
    ever go in it. The returned artifact was only ever an already-redacted head; a handle retains
    a SECOND artifact, and the middle it holds is precisely the part a human never read. Raw
    stdout would be a direct path to an unmasked secret, and a secret sitting entirely in an
    elided middle is the ordinary case here, not a boundary one. Frozen: a held capture is a
    historical fact about an already-exited command.
    """

    #: The command this output came from, redacted and capped — model-facing header text only.
    command: str
    #: The redacted output, one entry per line. Never raw stdout/stderr.
    lines: tuple[str, ...]


@dataclass
class SandboxSession:
    """Everything the eight sandbox tools touch, and nothing else. Mutable so `declare_done` can
    flip the done-signal; the harness resets it at the start of each run.

    SECRET-SAFETY RULE. `handle.token` is the LIVE supervisor bearer. Never `log()`,
    `repr()`, or return `SandboxSession` or `SandboxHandle` wholesale, and never render
    `handle.token` into an error message or a model-visible tool result. Exception logging binds
    only `session_id` / `user_id` / `app_id` — never `handle` / `handle.token`.
    """

    sandbox_client: SandboxClient
    handle: SandboxHandle
    # From the run-context, so `BuildResult.app_id` is populated and the BRAIN trace binds.
    app_id: uuid.UUID
    # ── Mutable per-run signals the tools set and the loop reads ──────────────────────────────
    done_requested: bool = False
    done_summary: str = ""
    # `uncommitted_writes` LIVED HERE and is gone. It counted file mutations since the model's
    # last `git commit` so `tools._note_write_and_maybe_remind` could nag at a cadence — and the
    # instruction it nagged about (the Write segment's COMMIT AS YOU WORK block) has been
    # deleted, because the platform commits the tree itself at every turn boundary. A counter
    # enforcing an instruction nobody gives is worse than no counter: it appends a reminder to
    # tool results for a discipline the prompt no longer teaches.
    # HOW MANY TIMES this turn mutated the tree. Bumped by `write_file` / `edit_file` /
    # `insert_lines` / `apply_schema_change` / `run_command`, never reset mid-turn.
    #
    # A COUNT RATHER THAN A FLAG, because two readers ask different questions of it. Most ask
    # `workspace_touched` — "did anything change in this whole turn" — since a Write turn that
    # only read files is an ordinary chat turn and must not pay for a verify pass or a self-heal
    # nudge. `app_state_toolset` instead asks "has the tree moved since I last looked", which a
    # flag cannot answer once it has latched: a model that edits, checks, edits and checks again
    # would be handed its pre-edit reading under a tool that promises the app's state right now.
    #
    # This is NOT the deleted `uncommitted_writes`. That one counted writes since the model's
    # last commit in order to nag about a commit discipline the prompt no longer teaches; this
    # one is read only to decide whether a cached reading is still true.
    writes: int = 0

    @property
    def workspace_touched(self) -> bool:
        """Did this turn mutate the tree at all?"""
        return self.writes > 0

    # ── The per-turn output buffer and the repeat-run memory ──────────────────────────────────
    # NEITHER IS PERSISTED. Both die with the session the harness rebuilds at the start of each
    # run, which is exactly the stated lifetime of a slice handle: a handle from a previous run
    # resolves to nothing and the model is told to re-run the command. Nothing here reaches the
    # database or blob storage.
    #
    # An OrderedDict, not a dict, because the eviction order IS the policy: `hold_output` drops
    # the OLDEST handle when the ring is full, so the notice the model just read always still
    # resolves.
    held_outputs: OrderedDict[str, HeldOutput] = field(default_factory=OrderedDict)
    # Redacted command strings already run this turn — the repeat-run adoption counter's memory.
    # Redacted, not raw, for the same reason `HeldOutput.lines` is: an argv token can carry
    # a credential, and this lives on the session for the whole turn.
    commands_seen: set[str] = field(default_factory=set)
    # The legacy build feed, and NOTHING IN PRODUCTION SETS IT ANY MORE: the only constructor
    # of a `ProgressEmitter` was the deleted harness, so on every live turn this is `None` and
    # `tools._step` takes its early return. It stays because `tools._step` still has to handle
    # both shapes and the tool tests drive the emitting arm; treat a non-None value as test-only.
    emitter: ProgressEmitter | None = None

    def hold_output(self, handle: str, held: HeldOutput) -> None:
        """Retain one truncated output under `handle`, evicting the oldest beyond the ring's cap.

        BOUNDED IN BOTH DIMENSIONS: each entry is capped upstream by `REDACT_INPUT_MAX_CHARS` and
        the ring holds `OUTPUT_SLICE_HANDLES_PER_TURN` of them, so a chatty turn cannot grow this
        without limit. Evicting the oldest (rather than refusing the newest) is deliberate — the
        handle the model was just handed is the one it is about to use."""
        self.held_outputs[handle] = held
        while len(self.held_outputs) > OUTPUT_SLICE_HANDLES_PER_TURN:
            self.held_outputs.popitem(last=False)

    def note_command(self, redacted_command: str) -> bool:
        """Record that this command ran; True if an IDENTICAL one already ran this turn.

        Capped at `REPEATED_COMMAND_MEMORY` distinct commands — past that it stops recording,
        so a pathological turn cannot grow the set unbounded; a repeat may then go uncounted,
        understating the metric but never affecting model behavior. Entries are whole argv
        strings, not hashed or shortened — a shortened key could collide two different commands
        into a false repeat, and an invented event is worse than a missed one. Each is already
        bounded by the argv redaction cap the caller applies.
        """
        repeated = redacted_command in self.commands_seen
        if not repeated and len(self.commands_seen) < REPEATED_COMMAND_MEMORY:
            self.commands_seen.add(redacted_command)
        return repeated
