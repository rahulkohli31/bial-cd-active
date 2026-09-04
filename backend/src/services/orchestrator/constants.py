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
from typing import Final, Literal

from anthropic.types.beta import BetaThinkingConfigParam
from pydantic_ai.profiles.anthropic import AnthropicEffort

# --- self-heal + model budgets (KD-7) ----------------------------------------

SELF_HEAL_MAX_RETRIES = 3
"""Flat repair-run budget (the brief's "~3 retries then escalate"). A red harness verify burns
one; at exhaustion BRAIN escalates → `ended(failed)`. OSS-validated 3-strike default (Aider
`max_reflections`, Roo `DEFAULT_CONSECUTIVE_MISTAKE_LIMIT`)."""

MODEL_TURN_CEILING = 50
"""`UsageLimits.request_limit` per `agent.iter` run — bounds a single run's model requests so a
within-run tool-call loop can't run away (a breach raises `UsageLimitExceeded` → escalation).
Distinct from the daily token quota (per-user, DB) and the self-heal budget (KD-7)."""

RUN_TOKEN_BUDGET = 750_000
"""Hard SPEND ceiling on a single build turn, the third of three bounds on one loop (R91).

WHY A THIRD ONE. `MODEL_TURN_CEILING` bounds requests and `RUN_WALL_CLOCK_DEADLINE_S` bounds
elapsed time, and a build can stay inside both while spending a fortune: fifty requests carrying
a large context are cheap in count and in seconds and expensive in tokens. The citizen can see
the meter and the agent cannot, so the platform is the only party that can hold this line —
which is exactly why it is a number here and not a sentence in a prompt.

MEASURED COST-WEIGHTED, THROUGH `usage/gate.py`'s `weighted_spend` — the same weighting the
citizen's daily meter uses, so the two ceilings measure the same thing. This loop re-sends the
same instructions and tool definitions verbatim on every step behind a cache breakpoint, and
counting those reads at face value would make the bound a function of how many steps a build
took rather than of how much work it did, punishing the caching that makes long builds
affordable.

NOT `RunUsage.total_tokens`, AND AN EARLIER VERSION OF THIS BOUND GOT THAT WRONG. That property
is `input_tokens + output_tokens`, and under pydantic-ai `input_tokens` is the grand-total
prompt size with the cache buckets already inside it — 10 fresh tokens plus a 90k cache read
arrive as `input_tokens == 90_010`. Reading it raw is the mistake `billable_spend` records as a
2026-07-30 production incident, where one calculator build booked 956k of a 1M daily cap on 68
tokens of real fresh input.

SIZED AGAINST A REAL TRACE, not chosen for roundness: the 2026-08-18 demo build spent ~938k
tokens, of which about 65% was rework after an in-place container reset. 750k leaves room for a
substantial legitimate build and stops the runaway shape that trace showed. Like the wall clock
above, it is a safety net rather than a tuned SLA — TUNE it against real build telemetry.

IT ENDS THE TURN WHERE THE APP WORKS, which is what makes it usable at all. The piece-at-a-time
ordering (`FIRST_SLICE_RULE`) is what buys that: stopping happens at a piece boundary rather
than mid-file, and the ending names what was agreed and not built."""

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

WORKSPACE_NOTE_MAX_POLLS = 5
"""How long the per-turn workspace note (U8/R14) waits for the dev server before answering.

MUCH SHORTER THAN `READINESS_MAX_POLLS`, because it is a different question asked at a different
moment. The verify budget decides whether a build may claim it finished and can afford to wait 30
seconds for a slow app; this one runs at the START of every turn in BOTH chat kinds — including a
one-line Plan question, which is the cheapest turn the platform serves and the one this budget is
sized against — and only has to tell the model what the user is looking at.

Its whole safety comes from the third answer: a budget that runs out here is `STILL_TRYING`, which
the note reports as "could not tell", never as "the app is down". So the cost of choosing five is
a vaguer note on a cold container, not a false one. Five covers the measured 5-7s first-route
compile often enough to be worth having, and small enough that nobody notices it on a warm
one."""

CRASH_EDGE_CONSECUTIVE_POLLS = 3
"""How many CONSECUTIVE `(ready=False, running=False)` polls a preview watcher needs before it
calls a dev-process crash. BOTH `_watch_preview` implementations (`orchestrator/harness.py` and
`turns/engine.py`) must read this — they emit the same signal to the same pane, and a debounce
applied to one of them only means the crash edge depends on which code path built the app.

A SINGLE negative is not evidence. `/dev/status` answers from a bounded wait on an in-flight
probe (`_STATUS_PROBE_WAIT`, 2s) while a real cold root render against a per-project Postgres can
legitimately take longer, and a negative is deliberately never cached — so a healthy-but-slow app
answers "not ready" for as long as the render takes. Paired with `running: false`, which is the
NORMAL state for a dev server the agent started itself through the open-sandbox `run_command`
surface, one negative read as a crash re-mounts the citizen's iframe under them. A crash, by
contrast, is PERSISTENT: a dead process does not answer the next poll either. So the cost of
requiring three is ~3s of latency on reporting a real crash; the benefit is that any single
affirmative inside the window — and the supervisor caches one for `_READY_CACHE_TTL` once the
render lands — resets the count.

Not a cure, and worth stating rather than discovering later: a render slower than three polls
still trips the edge. Three is sized to cover the probe-wait window plus the poll that follows
it, which is the flap actually observed, not to outlast an arbitrarily slow route."""

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

