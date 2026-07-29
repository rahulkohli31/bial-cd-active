"""In-module budgets, knobs, and the fail-closed file-surface guards (KD-7 / KD-9 / KD-10).

Every value BRAIN needs to bound the self-heal loop, clamp the model, and decide which paths
the model may touch lives HERE — never in `config.py` (rule §5.9 / `.claude/rules`; the config
surface is frozen for this track). Two guards are load-bearing security boundaries:

* `is_write_allowed` — the fail-closed write gate for the three mutator tools. The open-sandbox
  model (the vibe-coding pivot) makes the WHOLE workspace editable — config, `package.json`, the
  lockfile, and the app's own data client — so the old positive `app/`/`components/`/`lib/`
  allowlist and the never-edit set are GONE. Only two denials remain: `_normalize_rel` rejects
  absolute paths and `..` escapes (mirroring the supervisor `_resolve`), and `.git/**` is denied
  so a file tool can't corrupt the snapshot history. This is defense-in-depth, NOT the containment
  boundary — `run_command` can write anywhere regardless, so the real boundary is the supervisor
  workspace-escape guard + demoted `appuser` (R6).
* `is_read_ignored` — a denylist is fine HERE because a read cannot mutate (KD-10); it exists
  only to bound the context window, not to contain a threat.
"""

from __future__ import annotations

import posixpath
from typing import Literal

# --- self-heal + model budgets (KD-7) ----------------------------------------

SELF_HEAL_MAX_RETRIES = 3
"""Flat repair-run budget (the brief's "~3 retries then escalate"). A red harness verify burns
one; at exhaustion BRAIN escalates → `ended(failed)`. OSS-validated 3-strike default (Aider
`max_reflections`, Roo `DEFAULT_CONSECUTIVE_MISTAKE_LIMIT`)."""

MODEL_TURN_CEILING = 50
"""`UsageLimits.request_limit` per `agent.iter` run — bounds a single run's model requests so a
within-run tool-call loop can't run away (a breach raises `UsageLimitExceeded` → escalation).
Distinct from the daily token quota (per-user, DB) and the self-heal budget (KD-7)."""

RUN_WALL_CLOCK_DEADLINE_S = 1800.0
"""Hard WALL-CLOCK ceiling on a single `run_build`, independent of the count-based ceilings
(`MODEL_TURN_CEILING`, `SELF_HEAL_MAX_RETRIES`). Those cap the number of model requests and repair
runs but NOT elapsed time: a slow or wedged model-driven `run_command` can burn up to
`RUN_COMMAND_SLOW_TIMEOUT_S` (600s) EACH, so with only count ceilings a pathological build could
hold the ACA container + the one-per-user sandbox lock for hours. Escalates through the SAME
funnel the turn/self-heal ceilings use (`_escalation` → `ended(failed)`).

Checked BETWEEN loop iterations (never mid-`run_command`), so the true worst case is roughly this
deadline plus one in-flight command. Deliberately GENEROUS so a legitimately long-but-healthy build
(a cold-base `npm install`, several repair rounds) never trips it: sized well above one
`RUN_COMMAND_SLOW_TIMEOUT_S` and to ~2× the documented C1 900s per-command hard cap (1800s /
30 min).
This is a safety net, not a tuned SLA — TUNE it against real end-to-end build telemetry."""

# --- harness-driven commands + timeouts (KD-4 / KD-8) ------------------------
# The model now HAS an exec tool — `run_command`, a general shell over the same exec transport
# (the vibe-coding pivot, U1). These two are the harness's OWN between-run verify invocation
# (`tsc --noEmit`): run by the harness, not the model, with their own timeout — DISTINCT from the
# model-driven bounds below.

TYPECHECK_CMD: tuple[str, ...] = ("npx", "tsc", "--noEmit")
"""The only reliable type signal: Next 16 + Turbopack HMR does not fail the dev server on type
errors, so `tsc --noEmit` is what the harness reads between runs (KD-8). `next build` is NOT run
in Wave-1 — the production build is a DEPLOY-track concern (Decision D2 / KD-6)."""

EXEC_TIMEOUT_S = 300
"""Wall-clock cap for a harness-driven command run (well under C1's 900s hard cap)."""

RUN_COMMAND_SLOW_TIMEOUT_S = 600
"""Wall-clock cap for the SLOW class of model-driven `run_command` — installs and type-check/build
runs (U4/F4) — well under C1's 900s hard cap, but
DISTINCT from `EXEC_TIMEOUT_S`. An `npm install` on the pre-baked base can take far longer than a
`tsc --noEmit`, and widening the shared `EXEC_TIMEOUT_S` would loosen the deterministic tsc verify
gate. Tune against a REAL `npm install` on the Windows-built sandbox image, not a guess (a value
too low turns a legitimate large install into a false ModelRetry)."""

