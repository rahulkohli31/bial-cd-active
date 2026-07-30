"""The ONE hardened secret redactor — moved here from `services/orchestrator/errors.py` (U4)
so non-orchestrator callers (the native message store's persistence seam) can reuse it without
importing the orchestrator. The orchestrator keeps importing it from its historical path via a
re-export; there is exactly one implementation (never write new redaction regexes — the ReDoS
learning `security-issues/redos-secret-redaction-regex-2026-07-14.md` applies to every pattern
here: LINEAR patterns only, and callers on synchronous event-loop paths cap input length BEFORE
scanning).
"""

from __future__ import annotations

import re

_MASK = "***"

# `app_registry.app_key` is "bial_" + token_urlsafe(24) → "bial_" + ~32 url-safe chars. It is a
# publishable label rather than a secret and is no longer injected into the sandbox at all, but a
# diagnostic that echoes one still has no business reaching the portal — over-redact.
_CREDENTIAL_RE = re.compile(r"bial_[A-Za-z0-9_-]{16,}")
# Mask the VALUE of any credential-shaped `NAME<sep>value` assignment. The KEY and the VALUE may
# each be quoted, covering all three shapes `console.log(process.env)` / `JSON.stringify(process
# .env)` produce: JSON `"NAME":"v"`, JS object `NAME: 'v'`, and dotenv `NAME=v`. A quoted value
# may span spaces/newlines (a passphrase, a PEM); a bare value runs to the next structural
# delimiter, so a space-separated dotenv value is masked WHOLE. A value that itself packs `;`
# pairs (a connection string) has only its FIRST segment masked here — its embedded
# `Password=`/`AccountKey=`/`sig=` params are caught by `_CONN_PARAM_RE` below. Over-redact rather
# than leak. The name + separator + surrounding quotes are preserved so the diagnostic still reads.
#
# The key quantifier is BOUNDED (`{0,64}`, not `*`) on purpose: the trailing suffix alternation
# would otherwise force quadratic backtracking over a long run of `[A-Za-z0-9_]` (a
# `console.log`-dumped blob is app-controlled), stalling the event loop — a measured DoS. Env-var
# names never approach 64 chars, so the bound keeps the scan LINEAR without losing real matches.
# The suffix set is deliberately broad (families the child-env-scrub learning shows a narrow
# `_TOKEN/_SECRET/_KEY` filter misses) — [[sandbox-supervisor-child-env-scrub-allowlist]].
_SECRET_ASSIGN_RE = re.compile(
    r"(['\"]?[A-Za-z_][A-Za-z0-9_]{0,64}"  # key (bounded → linear)
    r"(?:_TOKEN|_SECRET|_SECRETS|_KEY|_APIKEY|_API_KEY|_ACCESS_KEY|_PASSWORD|_PASSWD|_PWD"
    r"|_CREDENTIAL|_CREDENTIALS|_AUTH|_DSN|_CONNECTION_STRING)['\"]?\s*[:=]\s*)"  # + separator
    # value: double-quoted | single-quoted (both may span spaces/newlines — a PEM/passphrase) |
    # a bare run to the next structural delimiter (so a multi-word dotenv value masks whole, but
    # a bare value can't swallow the next object key).
    r"(\"[^\"]*\"|'[^']*'|[^\r\n,;{}\[\]'\"]+)",
    re.IGNORECASE,
)
# URL-embedded credentials: `scheme://user:pass@host` → `scheme://***:***@host`. A connection
# string (`DATABASE_URL=postgres://u:p@db`) has a KEY ending in `_URL`, so the assignment regex
# above does not mask its value — this catches the userinfo in the value instead. Linear (no
# nested quantifier); benign `https://host:port/path` URLs (no `@`) are left untouched.
_URL_CRED_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.\-]*://)([^\s:/@]+):([^\s/@]+)@")
# `Authorization: Bearer <token>` / a bare `Bearer <token>` header dump.
_BEARER_RE = re.compile(r"(bearer\s+)([A-Za-z0-9._\-]+)", re.IGNORECASE)
# Credential PARAMETERS embedded in a connection string or a SAS query — masked regardless of any
# `NAME<sep>` prefix. A connection string packs many `key=value;` pairs on ONE line, so the
# assignment regex above only reaches the first `;` and leaves a trailing `Password=`/`AccountKey=`
# exposed; a raw SAS URL (`…?sv=…&sig=…`) has no assignment prefix at all. This pass masks the
# sensitive parameter VALUE directly (ADO `Password`/`Pwd`, Azure `AccountKey`/`SharedAccessKey`/
# `SharedAccessSignature`, a SAS `sig`, a bare `secret`/`access_key`). Case-insensitive; the value
# runs to the next `;`/`&`/whitespace/quote or end. Linear (bounded key alternation + one value run
# — no nesting), so it stays ReDoS-safe on the input-capped run_command/log egress surface.
_CONN_PARAM_RE = re.compile(
    r"(?i)\b(password|pwd|accountkey|sharedaccesskey|sharedaccesssignature|sig|secret"
    r"|access[_-]?key)(\s*[=:]\s*)([^\s;&,'\"]+)"
)


def _mask_assignment(match: re.Match[str]) -> str:
    prefix, value = match.group(1), match.group(2)
    quote = value[0] if value[:1] in ("'", '"') else ""
    return f"{prefix}{quote}{_MASK}{quote}"


def redact_secrets(text: str) -> str:
    """Mask credential-shaped substrings (KD-5): `bial_…` credentials, `NAME<sep>value`
    assignments across the credential families, URL-embedded `user:pass@`, connection-string / SAS
    parameters (`Password=`/`AccountKey=`/`SharedAccessSignature=`/`sig=`, masked even mid-string
    after a `;` where the assignment pass stops), and `Bearer` tokens. Idempotent and safe on any
    string — used on every egress path (build error envelopes, raw log relay, `run_command`
    stdout) AND at the message store's persistence seam (U4). Every pattern is LINEAR so it cannot
    stall the event loop on an adversarial (app-controlled) blob."""
    masked = _CREDENTIAL_RE.sub(_MASK, text)
    masked = _SECRET_ASSIGN_RE.sub(_mask_assignment, masked)
    masked = _URL_CRED_RE.sub(rf"\g<1>{_MASK}:{_MASK}@", masked)
    masked = _CONN_PARAM_RE.sub(rf"\g<1>\g<2>{_MASK}", masked)
    return _BEARER_RE.sub(rf"\g<1>{_MASK}", masked)