VERIFY_INDETERMINATE_RETRIES = 2
"""How many extra verify passes an INDETERMINATE verdict buys before the loop treats it as red.

DISTINCT FROM `VERIFY_TRANSIENT_RETRIES`, which retries ONE sandbox call that raised. This retries
the WHOLE verdict, and it exists because the two ways a check can come back empty had been one
state: a readiness poll whose budget ran out and a serving probe that timed out are both "we could
not tell", and feeding either to the model as a defect spends a repair run — the user's tokens and
their time — chasing a fault that may not exist. A build that answers on the second look was never
broken.

Two, not more, and the reason is the citizen rather than the arithmetic: each pass costs a `tsc`
and up to a full readiness budget, and a third would push the honest-failure ending far enough out
that the user is watching a progress state instead of reading a sentence. At exhaustion the verdict
falls through to the red path — exactly today's behaviour — so this can only ever ADD patience,
never remove an ending."""

VERIFY_INDETERMINATE_BACKOFF_S = 3.0
"""Sleep before re-asking an INDETERMINATE verdict.

Longer than `VERIFY_RETRY_BACKOFF_S` because the thing being waited out is different: that one
waits out a network blip, this one waits for an app that is slow to come up to finish coming up.
Re-asking instantly would just spend the second look on the same half-started server."""

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

RUN_COMMAND_SUMMARY_MAX_CHARS = 4_000
"""Truncation cap on a SUCCEEDING command's output (U22 / R28 / ASM13). Summarise a success, dump
a failure: on the success path the shape of the outcome is already known — an install finished, a
lint run was clean — so a quarter of the failure budget is enough, and the rest of the context
window is worth more to the build than a clean `npm install`'s package list. On the FAILURE path
the payload is unknown by construction (that is what makes it a failure), so it keeps
`RUN_COMMAND_OUTPUT_MAX_CHARS`. Either way the cut keeps BOTH ENDS and hands back a handle to the
middle, so "summarise" never means "lose the error"."""


def output_budget_for_exit(exit_code: int) -> int:
    """The character budget a command's output is rendered under, given its exit code (ASM13).

    THE RULE IS THE REPO'S OWN, from `docs/solutions/best-practices/never-truncate-failure-output`:
    summarise a success, dump a failure. It lives here rather than in the two mirrored renderers so
    the pair cannot drift on the one number that decides how much of a failure survives."""
    return RUN_COMMAND_OUTPUT_MAX_CHARS if exit_code != 0 else RUN_COMMAND_SUMMARY_MAX_CHARS


OUTPUT_SLICE_HANDLES_PER_TURN = 8
"""How many truncated outputs a turn holds for `fetch_output_slice` at once (U22). The ring is
per-`SandboxSession` and dies with it; each entry is already bounded by `REDACT_INPUT_MAX_CHARS`,
so this is the second half of a hard ceiling — 8 x 32k = 256KB of retained text per live turn, and
the oldest handle is evicted rather than the newest refused."""

REPEATED_COMMAND_MEMORY = 256
"""How many distinct commands one turn remembers for the repeat-run counter (U22). A ceiling, not
a budget: past it new commands stop being recorded, so a repeat can go uncounted rather than a set
growing without bound. It understates a metric; it never changes what the model may run."""

OUTPUT_SLICE_MAX_LINES = 400
"""Cap on one `fetch_output_slice` window (mirrors `VIEW_MAX_LINES`): the slice tool exists to
recover a REGION, not to re-inject the whole capture the cap just removed."""

REDACT_INPUT_MAX_CHARS = 32_000
"""Hard cap on the RAW diagnostic length fed to the secret-redactor before it is de-noised and
truncated to `CLEANED_STACK_MAX_CHARS`. The redaction regex is linear, but a pathological
multi-hundred-KB sandbox blob (app-controlled stdout) must never dominate a redaction pass that
runs synchronously on the control-plane event loop — this is the belt to the regex's suspenders
([[sandbox-supervisor-child-env-scrub-allowlist]]: sandbox output is untrusted)."""

MAX_OUTPUT_TOKENS = 64_000
"""Per-model-step output clamp."""

TEMPERATURE = 0.0
"""Deterministic generation — a build task wants the same edit for the same diagnostic."""

ADAPTIVE_THINKING: Final[BetaThinkingConfigParam] = {"type": "adaptive"}
"""How reasoning is asked for, and it is not a token budget.

The deployed model REFUSES a numeric budget outright: its provider profile disallows budget
thinking, and the library raises before the request rather than letting the provider return a
400, directing callers to adaptive thinking plus an effort level. So the two knobs are this and
the effort below — a shape the owner's ruling ("medium for planning, high for building") maps
onto directly, rather than two token counts nobody could defend.

Asserted against the REAL provider model in test, never a double: the refusal lives in
`AnthropicModel.prepare_request`, which a stub never executes, so a test that trusted a fake
would go green on a combination the live gateway rejects."""

PLAN_EFFORT: Final[AnthropicEffort] = "medium"
"""How hard the model thinks in a planning turn (owner's ruling)."""

BUILD_EFFORT: Final[AnthropicEffort] = "high"
"""How hard the model thinks in a build turn (owner's ruling).

Higher than planning because a build is where the thinking is spent on something that has to
work: the model is reading real files, choosing an edit, and answering a compiler. A plan is a
conversation about what to build, and the person is still in it."""

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