READINESS_MAX_POLLS = 30
"""How many `dev_status` polls the verify step waits for a slow-but-healthy dev server to report
`ready` before concluding it is not up (open-Q F — a readiness poll is not a repair run)."""

READINESS_POLL_S = 1.0
"""Sleep between readiness polls. A construction-time knob on the orchestrator so tests can drive
it to 0."""

RUN_COMMAND_DEFAULT_TIMEOUT_S = 180
"""Wall-clock cap for EVERY OTHER model-driven `run_command` (F4).

ONE global bound could not satisfy both halves of this fix, which is why there are two. The
observed wedge — a `drizzle-kit generate` blocked on an interactive prompt with no terminal to
answer it — burned 249s of a 7.5-minute build, so a bound that catches it must sit well under
that. But this file's own `RUN_COMMAND_SLOW_TIMEOUT_S` documents that a cold-base `npm install`
"routinely" consumes the full 600s, so lowering the single global would fail healthy builds.

The class comes from `projection.command_needs_the_long_timeout`, which reuses the SAME
classifier the friendly labels come from — a second classifier could disagree with the first
about what a command is.

TUNE against real telemetry: this is sized to catch the observed 249s stall with margin, not
measured against a distribution of legitimate command durations."""

PREVIEW_WATCH_POLL_S = 1.0
"""How often the DECOUPLED early readiness watcher polls `/dev/status` (F8/U5). It frames the
preview the instant the dev server serves — decoupled from the between-runs verify cadence, which
only checks at node boundaries and so would frame at first-model-response, not first-serve — and,
after framing, catches a dev-process crash (`running` true→false) to emit the distinct
`preview_reconnecting` signal. A construction-time knob on the orchestrator so tests drive it to 0.
TUNE against real build traces (like the run_command bounds, a starting value, not an SLA)."""

VERIFY_TRANSIENT_RETRIES = 2
"""Extra attempts a verify-step sandbox call (tsc exec / dev_status / dev_logs) gets on a
TRANSIENT `SandboxError` — one supervisor blip must not escalate a healthy build to a hard
FAILED. `SandboxGoneError` is never retried (restore-needed, terminal for the handle)."""

VERIFY_RETRY_BACKOFF_S = 2.0
"""Sleep between verify-step retries (short: the failure mode is a network blip, not a rebuild)."""

ATTACH_NOT_READY_RETRIES = 3
"""Extra `attach_existing` attempts on `SandboxNotReadyError` — cold-ACA ingress can wake slower
than the client's single ~8s reachability probe. `SandboxGoneError` still escalates immediately."""

ATTACH_RETRY_BACKOFF_S = 7.0
"""Sleep between attach re-probes (3 × ~7s on top of the probes ≈ ~30s total ingress tolerance)."""

# --- read/output bounds (KD-10) ----------------------------------------------

VIEW_MAX_LINES = 400
"""Cap on a single `read_file` view — C1 `view` has no size cap, so BRAIN bounds it in-module to
protect the context window (never view a whole large file)."""

LOG_TAIL_MAX_LINES = 200
"""Cap on how many `dev_logs` tail lines feed the de-noiser."""

CLEANED_STACK_MAX_CHARS = 4_000
"""Truncation cap for `BuildError.cleaned_stack` (the diagnostic egresses twice — portal
envelope + next-run prompt — so it stays bounded)."""

RUN_COMMAND_OUTPUT_MAX_CHARS = 16_000
"""Truncation cap on the redacted `run_command` stdout/stderr fed back to the model (U1). Larger
than `CLEANED_STACK_MAX_CHARS` — command output IS the model's working signal (an `npm install`
summary, a lint report), not just a diagnostic tail — but still bounded so a chatty run can't
dominate the context window. The RAW output is capped to `REDACT_INPUT_MAX_CHARS` BEFORE redaction
(the linear ReDoS guard), then the redacted result is truncated to this."""

REDACT_INPUT_MAX_CHARS = 32_000
"""Hard cap on the RAW diagnostic length fed to the secret-redactor before it is de-noised and
truncated to `CLEANED_STACK_MAX_CHARS`. The redaction regex is linear, but a pathological
multi-hundred-KB sandbox blob (app-controlled stdout) must never dominate a redaction pass that
runs synchronously on the control-plane event loop — this is the belt to the regex's suspenders
([[sandbox-supervisor-child-env-scrub-allowlist]]: sandbox output is untrusted)."""

