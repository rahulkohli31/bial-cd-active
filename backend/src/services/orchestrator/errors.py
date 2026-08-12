"""Raw exec/dev output → the frozen `BuildError{source, title, cleaned_stack}` (KD-5 / KD-8).

`declutter` turns a raw `tsc` blob or dev-server stderr tail into the structured, self-heal-
relevant error the repair prompt (and the C7 `error` envelope) carries. It **redacts first**:
the diagnostic egresses TWICE — into the portal envelope AND into the next run's model prompt —
and the running app can `console.log(process.env)`, so any credential-shaped substring must be
masked before anything else touches the text.

`redact_secrets` is the single redactor, reused by `progress.py` for the raw `log` egress path
(C7 §3.2 relays stdout/stderr straight to the portal). Treat all sandbox output as untrusted and
parse defensively ([[sandbox-supervisor-child-env-scrub-allowlist]]).

The redactor ITSELF now lives in `src/core/redaction.py` (U4 moved it so the native message
store's persistence seam can reuse it without importing the orchestrator); this module re-exports
it so every historical `from src.services.orchestrator.errors import redact_secrets` call site
keeps working. One implementation, two import paths — never a fork.
"""

from __future__ import annotations

import re

from src.api.v1.build_sessions.schemas import BuildError, ErrorSource
from src.core.redaction import redact_secrets as redact_secrets
from src.services.orchestrator.constants import CLEANED_STACK_MAX_CHARS, REDACT_INPUT_MAX_CHARS

_TITLE_MAX_CHARS = 200
_TRUNCATION_MARKER = "\n[... diagnostic truncated ...]"
_FALLBACK_TITLE = "The build reported an error with no readable diagnostic."

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
# Absolute sandbox paths → workspace-relative. The app lives at /workspace/app (KD-6).
_WORKSPACE_ROOTS = ("/workspace/app/", "/workspace/")

# `next build` opens with a banner and progress spinners, so its FIRST line is reliably
# "▲ Next.js 16.2.10" — noise. Scan for the line that actually names the failure, in
# SPECIFICITY order (a later `Type error:` beats an earlier generic `TypeError:`), the same
# shape as the tsc arm's `error TS` scan.
_NEXT_BUILD_MARKERS = (
    "Type error:",
    "Module not found:",
    "Error occurred prerendering page",
    # The build ran out of memory rather than finding a fault in the code. Surfacing it as
    # the TITLE matters: told "your code has an error", a citizen edits code that is fine.
    "JavaScript heap out of memory",
    "ReferenceError:",
    "TypeError:",
    "SyntaxError:",
    # Last and broadest: catches the shapes with no dedicated marker, notably
    # "Error: useSearchParams() should be wrapped in a suspense boundary" — a headline
    # member of the class `tsc --noEmit` is blind to.
    "Error:",
)
# A header, not a diagnostic — the useful line is the one after it.
_FAILED_TO_COMPILE = "Failed to compile"

# Progress chatter `next build` emits before (and after) anything useful. When no marker
# matched, the fallback must skip these or the title reads "▲ Next.js 16.2.10" — which
# tells a citizen nothing and tells the model less.
_NEXT_BUILD_NOISE_PREFIXES = (
    "▲",
    "✓",
    "✔",
    "○",
    "●",
    "ƒ",
    "└",
    "├",
    "┌",
    "> ",
    "$ ",
    "npm ",
    "Creating an optimized production build",
    "Compiled successfully",
    "Linting and checking validity of types",
    "Collecting page data",
    "Collecting build traces",
    "Generating static pages",
    "Finalizing page optimization",
    "Route (app)",
    "First Load JS",
)


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _relativize_paths(text: str) -> str:
    for root in _WORKSPACE_ROOTS:
        text = text.replace(root, "")
    return text


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + _TRUNCATION_MARKER


def _clip_title(line: str) -> str:
    """Cut an over-long title at a word boundary, with a visible marker. A bare `[:200]` cut
    lands mid-word ("…renders a blank page witho") and reads as a rendering bug on screen —
    the title is the ONE line of a diagnostic the portal's retry framing shows."""
    if len(line) <= _TITLE_MAX_CHARS:
        return line
    cut = line[:_TITLE_MAX_CHARS]
    head, space, _ = cut.rpartition(" ")
    return (head if space else cut).rstrip() + "…"


def _first_meaningful_line(text: str, source: ErrorSource) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return _FALLBACK_TITLE
    if source == ErrorSource.TSC:
        for line in lines:
            if "error TS" in line:
                return _clip_title(line)
    elif source == ErrorSource.NEXT_BUILD:
        for marker in _NEXT_BUILD_MARKERS:
            for line in lines:
                if marker in line:
                    return _clip_title(line)
        for index, line in enumerate(lines):
            if line.startswith(_FAILED_TO_COMPILE):
                if index + 1 < len(lines):
                    return _clip_title(lines[index + 1])
                break
        signal = [line for line in lines if not line.startswith(_NEXT_BUILD_NOISE_PREFIXES)]
        if signal:
            return _clip_title(signal[0])
    return _clip_title(lines[0])


def declutter(raw: str, source: ErrorSource) -> BuildError:
    """Redact → ANSI-strip → path-normalize → truncate → pick a title, yielding the frozen
    `BuildError`. Source-agnostic core (so a later `client` / `next_build` arm reuses it); the
    only source-specific bit is the title heuristic (`tsc` prefers the first `error TS…` line).
    Never crashes — empty input yields a safe fallback title."""
    # Cap the raw blob BEFORE redaction: the redactor is linear, but an app-controlled
    # multi-hundred-KB diagnostic must never dominate a synchronous pass on the event loop, and
    # the output is truncated to CLEANED_STACK_MAX_CHARS anyway (KD-5 defense-in-depth).
    redacted = redact_secrets(raw[:REDACT_INPUT_MAX_CHARS])
    cleaned = _relativize_paths(_strip_ansi(redacted))
    title = _first_meaningful_line(cleaned, source)
    return BuildError(
        source=source,
        title=title,
        cleaned_stack=_truncate(cleaned, CLEANED_STACK_MAX_CHARS),
    )


def from_tsc(raw: str) -> BuildError:
    """A raw `tsc --noEmit` blob → `BuildError(source=tsc)`."""
    return declutter(raw, ErrorSource.TSC)


def from_server(raw: str) -> BuildError:
    """A raw dev-server stderr tail → `BuildError(source=server)`."""
    return declutter(raw, ErrorSource.SERVER)


def from_next_build(raw: str) -> BuildError:
    """A raw `next build` log → `BuildError(source=next_build)`.

    The PRODUCTION build, run where the shipped image is made — not the dev-server verify.
    `tsc --noEmit` is structurally blind to the whole prerender/bundling failure class
    (`useSearchParams` without a Suspense boundary, `window` at module scope, `server-only`
    pulled into a client graph, a route that throws during static generation), so this is
    the only signal that says an app can actually be built and shipped.

    `ErrorSource.NEXT_BUILD` has existed unused since the taxonomy was written — this is the
    arm the docstring on `declutter` anticipated."""
    return declutter(raw, ErrorSource.NEXT_BUILD)
