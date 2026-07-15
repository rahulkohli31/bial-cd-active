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

# --- self-heal + model budgets (KD-7) ----------------------------------------

SELF_HEAL_MAX_RETRIES = 3
"""Flat repair-run budget (the brief's "~3 retries then escalate"). A red harness verify burns
one; at exhaustion BRAIN escalates → `ended(failed)`. OSS-validated 3-strike default (Aider
`max_reflections`, Roo `DEFAULT_CONSECUTIVE_MISTAKE_LIMIT`)."""

MODEL_TURN_CEILING = 50
"""`UsageLimits.request_limit` per `agent.iter` run — bounds a single run's model requests so a
within-run tool-call loop can't run away (a breach raises `UsageLimitExceeded` → escalation).
Distinct from the daily token quota (per-user, DB) and the self-heal budget (KD-7)."""

# --- harness-driven commands + timeouts (KD-4 / KD-8) ------------------------
# The model has NO exec tool; these are run by the harness between runs. There is deliberately
# no model-facing command allowlist — the surface does not exist.

TYPECHECK_CMD: tuple[str, ...] = ("npx", "tsc", "--noEmit")
"""The only reliable type signal: Next 16 + Turbopack HMR does not fail the dev server on type
errors, so `tsc --noEmit` is what the harness reads between runs (KD-8). `next build` is NOT run
in Wave-1 — the production build is a DEPLOY-track concern (Decision D2 / KD-6)."""

EXEC_TIMEOUT_S = 300
"""Wall-clock cap for a harness-driven command run (well under C1's 900s hard cap)."""

READINESS_MAX_POLLS = 30
"""How many `dev_status` polls the verify step waits for a slow-but-healthy dev server to report
`ready` before concluding it is not up (open-Q F — a readiness poll is not a repair run)."""

READINESS_POLL_S = 1.0
"""Sleep between readiness polls. A construction-time knob on the orchestrator so tests can drive
it to 0."""

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
