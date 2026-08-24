"""The eight sandbox tools — the model's ENTIRE action surface (KD-4 / KD-5 / KD-9 / KD-10 / R1).

Five file tools go through the C2 `files()` op; `run_command` runs a general shell command over
the C2 `exec` transport — the vibe-coding pivot, so the model can `npm install`, run linters, and
drive its own build (R1). `run_command`'s containment is NOT a tool-level allowlist (it can write
anywhere the workspace allows): it is the demoted `appuser`, the supervisor's fail-closed
child-env, and secret-redacted + length-capped output (R3/R13) — plus the destructive-SQL
sentinel (`sql_guard.you_shall_not_pass`, U1/#12) that refuses improvised DML/DDL against the
app's real database BEFORE the transport ever sees it. A non-zero command exit is a
NORMAL return the model reads and fixes; a transport failure (incl. an install-timeout 504) is
converted to a `ModelRetry` so the loop self-heals rather than hard-crashing (R11) — only a
`SandboxGoneError` escalates. The three file mutators share one fail-closed write guard
(absolute/`..` escape + `.git/` deny) applied BEFORE any `files()` call (KD-9). A `str_replace`
that fails to match exactly once is enriched into a `ModelRetry` so the model self-corrects in-run
(KD-5). Reads are bounded to `VIEW_MAX_LINES` and refuse the ignore set (KD-10). No tool ever
renders `session.handle` / `handle.token` into a result or an error (KD-9 secret-safety).

`fetch_output_slice` (U22/R28) is the seventh, and it exists because the output cap used to be
HEAD-ONLY: a truncated failure lost the assertion at the bottom, and recovering the middle cost a
re-run. Output is now cut to head AND tail under an exit-code-conditional budget, and the notice
between them names a handle the model reads the elided middle back through. The buffer behind
that handle holds `scrub_untrusted` output and NOTHING ELSE, lives on `SandboxSession` (so the
harness's per-run reset is its whole lifetime), never reaches the database or blob, and answers an
unknown handle with a plain re-run instruction rather than a `ModelRetry` — which would spend the
round-trip the tool exists to save.

`apply_schema_change` (U23/R29) is the eighth, and it is the one tool here that is a SEQUENCE
rather than an action: `drizzle-kit generate` then `npm run db:migrate`, the pair the prompt used
to dictate step by step. It exists because both of them can fail while exiting zero, so a model
reading exit codes believes a schema change happened that did not — it reads what they PRINTED,
reports a per-step outcome, refuses to call a run successful when any step failed, and says which
step failed and what state that left the workspace and the database in.

EVERY TOOL DOCSTRING BELOW IS PROMPT COPY (U20 / R26). pydantic-ai sends it to the model as the
tool's description at registration, and since U20 the build prompt's `TOOL SURFACE` block is
GENERATED from these same strings (`agent/toolsets.render_tool_surface`) — so a docstring edit
here is a prompt edit, and `test_prompt.py`'s drift check goes red until the snapshot in
`core/prompt_blocks.WRITE_TOOL_SURFACE` is regenerated. Write the FIRST SENTENCE as the line you
want in the prompt; the rest is detail the model reads on the tool schema. Two sentences in
particular are load-bearing beyond their own tool and must survive any trim: `run_command`'s
don't-start-or-restart-the-dev-server rule (the only thing covering a second `next dev` started
through `/exec`, which the supervisor's child env cannot tell from the real one) and
`declare_done`'s terminal-on-a-passing-check statement (U18 — a model promised a follow-up
round-trip withholds its closing message from `summary`, and there is no reply to put it in).

They are built as a `FunctionToolset` FACTORY over a `sandbox_of` accessor — mirroring
`agent/read_tools.read_only_toolset` — rather than `@build_agent.tool` decorators. ONE tool body,
two consumers: the legacy `/build-sessions` harness (`BuildDeps.sandbox`) and a Write chat turn.
The accessor closure is the only thing that knows the run's deps type.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final, Literal, cast

from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.toolsets.function import FunctionToolset

from src.core.prompt_blocks import APPLY_SCHEMA_CHANGE_TOOL
from src.core.redaction import (
    cut_before_an_open_credential,
    leaves_a_credential_value_open,
    scrub_untrusted,
)
from src.db.models.harness_counter import HarnessCounter
from src.services.messages.projection import (
    classify_command,
    classify_file_step,
    classify_tool_call,
    command_needs_the_long_timeout,
    command_only_inspects,
)
from src.services.orchestrator.constants import (
    OUTPUT_SLICE_MAX_LINES,
    REDACT_INPUT_MAX_CHARS,
    RUN_COMMAND_DEFAULT_TIMEOUT_S,
    RUN_COMMAND_OUTPUT_MAX_CHARS,
    RUN_COMMAND_SLOW_TIMEOUT_S,
    VIEW_MAX_LINES,
    is_read_ignored,
    is_write_allowed,
    output_budget_for_exit,
)
from src.services.orchestrator.deps import HeldOutput, SandboxSession
from src.services.orchestrator.errors import redact_secrets
from src.services.orchestrator.sql_guard import you_shall_not_pass
from src.services.sandbox import (
    ExecResult,
    FileCreate,
    FileInsert,
    FileStrReplace,
    FileView,
    SandboxError,
    SandboxGoneError,
)

# ---------------------------------------------------------------------------------------
# U22 / R28: output the model can act on — head AND tail, and a handle to the middle
# ---------------------------------------------------------------------------------------
#
# MIRRORED, DELIBERATELY, in `agent/read_tools.py` (`_is_predictable_noise` / `_redacted_lines` /
# `_render_output` / `_cap_redact_cap`). The two toolsets are separate surfaces and the read
# surface adds NO runtime edge into this package — both module docstrings say so — which is why
# this is copied rather than imported. What keeps a copy from drifting into a different policy is
# `tests/services/agent/test_read_tools.py::test_the_two_mirrored_output_caps_behave_identically`,
# one table-driven comparison run against BOTH functions.
#
# THE ONE PLACE THE TWO CALLERS DIFFER, stated here so it is not mistaken for drift: the sandbox
# `run_command` renders under `output_budget_for_exit` (summarise a success, dump a failure) and
# passes a HANDLE, because Write is the only mode that runs builds and the only mode the slice
# tool is registered in. The read-mode caller always passes the full dump budget and no handle —
# in Ask/Plan the successful output IS the answer the model asked for, and there is no
# `fetch_output_slice` there to recover an elided middle with.

_NOISE_KEEP_TOKENS: Final = (
    "deprecat",
    "vulnerab",
    "audit",
    "severity",
    "advisor",
    "cve-",
    "security",
)
"""WHERE THE NOISE BOUNDARY IS DRAWN, and the conservative half of it. A line carrying any of
these is NEVER dropped, whatever else it looks like — `npm WARN deprecated x@1: use y`, an audit
summary, a severity table, a CVE reference. R28 asks for predictable noise to go and for genuine
vulnerability or deprecation signal to stay, and this is the second half stated as a rule instead
of trusted to the patterns below being narrow enough."""

_NOISE_PATTERNS: Final = (
    re.compile(r"^npm notice"),
    re.compile(r"^\s*\d+ packages? (?:are|is) looking for funding"),
    re.compile(r"^\s*run `npm fund` for details"),
    re.compile(r"^Progress: resolved \d+, reused \d+"),
    re.compile(r"^\s*[\u2800-\u28ff]"),
    re.compile(r"^\s*\[[#_=\-]+\]\s*$"),
)
"""The predictable noise, and NOTHING WIDER. Every pattern here is a SOLICITATION or a PROGRESS
FRAME: an upgrade notice for npm itself, a funding request, a pnpm resolution counter, a braille
spinner frame, an ASCII progress bar. None of them is actionable by a model building an app, and
all of them recur on every install.

