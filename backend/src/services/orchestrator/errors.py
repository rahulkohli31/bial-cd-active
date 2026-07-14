"""Raw exec/dev output → the frozen `BuildError{source, title, cleaned_stack}` (KD-5 / KD-8).

`declutter` turns a raw `tsc` blob or dev-server stderr tail into the structured, self-heal-
relevant error the repair prompt (and the C7 `error` envelope) carries. It **redacts first**:
the diagnostic egresses TWICE — into the portal envelope AND into the next run's model prompt —
and the running app can `console.log(process.env)`, so any credential-shaped substring must be
masked before anything else touches the text.

`redact_secrets` is the single redactor, reused by `progress.py` for the raw `log` egress path
(C7 §3.2 relays stdout/stderr straight to the portal). Treat all sandbox output as untrusted and
parse defensively ([[sandbox-supervisor-child-env-scrub-allowlist]]).
"""

from __future__ import annotations

import re

from src.api.v1.build_sessions.schemas import BuildError, ErrorSource
from src.services.orchestrator.constants import CLEANED_STACK_MAX_CHARS

_TITLE_MAX_CHARS = 200
_TRUNCATION_MARKER = "\n[... diagnostic truncated ...]"
_FALLBACK_TITLE = "The build reported an error with no readable diagnostic."

# --- secret redaction (KD-5) -------------------------------------------------
# BIAL_APP_CREDENTIAL is "bial_" + token_urlsafe(24) (C6 §4) → "bial_" + ~32 url-safe chars.
_CREDENTIAL_RE = re.compile(r"bial_[A-Za-z0-9_-]{16,}")
# Mask the VALUE of any *_TOKEN / *_SECRET / *_KEY assignment. The KEY and the VALUE may each be
# quoted, covering all three shapes `console.log(process.env)` / `JSON.stringify(process.env)`
# produce: JSON `"NAME":"v"`, JS object `NAME: 'v'`, and dotenv `NAME=v`. A quoted value may span
# spaces/newlines (a passphrase, a PEM); a bare value runs to the next structural delimiter, so a
# multi-word dotenv value is masked WHOLE. Over-redact rather than leak. The name + separator +
# surrounding quotes are preserved so the diagnostic still reads.
_SECRET_ASSIGN_RE = re.compile(
    r"(['\"]?[A-Za-z_][A-Za-z0-9_]*(?:_TOKEN|_SECRET|_KEY)['\"]?\s*[:=]\s*)"  # key + separator
    # value: double-quoted | single-quoted (both may span spaces/newlines — a PEM/passphrase) |
    # a bare run to the next structural delimiter (so a multi-word dotenv value masks whole, but
    # a bare value can't swallow the next object key).
    r"(\"[^\"]*\"|'[^']*'|[^\r\n,;{}\[\]'\"]+)",
    re.IGNORECASE,
)
_MASK = "***"


def _mask_assignment(match: re.Match[str]) -> str:
    prefix, value = match.group(1), match.group(2)
    quote = value[0] if value[:1] in ("'", '"') else ""
    return f"{prefix}{quote}{_MASK}{quote}"


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
# Absolute sandbox paths → workspace-relative. The app lives at /workspace/app (KD-6).
_WORKSPACE_ROOTS = ("/workspace/app/", "/workspace/")


def redact_secrets(text: str) -> str:
    """Mask credential-shaped and `*_TOKEN/_SECRET/_KEY=value` substrings (KD-5). Idempotent and
    safe on any string — used on BOTH egress paths (the `error` envelope's `cleaned_stack` here,
    and the raw `log` text in `progress.py`)."""
    masked = _CREDENTIAL_RE.sub(_MASK, text)
    return _SECRET_ASSIGN_RE.sub(_mask_assignment, masked)


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


def _first_meaningful_line(text: str, source: ErrorSource) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return _FALLBACK_TITLE
    if source == ErrorSource.TSC:
        for line in lines:
            if "error TS" in line:
                return line[:_TITLE_MAX_CHARS]
    return lines[0][:_TITLE_MAX_CHARS]


def declutter(raw: str, source: ErrorSource) -> BuildError:
    """Redact → ANSI-strip → path-normalize → truncate → pick a title, yielding the frozen
    `BuildError`. Source-agnostic core (so a later `client` / `next_build` arm reuses it); the
    only source-specific bit is the title heuristic (`tsc` prefers the first `error TS…` line).
    Never crashes — empty input yields a safe fallback title."""
    redacted = redact_secrets(raw)
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