MAX_OUTPUT_TOKENS = 64_000
"""Per-model-step output clamp (mirrors the chat relay's `_MAX_OUTPUT_TOKENS`)."""

TEMPERATURE = 0.0
"""Deterministic generation — a build task wants the same edit for the same diagnostic."""

CACHE_TTL: Literal["1h"] = "1h"
"""TTL for every Anthropic prompt-cache breakpoint the loop sets (`anthropic_cache_instructions`,
`anthropic_cache_tool_definitions`, `anthropic_cache`) — the 1-HOUR tier, deliberately NOT the
5-minute default that `True` would select. Lives HERE, not in `config.py`: caching is a property of
how THIS loop is shaped, not a per-deployment knob (rule §5.9).

Why NOT 5m: a breakpoint only pays off if the NEXT step reads the entry before it expires, and this
loop's steps are anything but tightly spaced. A single model-driven `run_command` may burn up to
`RUN_COMMAND_SLOW_TIMEOUT_S` (600s) on its own — a cold-base `npm install` routinely does — and
between
runs the harness adds an `EXEC_TIMEOUT_S`-bounded `tsc` plus up to `READINESS_MAX_POLLS` readiness
polls. Any ONE of those can outlive a 5-minute entry, which would make every step pay the
cache-WRITE premium and read nothing back: a net cost INCREASE over not caching at all.

Why 1h is the safe pick: a 1h write costs ~2× base input (vs ~1.25× for 5m) but reads at ~0.1×, and
the whole build is bounded by `RUN_WALL_CLOCK_DEADLINE_S` (1800s / 30 min) — so every step of a
single build lands inside ONE 1-hour window: one write, then reads for the rest of the run."""

# --- the read ignore set (KD-10) ---------------------------------------------

READ_IGNORE_SEGMENTS: frozenset[str] = frozenset({"node_modules", ".next", "dist", ".git"})
"""Path segments the model never views — bounds context, not a threat surface (KD-10)."""

READ_IGNORE_FILES: frozenset[str] = frozenset({"package-lock.json", "pnpm-lock.yaml", "yarn.lock"})
"""Lockfiles the model never views (huge, no build-relevant signal)."""


def _normalize_rel(path: str) -> str | None:
    """Normalize a tool-supplied path to a workspace-relative POSIX path, or `None` when it is
    unusable as a workspace-relative write target: empty, NUL-bearing, absolute, or escaping the
    workspace via `..`. Fail-closed — a `None` result denies the write."""
    if not path or "\x00" in path or path.startswith("/"):
        return None
    normalized = posixpath.normpath(path)
    # normpath collapses `a/../b` → `b` and `./a` → `a`; anything that still escapes upward or is
    # the bare cwd is not a writable target.
    if normalized == "." or normalized == ".." or normalized.startswith("../"):
        return None
    if normalized.startswith("/"):  # normpath can't reintroduce a root, but deny fail-closed
        return None
    return normalized


def is_write_allowed(path: str) -> bool:
    """Fail-closed write gate for `write_file` / `edit_file` / `insert_lines` (KD-9), applied
    BEFORE any `files()` call. The open-sandbox model makes the whole workspace editable, so the
    only denials are: an unusable path (absolute / `..` escape → `_normalize_rel` returns `None`)
    and anything under `.git/` (snapshot-history integrity). The `.git` check is exact-dir +
    slash-prefix so legitimate dotfiles like `.gitignore` stay writable. This is defense-in-depth,
    not the real boundary — `run_command` can write anywhere, so the supervisor workspace-escape
    guard + demoted `appuser` are the actual containment (R6)."""
    rel = _normalize_rel(path)
    if rel is None:
        return False
    if rel == ".git" or rel.startswith(".git/"):
        return False
    return True


def is_read_ignored(path: str) -> bool:
    """True when the model may not `read_file` this path (KD-10): a build-irrelevant heavy tree
    (`node_modules`, `.next`, `dist`, `.git`), a lockfile, or an unusable path. A denylist is
    safe here for the CONTEXT bound — a read cannot mutate — but the path is still normalized
    through the SAME fail-closed `_normalize_rel` as the write guard, so an absolute path
    (`/proc/self/environ`, `/workspace/.env`) or a `..` escape is denied rather than stripped to a
    readable relative path. C1 is the real workspace-escape boundary; this stays symmetric with
    `is_write_allowed` so the two guards can't be assumed to differ."""
    rel = _normalize_rel(path)
    if rel is None:
        return True
    if any(segment in READ_IGNORE_SEGMENTS for segment in rel.split("/")):
        return True
    return posixpath.basename(rel) in READ_IGNORE_FILES