WHAT IS DELIBERATELY NOT HERE, since the plan left the boundary to implementation and being wrong
in this direction is the expensive one: driver and runtime deprecation warnings
(`(node:1) [DEP0040] DeprecationWarning: …`) stay. They read like noise because they recur, but
telling the deprecation a model can act on (a package IT just installed) from one it cannot needs
to know which dependency the line came from, and the line does not carry it. Dropping the class
would mean the platform silently deciding a citizen's app may keep a deprecated dependency.
Conservative rule: when in doubt, keep the line.

Every pattern is anchored and quantifier-flat (the repo's ReDoS constraint)."""


def _is_predictable_noise(line: str) -> bool:
    """Is this line recurring dependency-manager chatter with nothing in it for the model?"""
    lowered = line.lower()
    if any(token in lowered for token in _NOISE_KEEP_TOKENS):
        return False
    return any(pattern.search(line) for pattern in _NOISE_PATTERNS)


_WITHHELD_TAIL_NOTICE = (
    "[... the end of this output was withheld: the part that was dropped opens a credential value "
    "that never closes, so the remaining text may be the inside of one and cannot be masked "
    "safely. Re-run a narrower command to see it ...]"
)
"""Why the tail is missing, said to the model in terms it can act on.

Withholding rather than masking, because there is nothing here to mask AGAINST: the key that
identifies the value is in the text that was dropped. Guessing would either leak (mask nothing) or
destroy the tail of every build log that happens to contain a quote (mask everything)."""


_WITHHELD_HEAD_NOTICE = (
    "[... the rest of this output was withheld: a credential value opens above and never closes "
    "in what was captured, so everything after it may be the inside of one and cannot be masked "
    "safely. Re-run a narrower command to see it ...]"
)
"""The SAME rule at the other end of the capture, said the same way.

The redactor's quoted arms need their CLOSING delimiter, so a value that opens inside the head and
closes past it matches nothing at all and renders raw — the head was never "masked with its key
present", it was simply unmatched. Cut at the opener and say so, exactly as the tail does."""


def _capture_limit_marker(dropped: int) -> str:
    """What a capture too big to scan lost, said out loud. NOT recoverable through a handle —
    this text was never captured, so the marker names the only remedy there is."""
    return (
        f"[... {dropped:,} characters dropped at capture — this command printed more than the "
        f"{REDACT_INPUT_MAX_CHARS:,}-character limit, so only its first and last "
        f"{REDACT_INPUT_MAX_CHARS // 2:,} characters were read. No handle holds the rest; "
        "re-run a narrower command to see it ...]"
    )


def _within_the_capture_limit(text: str) -> tuple[str, str, int]:
    """The head and the tail of a raw capture, cut ON LINE BOUNDARIES — and what that cost.

    TWO PROPERTIES, and the older single `text[:cap]` slice got both wrong.

    SECURITY: the redactor is never handed half a line. A credential is a shape on ONE line —
    `_URL_CRED_RE` needs the `@` that follows the password, the assignment families need their
    terminator — so a cut landing inside one leaves a fragment that matches nothing and is
    egressed in the clear. Whole lines in, whole lines out: a line is scanned entire or dropped
    entire, and a single line longer than the window is therefore dropped rather than truncated.

    THAT IS NOT THE WHOLE OF IT, and this docstring used to claim it was ("head-only hid the
    fragment; only the retained tail is new exposure"). It is false for the quoted arms, which
    span newlines on purpose: a value that OPENS in the head and closes past the cut is not
    masked-with-its-key-present, it matches nothing at all and renders verbatim — a line-boundary
    cut does not help, because the value legitimately contains the newlines it is cut on. Both
    ends of this cut therefore go through the same guard in `_redacted_lines`, one withholding
    the tail and one cutting the head back to the credential's own line.

    TRUTHFULNESS: a head-only cap silently deleted the END of every capture over the limit —
    which is where `Failed to compile.` and the failing assertion live — and the notice below
    then reported the surviving line count as the total. The tail is cut here so the tail is
    what the model reads.

    The two halves together stay inside `REDACT_INPUT_MAX_CHARS`, so the synchronous redaction
    scan is bounded exactly as it was (the ReDoS guard)."""
    if len(text) <= REDACT_INPUT_MAX_CHARS:
        return text, "", 0
    half = REDACT_INPUT_MAX_CHARS // 2
    # `rpartition`/`partition` answer "" for a window with no newline in it at all, which IS the
    # rule for a single over-long line: drop it rather than feed the redactor its prefix.
    head = text[:half].rpartition("\n")[0]
    tail = text[len(text) - half :].partition("\n")[2]
    return head, tail, len(text) - len(head) - len(tail)


def _scrubbed_lines(text: str, *, denoise: bool) -> list[str]:
    """Scrub one whole-line chunk and split it, dropping dependency-manager chatter if asked."""
    if not text:
        return []
    scrubbed = scrub_untrusted(text, limit=REDACT_INPUT_MAX_CHARS)
    lines = scrubbed.splitlines()
    return [line for line in lines if not (denoise and _is_predictable_noise(line))]


def _redacted_lines(text: str, *, denoise: bool) -> list[str]:
    """The SAFE artifact, and the ONLY thing that is ever buffered or returned (U22 / R3).

    `scrub_untrusted` is cap → de-escape → redact, in that order, on the raw capture: the cap
    bounds the work an app-controlled blob can make a synchronous scan do (ReDoS guard), the
    escape strip runs BEFORE the mask because an ANSI sequence spliced into a credential splits
    the token and the pattern stops matching, and the mask is what makes the text egressable at
    all. It replaces the older `redact_secrets(text[:cap])` here because a handle RETAINS this
    string for the rest of the turn — `core/redaction.scrub_untrusted`'s own docstring names
    reaching for `redact_secrets` alone on app-authored text as the mistake it exists to stop.

    REDACTION HAPPENS HERE, ONCE, ON EVERY CHARACTER THAT SURVIVES CAPTURE — before any slicing,
    before the buffer, before the head/tail cut. That ordering is the unit's security property
    twice over: the elided middle a handle hands back was never read by a human, so a buffer
    built from raw stdout would be a direct path to a secret nobody ever saw; and cutting first
    would split a credential that straddles the cut into two fragments that no longer match the
    redactor's shapes, which is exactly how a cap applied after redaction re-exposes one. The
    capture cut above is the same rule applied one level up, which is why it cuts on lines.

    `denoise` is the CALLER's answer to "is this text a build log?", never a guess made from the
    text itself. Dropping a line is only ever right for dependency-manager chatter; the same
    filter run over `cat`'s output silently deletes a line from a file the model asked to read,
    and an `edit_file` composed from that read then fails to match with no visible cause."""
    head, tail, dropped = _within_the_capture_limit(text)
    # THE HEAD IS CUT WHEN IT ENDS INSIDE A CREDENTIAL — the same rule as the tail guard below,
    # at the other end of the same cut, and it is NOT covered by masking the head. The redactor's
    # quoted arms need their closing delimiter and its bare arm excludes quote characters, so a
    # value opened in the head and closed past it matches nothing and renders VERBATIM. That is
    # the whole head of a real bearer credential, in the model's context and in the persisted
    # step row. Cheap, too: the head is inside the redaction cap by construction.
    safe_head = cut_before_an_open_credential(head)
    lines = _scrubbed_lines(head if safe_head is None else safe_head, denoise=denoise)
    if safe_head is not None:
        lines.append(_WITHHELD_HEAD_NOTICE)
    if dropped:
        lines.append(_capture_limit_marker(dropped))
    # THE TAIL IS WITHHELD WHEN IT MIGHT BE A CREDENTIAL'S BODY. `_SECRET_ASSIGN_RE`'s quoted arms
    # span newlines on purpose (a PEM, a passphrase), so "a credential is a shape on one line" —
    # the assumption the line-boundary cut above was built on — is false for those arms. A tail
    # that begins part-way through such a value carries no key, matches none of the redactor's
    # shapes, and egresses in the clear; the old head-only cap never showed that text at all, so
    # retaining the tail without this check was strictly worse than not retaining it.
    # `leaves_a_credential_value_open` answers on everything BEFORE the tail (head + dropped
    # middle) and fails toward withholding.
    if tail and leaves_a_credential_value_open(text[: len(text) - len(tail)]):
        lines.append(_WITHHELD_TAIL_NOTICE)
        return lines
    lines.extend(_scrubbed_lines(tail, denoise=denoise))
    return lines


def _leading_lines_within(lines: list[str], budget: int) -> int:
    """How many leading lines fit in `budget` characters (newline separators counted)."""
    used = 0
    for index, line in enumerate(lines):
        used += len(line) + 1
        if used > budget:
            return index
    return len(lines)


def _elision_notice(
    *,
    elided_lines: int,
    elided_chars: int,
    first: int,
    last: int,
    total: int,
    cut_line: int,
    partly_shown: bool,
    handle: str | None,
) -> str:
    """The truncation notice: WHAT was removed, and — inline — how to get it back.

    NAMING THE TOOL AND THE HANDLE IN THE NOTICE ITSELF is the point. A capability described once
    in a system prompt is a thing the model has to remember at the moment it is staring at a
    truncated log; a call it can copy off the line in front of it is not.

    The line numbers inside the call are printed WITHOUT thousands separators while the totals
    around them keep theirs: the first pair are arguments to be copied verbatim into a tool call,
    and `start_line=1,024` is not an integer.

    `cut_line` IS PASSED RATHER THAN DERIVED (it used to be `first - 1`). The caller now names the
    elided range from the first line it did not show WHOLE, so the two numbers stopped being one
    apart — and a notice that computes a line number out of another notice's arithmetic goes wrong
    silently, one truncation shape at a time."""
    if elided_lines > 0:
        edge = " (the ends of that range are only partly shown here)" if partly_shown else ""
        what = (
            f"{elided_lines:,} lines ({elided_chars:,} characters) elided — "
            f"lines {first}-{last} of {total:,}{edge}"
        )
        how = (
            f'read them with fetch_output_slice(handle="{handle}", '
            f"start_line={first}, end_line={last})"
            if handle is not None
            else "re-run a narrower command to see them"
        )
    else:
        what = f"{elided_chars:,} characters elided from the middle of line {cut_line:,}"
        how = (
            f'read that line with fetch_output_slice(handle="{handle}", '
            f"start_line={cut_line}, end_line={cut_line})"
            if handle is not None
            else "re-run a narrower command to see them"
        )
    return f"\n[... {what}; {how} ...]\n"


def _render_output(lines: list[str], *, budget: int, handle: str | None) -> str:
    """Render redacted lines under `budget`, keeping the HEAD AND THE TAIL (ASM13).

    Head-only was the defect: a stack trace puts its message at the top and the failing assertion
    at the bottom, so a head cap loses the error and a tail cap loses the cause. The budget is
    split down the middle and the two ends are joined by a notice that says what is missing.

    NOTHING IS RE-REDACTED HERE. The input is already `_redacted_lines`' output, so this function
    only ever cuts already-masked text — which is what makes a cut safe at all."""
    joined = "\n".join(lines)
    if len(joined) <= budget:
        return joined
    head_budget = budget // 2
    tail_budget = budget - head_budget
    # At least one line in the head even when that single line is longer than the whole budget;
    # it is hard-capped below, and the `else` arm carves the tail out of the same line.
    whole_lines_in_head = _leading_lines_within(lines, head_budget)
    head_count = max(1, whole_lines_in_head)
    remaining = lines[head_count:]
    tail_count = _leading_lines_within(remaining[::-1], tail_budget)
    head_text = "\n".join(lines[:head_count])[:head_budget]
    if tail_count:
        tail_text = "\n".join(lines[len(lines) - tail_count :])[-tail_budget:]
    else:
        # NO WHOLE LINE FITS THE TAIL BUDGET — carve the tail out of the last line instead, or
        # this renders head-only for the one shape that cannot recover from it. A capture that
        # is one enormous line (a `curl` payload, a `--json` reporter) puts its answer at the
        # END exactly as a stack trace does, and `fetch_output_slice` addresses LINES: the slice
        # the notice points at came back head-first and cut in the same place, so the end of
        # that line was unreachable in any number of calls.
        tail_text = lines[-1][-tail_budget:]
    # A LINE SHOWN ONLY IN PART BELONGS IN THE ELIDED RANGE, at either end of it. The head's
    # first line is hard-capped when no whole line fit (`whole_lines_in_head == 0`), and the tail
    # is carved out of the last line when no whole line fit there (`tail_count == 0`) — in both
    # cases the REST of that line is missing, and `fetch_output_slice` addresses LINES, so naming
    # it in the range is the only way the model can ever read it whole. Reporting the head's cut
    # line as fully shown (the old unconditional `head_count + 1`) left it unreachable in any
    # number of slice calls, and the mid-line arm below only rescued it when nothing else was
    # elided at all.
    notice = _elision_notice(
        elided_lines=len(lines) - head_count - tail_count,
        elided_chars=len(joined) - len(head_text) - len(tail_text),
        first=head_count + 1 if whole_lines_in_head else head_count,
        last=len(lines) - tail_count,
        total=len(lines),
        cut_line=head_count,
        partly_shown=not whole_lines_in_head or not tail_count,
        handle=handle,
    )
    return head_text + notice + tail_text


def _redact_command_output(
    text: str, *, budget: int, handle: str | None = None, denoise: bool = True
) -> str:
    """Raw capture → the model-facing artifact: cap → de-escape → redact → de-noise → head+tail.

    The MIRROR of `agent/read_tools._cap_redact_cap` (which has no `denoise` arm at all — see the
    block comment there); the shared table-driven test pins them identical. `run_command` is the
    first tool to egress captured stdout, so this is a first-class secret-safety surface, not a
    diagnostic afterthought."""
    return _render_output(_redacted_lines(text, denoise=denoise), budget=budget, handle=handle)


def _new_output_handle() -> str:
    """A short, model-typeable name for one held capture. Random rather than sequential so a
    handle from a previous run cannot be guessed into a collision with a live one — it is not a
    secret (it names redacted text), and it is not a UUID either (ADR-0006: a raw UUID is never
    the thing a caller quotes)."""
    return f"out_{secrets.token_hex(4)}"


OUTPUT_NO_LONGER_HELD: Final = "That output is no longer held — re-run the command."
"""The answer to an unknown or expired handle: A PLAIN INSTRUCTION, NOT AN EXCEPTION. A
`ModelRetry` here would cost the round-trip this whole unit exists to save, and there is nothing
for the model to self-correct — the buffer is gone because the turn moved on, which is the
documented lifetime, not a mistake it made."""


async def _count_at_the_tool_boundary(counter: HarnessCounter, session: SandboxSession) -> None:
    """Record one adoption counter for this build (U22 / U25's surface).

    Imported INSIDE the function on purpose: `services.build_sessions.__init__` reaches this
    module through the session manager, so a module-level import closes a real cycle. Same shape
    `usage/gate.py` uses, and for the same reason. `count` owns its own session and swallows
    everything, so this can never fail the tool call it is measuring."""
    from src.services.build_sessions.counters import count

    await count(counter, app_id=session.app_id)


async def _format_command_result(
    session: SandboxSession,
    result: ExecResult,
    *,
    command: str,
    denoise: bool,
    budget: int | None = None,
) -> str:
    """Render an `ExecResult` for the model: the exit code plus redacted stdout/stderr, capped by
    what the exit code says the output is WORTH (ASM13) — a success is summarised, a failure is
    dumped. Empty streams are omitted so a clean run reads tersely.

    A stream that does not fit its budget is HELD under its own handle before it is cut, so the
    notice the model reads names something that actually resolves. A stream that fits is not held
    at all: there is nothing to recover, and the ring is worth more to the next truncation.

    `budget` OVERRIDES the exit code's own answer, and exists for exactly one caller: U23's
    composite, whose whole point is that the underlying exit code lies. A step that exited 0 after
    failing must be DUMPED, not summarised — sizing its output from `result.exit` would let the
    misleading zero decide how much of the failure the model gets to read."""
    budget = output_budget_for_exit(result.exit) if budget is None else budget
    sections = [f"exit code: {result.exit}"]
    for stream, raw in (("stdout", result.stdout), ("stderr", result.stderr)):
        lines = _redacted_lines(raw, denoise=denoise)
        if not any(line.strip() for line in lines):
            continue
        handle: str | None = None
        if len("\n".join(lines)) > budget:
            handle = _new_output_handle()
            session.hold_output(handle, HeldOutput(command=command, lines=tuple(lines)))
            await _count_at_the_tool_boundary(HarnessCounter.OUTPUT_TRUNCATED, session)
        rendered = _render_output(lines, budget=budget, handle=handle).strip()
        sections.append(f"{stream}:\n{rendered}")
    return "\n\n".join(sections)


# ---------------------------------------------------------------------------------------
# U23 / R29: one operation for applying a database change, and a status that tells the truth
# ---------------------------------------------------------------------------------------
#
# THE SHAPE R29 NAMES, MET TWICE IN ONE SEQUENCE. Applying a schema change used to be two
# prompt-taught commands, and BOTH of them can fail while exiting zero:
#
#   1. `npx drizzle-kit generate --name <what>` reaches the RENAME RESOLVER whenever a diff is
#      ambiguous ("is `label` created, or renamed from `title`?"). No CLI flag answers it —
#      `--name` least of all, it names the output file — and under this sandbox's real conditions
#      (`stdin=DEVNULL`, no TTY, `CI=1`) it does not even hang: drizzle-kit prints "Interactive
#      prompts require a TTY terminal" to stderr, writes NO migration, and exits 0.
#   2. `npm run db:migrate` runs `sandbox/template/scripts/db-migrate.mjs`, which is NON-FATAL BY
#      DESIGN — its own file header explains why: a migrate step that fails hard means `next dev`
#      never prints "Ready in", and the harness then reports a rendering fault that does not
#      exist. So it catches every error, abandons a slow migration after 20s, and ALWAYS exits 0.
#
# The composite therefore does not merely save a round trip. It is the only thing in the loop that
# reads what those commands PRINTED and reports the failure their exit codes hide — per step,
# naming which step failed and what state that left the workspace and the database in.
#
# WHAT IT DOES NOT DO is prevent the rename resolver. The one-kind-of-change-per-call rule in the
# DATABASE block is the only thing that does, and the TTY defences (`CI=1`,
# `stdin=subprocess.DEVNULL`, and `_refuse_a_manufactured_tty`, all in `sandbox/supervisor/app.py`)
# are what make reaching it FAST AND LOUD rather than the observed four-minute stall. All three
# stay; trimming any of them turns this tool's cleanest failure back into a wedge.

_GENERATE_ARGV: Final = ("npx", "drizzle-kit", "generate", "--name")
_MIGRATE_ARGV: Final = ("npm", "run", "db:migrate")
"""The ONLY place either command is spelled now that the prompt has stopped dictating them
(`core/prompt_blocks.APPLY_SCHEMA_CHANGE_TOOL` carries the reasoning). One spelling, one caller."""

_MIGRATION_NAME_MAX_CHARS: Final = 60
_NOT_A_NAME: Final = re.compile(r"[^a-z0-9]+")
"""Everything that is not a migration-name character, collapsed to one `_`. Linear and
quantifier-flat, like every regex in this package (the ReDoS constraint)."""

_GENERATE_FAILED_MARKERS: Final = ("interactive prompts require a tty",)
"""EXIT-ZERO FAILURE SHAPE 1 — the interactive question nobody can answer. Measured against the
template's pinned `drizzle-kit@0.31.10` under the sandbox's own conditions, which is the only
place the string matters. It is a THIRD-PARTY string, so nothing in this repo can keep it true: a
drizzle-kit bump that reworded it would make this detector silent. What the tests can do, and do,
is pin it identical to the two other places the same measurement is written down — the supervisor's
refusal note and `core/prompt_blocks.APPLY_SCHEMA_CHANGE_TOOL` — so re-measuring it lands in all
three or in none."""

_MIGRATE_FAILED_MARKERS: Final = (
    "[db] migrations failed",
    "[db] migrations still running after",
    "skipping migrations",
)
"""EXIT-ZERO FAILURE SHAPES 2 AND 3 — an error caught and swallowed, and work abandoned or never
attempted. Every one of them is `scripts/db-migrate.mjs`'s OWN wording, on a path that ends in
`process.exit(0)`: the caught-error line, the 20-second abandon timer, and the no-DSN skip (which
applies nothing at all and would otherwise read as a clean run). Pinned against the real script by
`test_the_migrate_failure_markers_match_the_script_that_prints_them` — a detector and the program
it reads are exactly the pair that drifts silently."""

_APPLIED_STATE: Final = (
    "the migration is applied — the database now matches `db/schema.ts`, and you can query the "
    "new shape."
)
"""The only success state there is: BOTH steps ran, so the workspace and the database agree."""


@dataclass(frozen=True)
class _CompositeStep:
    """One step of the composite: what it is called, what it runs, how it fails while claiming it
    did not, and what a failure leaves behind for the model to reason from."""

    #: The step's name in the report — plain words, because the model reads this, not a log.
    what: str
    argv: tuple[str, ...]
    #: Substrings that mean "this failed", scanned case-folded over the RAW capture (before
    #: redaction and de-noising, so neither can hide a marker from the detector).
    failure_markers: tuple[str, ...]
    #: What the workspace and the database are left in when THIS step fails. Not decoration: R29
    #: asks the operation to say what state it left things in, and the answer differs per step.
    state_when_failed: str


@dataclass(frozen=True)
class _StepOutcome:
    """What one step actually did. `result is None` means it never ran."""

    step: _CompositeStep
    result: ExecResult | None
    ok: bool


def _migration_name(what_changed: str) -> str:
    """A model-written description → the slug drizzle-kit names the migration file with.

    NORMALISED RATHER THAN REFUSED, deliberately. "add visitors table" is exactly what a model
    should be writing here, and bouncing it back as a `ModelRetry` would spend the round-trip this
    whole unit exists to save on a formatting quibble. The report names the command it ran, so the
    model sees the slug it got. Only an input with no usable characters at all is refused."""
    return _NOT_A_NAME.sub("_", what_changed.strip().lower()).strip("_")[
        :_MIGRATION_NAME_MAX_CHARS
    ]


def _the_two_steps(name: str) -> tuple[_CompositeStep, ...]:
    """The fixed sequence, built around one migration name."""
    return (
        _CompositeStep(
            what="generate the migration",
            argv=(*_GENERATE_ARGV, name),
            failure_markers=_GENERATE_FAILED_MARKERS,
            state_when_failed=(
                "NO migration file was written and the database was not touched, so your "
                "`db/schema.ts` edit has NOT been applied — the code and the database disagree. "
                "Nothing needs undoing; make ONE kind of schema change and call this again."
            ),
        ),
        _CompositeStep(
            what="apply the migration to the database",
            argv=_MIGRATE_ARGV,
            failure_markers=_MIGRATE_FAILED_MARKERS,
            state_when_failed=(
                "the migration file IS written under `drizzle/`, but the database did not take "
                "it: the tables still do not match `db/schema.ts`, and a query against the new "
                "shape will fail at runtime. Do NOT edit `db/schema.ts` again to work around it "
                "— read the error below, fix its cause, and call this again (the generate step "
                "finds nothing new to do and the same migration is re-applied)."
            ),
        ),
    )


def _the_command_lied(result: ExecResult, markers: tuple[str, ...]) -> bool:
    """Did this command FAIL while exiting zero? Scanned over the RAW capture, case-folded.

    Raw, not redacted: the redactor and the noise filter both rewrite lines, and a detector that
    reads their output is one masking rule away from going quietly blind. Nothing scanned here is
    ever returned — the report is built from `_redact_command_output`'s artifact, as always.

    AND UNCAPPED, DELIBERATELY, unlike every regex on this path. The capture is app-controlled and
    unbounded, so the instinct is to hand it the same head+tail window `_within_the_capture_limit`
    cuts — but that window has a MIDDLE it throws away, and a marker dropped there turns a failed
    step into a reported success. This detector fails OPEN when it misses, which is the one
    direction that matters here. The cost that would buy: `.lower()` plus a substring search is
    memchr-speed, measured at 3.7 ms over 6.4 MB (~0.6 ms/MB) — three orders of magnitude cheaper
    per byte than the credential scan whose bound this would be copying, and not worth a detector
    that can be silenced by padding."""
    printed = f"{result.stdout}\n{result.stderr}".lower()
    return any(marker in printed for marker in markers)


def _still_doing_it_the_hard_way(argv: list[str]) -> bool:
    """Is this a raw `drizzle-kit generate` — the two-step sequence driven BY HAND (U23/R29)?

    The adoption question, and the head of the sequence is what answers it. A lone
    `npm run db:migrate` is NOT counted: re-applying an existing migration is legitimate work the
    composite does not replace, and counting it would inflate the by-hand number with runs that
    were never the sequence at all."""
    joined = " ".join(argv).lower()
    return "drizzle-kit" in joined and "generate" in joined


async def _render_the_schema_change_report(
    session: SandboxSession, outcomes: list[_StepOutcome], *, budget: int
) -> str:
    """The composite's whole answer: a terminal verdict, the state it left things in, and one
    block per step — including the step that never ran, which is a per-step outcome too."""
    total = len(outcomes)
    failed = next(
        (outcome for outcome in outcomes if outcome.result is not None and not outcome.ok), None
    )
    if failed is None:
        headline = f"{APPLY_SCHEMA_CHANGE_TOOL} SUCCEEDED — all {total} steps ran."
        state = _APPLIED_STATE
    else:
        headline = (
            f"{APPLY_SCHEMA_CHANGE_TOOL} FAILED at step {outcomes.index(failed) + 1} of {total} "
            f"— {failed.step.what}."
        )
        state = failed.step.state_when_failed
    sections = [headline, f"WHAT STATE THINGS ARE IN: {state}"]
    for index, outcome in enumerate(outcomes, start=1):
        head = f"STEP {index} of {total} — {outcome.step.what}: "
        if outcome.result is None:
            sections.append(
                f"{head}NOT RUN\nstep {index - 1} failed, so this step never started and nothing "
                "it would have done has happened."
            )
            continue
        command = redact_secrets(" ".join(outcome.step.argv)[:REDACT_INPUT_MAX_CHARS])
        lines = [f"{head}{'OK' if outcome.ok else 'FAILED'}", f"command: `{command}`"]
        if not outcome.ok and outcome.result.exit == 0:
            # THE OVERRIDE, SAID OUT LOUD. The model has been taught for its whole life that a
            # zero exit means success; a verdict that silently contradicts one is a verdict it
            # will argue with. Naming the override is what makes it usable.
            lines.append(
                "it exited 0 — that exit code is WRONG, and this operation overrides it: the "
                "output below says the step failed. Read the output, not the code."
            )
        lines.append(
            await _format_command_result(
                session, outcome.result, command=command, denoise=True, budget=budget
            )
        )
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


async def _step(
    session: SandboxSession,
    *,
    name: str,
    label: str,
    state: Literal["started", "ok", "failed"],
    hidden: bool = False,
) -> None:
    """The legacy C7 build feed. `emitter is None` on the chat-turn path, where the ENGINE emits a
    StepFrame per tool call from the run's own FunctionToolCall/Result events using the same
    `classify_tool_call` label — emitting both would render every step twice."""
    if session.emitter is None:
        return
    await session.emitter.step(name=name, label=label, state=state, hidden=hidden)


def _require_writable(path: str) -> None:
    """Fail-closed write gate (KD-9). Raises `ModelRetry` — never touching `files()` — for the two
    remaining denials in the open-sandbox model: a path that escapes the workspace (absolute or
    `..`) or a write into `.git/` (snapshot-history integrity). Every other workspace-relative path
    is writable — config, `package.json`, and the data client included."""
    if not is_write_allowed(path):
        raise ModelRetry(
            f"`{path}` cannot be written: paths must stay inside the workspace (no absolute paths "
            "or `..` escapes) and `.git/` is protected to keep the snapshot history intact. Use a "
            "workspace-relative path."
        )


async def _reanchor(session: SandboxSession, path: str) -> str:
    """Build the enriched retry message for a failed exact-replace: the current file, line-
    numbered, plus guidance to add unique context or fall back to a whole-file write (KD-5)."""
    try:
        result = await session.sandbox_client.files(
            session.handle, FileView(path=path, view_range=[1, VIEW_MAX_LINES])
        )
    except SandboxGoneError:
        raise  # a gone sandbox is terminal — escalate, never re-anchor (KD-11)
    except SandboxError:
        current = "(the current file could not be read)"
    else:
        content = result.detail.get("content")
        current = content if isinstance(content, str) else "(the current file could not be read)"
    return (
        f"The exact replacement in `{path}` failed: `old_str` must match EXACTLY ONCE, but it "
        "matched zero or several times. Here is the current file, line-numbered:\n\n"
        f"{current}\n\n"
        "Retry `edit_file` with an `old_str` that includes at least 3 lines of unique surrounding "
        "context, or use `write_file` to replace the whole file."
    )


# ---------------------------------------------------------------------------------------
# The toolset factory
# ---------------------------------------------------------------------------------------


def sandbox_toolset[DepsT](
    sandbox_of: Callable[[RunContext[DepsT]], SandboxSession],
) -> FunctionToolset[DepsT]:
    """The eight sandbox tools over whatever deps `sandbox_of` resolves the session from. Generic
    on the deps type for the same reason `read_only_toolset` is: ONE tool body, two consumers (the
    legacy harness's `BuildDeps`, a Write chat turn's own deps).

    The inner tools annotate `RunContext[Any]`: pydantic-ai resolves tool annotations with
    `get_type_hints` at registration, and a PEP-695 type param of the ENCLOSING function is not in
    scope there under deferred annotations. The factory signature carries the real typing; the
    `cast` at the return is the one boundary where it narrows back.
    """

    async def read_file(
        ctx: RunContext[Any], path: str, view_range: list[int] | None = None
    ) -> str:
        """Read a file's contents (line-numbered). Optionally pass `view_range` as `[start, end]`
        (1-indexed; `end` may be -1 for end-of-file). Use before editing an unfamiliar file. Reads
        are bounded — do not read `node_modules`, `.next`, `dist`, or lockfiles."""
        session = sandbox_of(ctx)
        if is_read_ignored(path):
            raise ModelRetry(
                f"`{path}` is not readable — it's a heavy or irrelevant path (`node_modules`, "
                "`.next`, `dist`, `.git/`, a lockfile) or escapes the workspace. Every other "
                "workspace-relative path is readable, including root config like `package.json`, "
                "`next.config.ts`, and `tsconfig.json`."
            )
        # Bound the view so a huge file can't blow the context window (KD-10). The -1 end-of-file
        # spelling is bounded by the SAME budget: it becomes an explicit start+VIEW_MAX_LINES-1
        # window (the supervisor clamps to the real file length), so "-1 = end of file" holds for
        # any file within budget and a huge file is capped instead of read whole.
        if view_range is None:
            bounded = [1, VIEW_MAX_LINES]
        else:
            start = max(1, view_range[0])
            end = view_range[1]
            if end == -1 or end - start + 1 > VIEW_MAX_LINES:
                end = start + VIEW_MAX_LINES - 1
            bounded = [start, end]
        try:
            result = await session.sandbox_client.files(
                session.handle, FileView(path=path, view_range=bounded)
            )
        except SandboxGoneError:
            raise  # terminal infra failure — propagate to the sandbox_gone escalation (KD-11)
        except SandboxError as exc:
            raise ModelRetry(f"Could not read `{path}`: {exc}. Check the path.") from exc
        # `content` is a contractually-required C1 `view` field (C2) — a missing/non-str value is a
        # malformed response, surfaced as a retry not a phantom empty file (fail-first).
        content = result.detail.get("content")
        if not isinstance(content, str):
            raise ModelRetry(
                f"The read of `{path}` returned no content — a malformed sandbox response. Retry, "
                "or read a different path."
            )
        return content

    async def write_file(ctx: RunContext[Any], path: str, file_text: str) -> str:
        """Create or overwrite a file with `file_text`. Use for new files or a whole-file rewrite.
        Any workspace-relative path is allowed except `.git/` and paths escaping the workspace."""
        session = sandbox_of(ctx)
        _require_writable(path)
        try:
            await session.sandbox_client.files(
                session.handle, FileCreate(path=path, file_text=file_text)
            )
        except SandboxGoneError:
            raise  # terminal infra failure — propagate to the sandbox_gone escalation (KD-11)
        except SandboxError as exc:
            raise ModelRetry(f"Could not write `{path}`: {exc}.") from exc
        session.workspace_touched = True
        label, hidden = classify_file_step("write_file", path)
        await _step(session, name="edit", label=label, state="ok", hidden=hidden)
        return f"Wrote `{path}`."

    async def edit_file(ctx: RunContext[Any], path: str, old_str: str, new_str: str) -> str:
        """Replace the single exact occurrence of `old_str` with `new_str` in `path`. `old_str`
        must match EXACTLY ONCE — include at least 3 lines of unique surrounding context. Only
        writable paths are allowed."""
        session = sandbox_of(ctx)
        _require_writable(path)
        try:
            await session.sandbox_client.files(
                session.handle, FileStrReplace(path=path, old_str=old_str, new_str=new_str)
            )
        except SandboxGoneError:
            raise  # terminal infra failure — propagate to the sandbox_gone escalation (KD-11)
        except SandboxError as exc:
            # A files() error from a tool → enrich into a ModelRetry so the model self-corrects
            # in-run.
            raise ModelRetry(await _reanchor(session, path)) from exc
        session.workspace_touched = True
        label, hidden = classify_file_step("edit_file", path)
        await _step(session, name="edit", label=label, state="ok", hidden=hidden)
        return f"Edited `{path}`."

    async def insert_lines(
        ctx: RunContext[Any], path: str, insert_line: int, insert_text: str
    ) -> str:
        """Insert `insert_text` into `path` after line `insert_line` (0-based; 0 inserts at the
        top). Only writable paths are allowed."""
        session = sandbox_of(ctx)
        _require_writable(path)
        try:
            await session.sandbox_client.files(
                session.handle,
                FileInsert(path=path, insert_line=insert_line, insert_text=insert_text),
            )
        except SandboxGoneError:
            raise  # terminal infra failure — propagate to the sandbox_gone escalation (KD-11)
        except SandboxError as exc:
            raise ModelRetry(f"Could not insert into `{path}`: {exc}.") from exc
        session.workspace_touched = True
        label, hidden = classify_file_step("insert_lines", path)
        await _step(session, name="edit", label=label, state="ok", hidden=hidden)
        return f"Inserted into `{path}`."

    async def declare_done(ctx: RunContext[Any], summary: str) -> str:
        """Declare the build finished, and put your closing message to the user in `summary`.

        On a passing check this call ENDS THE TURN, so `summary` is the last thing the user
        reads. Write it for them rather than about the work: a short list of what they can now
        do with their app, a handful of plain sentences in the everyday words they used to ask
        for it, with no file names, commands, libraries or frameworks in it. Do not hold that
        message back for a reply afterwards — on the passing path there is no reply to write it
        in. If the app does NOT check out you receive the diagnostic and carry on fixing it
        (KD-6)."""
        session = sandbox_of(ctx)
        session.done_requested = True
        session.done_summary = summary
        # Deliberately does NOT set `workspace_touched`. A claim is not a mutation, and that flag
        # is the turn engine's only evidence that anything was actually built: setting it here let
        # a model that wrote nothing declare itself done and collect "Build complete — your app is
        # live below" over an untouched template. The write tools above set it when they write.
        await _step(session, name="declare_done", label="Verifying the build…", state="started")
        return (
            "Acknowledged — that summary is now the closing message the user reads. The harness "
            "is checking the app: if it checks out, this turn ends here and nothing further is "
            "asked of you. If it does not, you will get the diagnostic to fix."
        )

    async def run_command(ctx: RunContext[Any], command: list[str]) -> str:
        """Run a shell command in the app workspace and get its output back. Pass the command as a
        list of argv tokens — e.g. `["npm", "install", "zod"]`, `["npm", "run", "lint"]`,
        `["ls", "app"]`. It runs as an unprivileged user; the output is secret-redacted and
        length-capped before you see it. A non-zero exit code comes back as a normal result — read
        the output and fix the cause. A long output is cut to its first and last lines, and the
        notice in the middle names a handle — pass that handle to `fetch_output_slice` to read
        what was cut, instead of running the command again.
        Do NOT start or restart the dev server (`next dev`); it is
        already running and the harness reads it for you (KD-6)."""
        session = sandbox_of(ctx)
        # alias keeps the call off the JS-oriented exec guard
        transport = session.sandbox_client.exec
        # The FRIENDLY label is the only thing the browser ever sees for this command (F3/U3): the
        # classifier maps argv → citizen-plain copy (or a fail-closed "Working on your app"), so
        # the raw command / `$ argv` never reaches a visible step. `redacted_cmd` stays MODEL-only
        # — it rides the retry messages the model reads, never a StepEvent.
        friendly, hidden = classify_command(command)
        redacted_cmd = redact_secrets(" ".join(command)[:REDACT_INPUT_MAX_CHARS])
        # The data-safety sentinel (U1 / #12) runs BEFORE the transport: improvised destructive
        # SQL never reaches the sandbox. The blocked attempt is emitted as a failed step so
        # route-around behaviour stays observable in BRAIN traces (the iteration-2 tripwire).
        refusal = you_shall_not_pass(command)
        if refusal is not None:
            # The `— blocked …`/`— couldn't finish` suffixes are a LIVE-ONLY affordance on the
            # friendly base: on reload the state (failed) matches and the reason rides the Details
            # expander instead, so parity stays 'same friendly item, no raw shell' (see
            # classify_command).
            await _step(
                session,
                name="run_command",
                label=f"{friendly} — blocked to protect your data",
                state="failed",
                hidden=hidden,
            )
            raise ModelRetry(refusal)
        # U22's adoption question, counted where it is observable: did the slice handle actually
        # replace the re-run it exists to save? An identical command run a second time inside ONE
        # turn is the cost being measured, and it is read beside `output_slice_fetched` — one
        # number alone says nothing.
        #
        # COUNTED AFTER THE SQL SENTINEL AND BEFORE THE TRANSPORT, deliberately. A refused command
        # never ran, so a second refusal is the model routing around a guard (U1's own tripwire
        # already watches that) rather than paying for output it lost. A command that RAN and
        # failed is a genuine repeat and counts as one.
        if session.note_command(redacted_cmd):
            await _count_at_the_tool_boundary(HarnessCounter.COMMAND_RERUN_IN_TURN, session)
        # U23's adoption question, its other half: the sequence driven BY HAND, counted where the
        # hand is. Read against `schema_change_composed` — one number alone cannot tell "the
        # composite is being used" from "nobody is changing the schema at all".
        if _still_doing_it_the_hard_way(command):
            await _count_at_the_tool_boundary(HarnessCounter.SCHEMA_CHANGE_BY_HAND, session)
        # No `started` emit: run_command collapses to ONE terminal row per command (F3/U3). The
        # build headline spinner already conveys "working", and two emits sharing a friendly label
        # would otherwise render as two identical rows — this matches the reload projection's
        # one-row shape.
        try:
            # F4: the bound depends on WHAT the command is. A wedged command used to get the
            # full 600s, and the 1800s wall-clock deadline is only evaluated BETWEEN self-heal
            # iterations, so nothing could interrupt a churning one.
            timeout_s = (
                RUN_COMMAND_SLOW_TIMEOUT_S
                if command_needs_the_long_timeout(command)
                else RUN_COMMAND_DEFAULT_TIMEOUT_S
            )
            result = await transport(session.handle, command, timeout_s=timeout_s)
        except SandboxGoneError:
            await _step(
                session,
                name="run_command",
                label=f"{friendly} — couldn't finish",
                state="failed",
                hidden=hidden,
            )
            raise  # terminal infra failure — propagate to the sandbox_gone escalation (KD-11)
        except SandboxError as exc:
            # A transport failure (supervisor 504 incl. an install timeout, or a blip) → enrich
            # into a ModelRetry so a command/install failure re-enters the loop instead of
            # hard-crashing the build (R11). Only SandboxGoneError escalates. The message is
            # redacted defensively.
            await _step(
                session,
                name="run_command",
                label=f"{friendly} — couldn't finish",
                state="failed",
                hidden=hidden,
            )
            # `denoise=False`: a transport exception is not a dependency-manager log, and the
            # only thing a noise filter could do to one is delete a line of it.
            detail = _redact_command_output(
                str(exc), budget=RUN_COMMAND_OUTPUT_MAX_CHARS, denoise=False
            )
            raise ModelRetry(
                f"`{redacted_cmd}` could not run: {detail}. The sandbox may be busy or the "
                "command may have timed out — retry, or adjust the command."
            ) from exc
        await _step(
            session,
            name="run_command",
            label=friendly,
            state="ok" if result.exit == 0 else "failed",
            hidden=hidden,
        )
        # A command that RAN counts as touching the workspace, and it has to: the open-sandbox
        # pivot made this a first-class write surface (`npm install`, scaffolding, codegen, `git`),
        # so a build can legitimately do all of its work here and never call a file tool. That was
        # invisible while `declare_done` set this flag for everyone; with the flag removed from a
        # mere claim, leaving it unset here would fail honest builds with "nothing was built".
        #
        # The residual is deliberate and much narrower than what it replaces: a run that only ever
        # shelled `ls` and then declared done still satisfies the guard. Distinguishing that from a
        # real mutation needs the workspace's own answer (a `git status` round-trip), which is a
        # bigger change than this guard is worth. Acting on the workspace is the line; TALKING
        # about it is not.
        session.workspace_touched = True
        # DE-NOISE A BUILD LOG, NEVER A FILE. `run_command` is the open sandbox's general shell,
        # so the same call that runs `npm install` also runs `cat`, `sed -n '40,80p'` and `grep`
        # — and there the "output" IS file content. Dropping a line from it is a silent edit to
        # what the model believes the file says, and the `edit_file` composed from that read then
        # fails to match with nothing on screen to explain why. The classifier already knows
        # which argv only inspect; it is asked here rather than guessed from the text.
        return await _format_command_result(
            session, result, command=redacted_cmd, denoise=not command_only_inspects(command)
        )

    async def fetch_output_slice(
        ctx: RunContext[Any], handle: str, start_line: int, end_line: int
    ) -> str:
        """Read the part of a command's output that was cut, using the handle from its truncation
        notice. Pass `handle` exactly as the notice spelled it plus the 1-indexed `start_line` and
        `end_line` you want — the notice already names the range that was removed. Use this
        INSTEAD of running the command again; the output is held only until this turn ends, and
        if it has already been released you are told to re-run the command."""
        session = sandbox_of(ctx)
        held = session.held_outputs.get(handle)
        if held is None:
            # A PLAIN INSTRUCTION, NOT A `ModelRetry` — see `OUTPUT_NO_LONGER_HELD`.
            return OUTPUT_NO_LONGER_HELD
        total = len(held.lines)
        first = max(1, start_line)
        if first > total:
            return (
                f"`{handle}` holds {total:,} lines — line {start_line} is past the end. Ask for a "
                f"range inside 1-{total:,}."
            )
        last = total if end_line <= 0 else min(end_line, total)
        window = list(held.lines[first - 1 : max(first, last)][:OUTPUT_SLICE_MAX_LINES])
        # TRIMMED BY LINES, NOT BY CHARACTERS, and that order is the whole correctness of the
        # header. Cutting the joined text AFTER the range was computed made the header name lines
        # that were never returned, and the continuation hint below never fired, because it was
        # guarded on the line cap alone — so a model following the tool's own instruction skipped
        # the gap in silence. That is this unit's own defect, one layer down.
        window = window[: max(1, _leading_lines_within(window, RUN_COMMAND_OUTPUT_MAX_CHARS))]
        shown_last = first + len(window) - 1
        await _count_at_the_tool_boundary(HarnessCounter.OUTPUT_SLICE_FETCHED, session)
        # ALREADY REDACTED — `held.lines` is `_redacted_lines`' output and nothing else is ever
        # put there, so this returns masked text without a second redaction pass (a second pass
        # over a SLICE is exactly what would re-expose a credential straddling the cut).
        body = "\n".join(window)
        if len(body) > RUN_COMMAND_OUTPUT_MAX_CHARS:
            # ONE line longer than the whole budget. There is no narrower slice to ask for — a
            # handle addresses lines, not characters — so it is rendered head + tail like any
            # other over-budget output, and the notice says the middle of the line is only
            # reachable by re-running a narrower command.
            body = _render_output(window, budget=RUN_COMMAND_OUTPUT_MAX_CHARS, handle=None)
        header = f"`{held.command}` output, lines {first:,}-{shown_last:,} of {total:,}:"
        if shown_last < last:
            header += f" (more remains — continue from start_line={shown_last + 1})"
        return f"{header}\n{body}"

    async def apply_schema_change(ctx: RunContext[Any], what_changed: str) -> str:
        """Apply the schema edits you just made in `db/schema.ts` — this generates the migration
        and runs it in one call, and tells you truthfully which step failed if either did.

        Pass `what_changed` as a short description of the edit ("add visitors table"); it names
        the migration file, so pass something a person could still read six months from now. Make
        ONE kind of schema change per call — drizzle-kit cannot tell a rename from a drop plus a
        create, so it stops and asks, and there is no terminal here to answer it. Both commands
        behind this call can print a failure and still exit 0, which is exactly what this call
        exists to catch: it reports each step's outcome, and on a failure it names the step,
        overrides the misleading exit code, and tells you what state your workspace and database
        were left in. Trust what it says over any exit code inside it, and do not run the
        generate or migrate commands yourself through `run_command`."""
        session = sandbox_of(ctx)
        # THE ADOPTION QUESTION'S FIRST HALF, counted before anything can go wrong: "was the tool
        # reached for", not "did it succeed". A composite that failed was still adopted.
        await _count_at_the_tool_boundary(HarnessCounter.SCHEMA_CHANGE_COMPOSED, session)
        name = _migration_name(what_changed)
        if not name:
            raise ModelRetry(
                "`what_changed` has to describe the schema edit in words — "
                f"`{redact_secrets(what_changed[:REDACT_INPUT_MAX_CHARS])}` leaves nothing to "
                "name the migration file with. Try something like `add visitors table`."
            )
        friendly, hidden = classify_tool_call(APPLY_SCHEMA_CHANGE_TOOL, "")
        # alias keeps the call off the JS-oriented exec guard
        transport = session.sandbox_client.exec
        outcomes: list[_StepOutcome] = []
        for step in _the_two_steps(name):
            if outcomes and not outcomes[-1].ok:
                # STEP TWO DOES NOT RUN AFTER A FAILED STEP ONE, and it is recorded rather than
                # dropped: "not run" is a per-step outcome the model needs, because the state it
                # implies (nothing applied) is different from "ran and failed".
                outcomes.append(_StepOutcome(step=step, result=None, ok=False))
                continue
            argv = list(step.argv)
            # F4's classifier, ASKED RATHER THAN ASSUMED — the same one `run_command` uses, so a
            # step here can never get a different bound from the identical command run by hand.
            # Neither of these is in the slow class, and that is the point: a generate still
            # running after minutes is waiting for a terminal that does not exist.
            timeout_s = (
                RUN_COMMAND_SLOW_TIMEOUT_S
                if command_needs_the_long_timeout(argv)
                else RUN_COMMAND_DEFAULT_TIMEOUT_S
            )
            try:
                result = await transport(session.handle, argv, timeout_s=timeout_s)
            except SandboxGoneError:
                await _step(
                    session,
                    name=APPLY_SCHEMA_CHANGE_TOOL,
                    label=f"{friendly} — couldn't finish",
                    state="failed",
                    hidden=hidden,
                )
                raise  # terminal infra failure — propagate to the sandbox_gone escalation (KD-11)
            except SandboxError as exc:
                await _step(
                    session,
                    name=APPLY_SCHEMA_CHANGE_TOOL,
                    label=f"{friendly} — couldn't finish",
                    state="failed",
                    hidden=hidden,
                )
                # A transport failure is not a step verdict — the step never returned one — so it
                # goes back as a `ModelRetry` like `run_command`'s, and still names the state.
                detail = _redact_command_output(
                    str(exc), budget=RUN_COMMAND_OUTPUT_MAX_CHARS, denoise=False
                )
                raise ModelRetry(
                    f"The `{step.what}` step could not run: {detail}. "
                    f"{step.state_when_failed} Retry once the sandbox settles."
                ) from exc
            # A step that RAN acted on the workspace — same rule `run_command` applies, and for
            # the same reason: the generate writes files and the migrate writes tables.
            session.workspace_touched = True
            outcomes.append(
                _StepOutcome(
                    step=step,
                    result=result,
                    ok=result.exit == 0 and not _the_command_lied(result, step.failure_markers),
                )
            )
        succeeded = all(outcome.ok for outcome in outcomes)
        await _step(
            session,
            name=APPLY_SCHEMA_CHANGE_TOOL,
            label=friendly,
            state="ok" if succeeded else "failed",
            hidden=hidden,
        )
        # THE OVERRIDE REACHES THE CAP TOO, not just the wording. `output_budget_for_exit` is
        # asked about the OPERATION's verdict rather than any command's exit code, so a step that
        # failed while exiting 0 is DUMPED like the failure it is — sizing it from the underlying
        # zero would let the lie decide how much of the truth the model gets to read (U22/ASM13).
        return await _render_the_schema_change_report(
            session, outcomes, budget=output_budget_for_exit(0 if succeeded else 1)
        )

    toolset = FunctionToolset[Any](
        [
            read_file,
            write_file,
            edit_file,
            insert_lines,
            declare_done,
            run_command,
            fetch_output_slice,
            apply_schema_change,
        ],
        id="sandbox-tools",
    )
    return cast(FunctionToolset[DepsT], toolset)
