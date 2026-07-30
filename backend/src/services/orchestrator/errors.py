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
