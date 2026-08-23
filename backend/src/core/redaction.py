"""The ONE hardened secret redactor — moved here from `services/orchestrator/errors.py` (U4)
so non-orchestrator callers (the native message store's persistence seam) can reuse it without
importing the orchestrator. The orchestrator keeps importing it from its historical path via a
re-export; there is exactly one implementation (never write new redaction regexes — the ReDoS
learning `security-issues/redos-secret-redaction-regex-2026-07-14.md` applies to every pattern
here: LINEAR patterns only, and callers on synchronous event-loop paths cap input length BEFORE
scanning).

Since U2 (plan 2026-08-19-001) the module carries TWO consumers of one pattern set:

* `redact_secrets` — the masker. Tuned to over-redact; a false positive costs nothing on an
  egress path.
* `detect_credentials` — the pre-publish credential scan (R4a/P8). Reports the pattern FAMILY,
  a Tier label, and a line number — NEVER the matched value. Tier A is value-shaped and
  unambiguous (ported from the gitleaks / detect-secrets rule sets, patterns not dependency);
  it stands in as the credentials answer when the model is unavailable, so its precision
  burden is absolute. Tier B is a credential-shaped NAME with a hardcoded literal value —
  a lead handed to the review, expected to be mostly noise, never binding.

The masker's patterns are wrong for source code in both directions (ASM2): they miss
`const password = "hunter2"` (quoted values, camelCase names) and fire on
`password: z.string().min(8)` (any bare value). The shared `_NAME_LITERAL_RE` family widens
the masker to quoted source literals while its post-filter keeps the DETECTOR off values that
are identifiers, member accesses or call expressions. One candidate pattern, one classifier,
two consumers — no second regex family to drift.
"""

from __future__ import annotations

import asyncio
import base64
import enum
import json
import re
from bisect import bisect_right
from collections.abc import Callable
from dataclasses import dataclass

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
# above does not mask its value — this catches the userinfo in the value instead. Benign
# `https://host:port/path` URLs (no `@`) are left untouched.
#
# The scheme run is BOUNDED (`{0,64}`, was `*`) for the same reason as the assignment key
# above: unbounded, a long pure-alphanumeric run (a minified bundle line, base64 in a log)
# made the search for `://` QUADRATIC — ~30s at the detector's 256KB ceiling, measured. Every
# real scheme fits in 65 chars with room to spare, so the bound loses no masking.
_URL_CRED_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.\-]{0,64}://)([^\s:/@]+):([^\s/@]+)@")
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
#
# Widened for U2: a QUOTED value alternation (`Password="p w d"`, `password: "hunter2"`) — the
# bare class excludes quote characters, so quoted source literals previously passed through
# unmasked (ASM2). Bare values behave byte-identically to before; quoted arms require at least
# one char so an empty `""` is never rewritten into a phantom `"***"`.
_CONN_PARAM_RE = re.compile(
    r"(?i)\b(password|pwd|accountkey|sharedaccesskey|sharedaccesssignature|sig|secret"
    r"|access[_-]?key)(\s*[=:]\s*)(\"[^\"\r\n]{1,512}\"|'[^'\r\n]{1,512}'|[^\s;&,'\"]+)"
)

# --- the shared name-literal family (U2) ----------------------------------------------------
#
# One candidate pattern serving BOTH consumers: the masker masks the value of any
# credential-shaped name whose value is a hardcoded literal, and the detector reports the same
# set as Tier B leads. The separator guards keep comparisons (`==`/`===`/`!=`), arrows (`=>`),
# walrus (`:=`) and TS `::` out; the value arms are single-line and bounded. A long unbroken
# identifier run costs at most 65 backtracks per offset (the same accepted constant as
# `_SECRET_ASSIGN_RE`'s bounded key), so the scan stays linear on adversarial input.
_NAME_LITERAL_RE = re.compile(
    r"(['\"]?(?P<name>[A-Za-z_$][A-Za-z0-9_$]{0,64})['\"]?"
    r"\s{0,8}(?<![=!<>:])[:=](?![=>])\s{0,8})"
    r"(?P<value>\"[^\"\r\n]{0,512}\"|'[^'\r\n]{0,512}'|`[^`\r\n]{0,512}`"
    r"|[^\s,;(){}\[\]'\"`]{1,256})"
)
# Camel boundary for name-word splitting: `apiKey` → `api_Key` → ["api", "key"].
_CAMEL_SPLIT_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
# A name is credential-shaped when its LAST word (or last two words joined) is one of these —
# suffix-anchored so `monkey`/`tokenizer`/`sortField` never qualify while `stripeSecret`,
# `AUTH_TOKEN`, `accessKey` and `connectionString` all do.
_CREDENTIAL_NAME_WORDS = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "secret",
        "secrets",
        "token",
        "key",
        "apikey",
        "accesskey",
        "credential",
        "credentials",
        "auth",
        "dsn",
        "connectionstring",
    }
)
# A value that is an identifier or a member-access chain is a REFERENCE, not a hardcoded
# secret (`process.env.X`, `formData.get` — the candidate's bare arm stops at `(`, so a call
# expression surfaces here as its callee). Fully bounded: no nested unbounded quantifiers.
_REFERENCE_VALUE_RE = re.compile(
    r"[A-Za-z_$][A-Za-z0-9_$]{0,128}(?:\.[A-Za-z_$][A-Za-z0-9_$]{0,128}){0,16}"
)


def _credential_shaped_name(name: str) -> bool:
    words = [w for w in _CAMEL_SPLIT_RE.sub("_", name.replace("$", "")).lower().split("_") if w]
    if not words:
        return False
    return words[-1] in _CREDENTIAL_NAME_WORDS or "".join(words[-2:]) in _CREDENTIAL_NAME_WORDS


def _literal_secret_content(value: str) -> str | None:
    """The hardcoded content when `value` is a literal, else None (reference shapes: an
    identifier, a member access, a call expression's callee, an interpolated template)."""
    first = value[0]
    if first in "\"'`":
        content = value[1:-1]
        if first == "`" and "${" in content:
            return None
        return content
    if _REFERENCE_VALUE_RE.fullmatch(value):
        return None
    return value


# --- Tier A patterns, ported from established rule sets (gitleaks / detect-secrets) ----------
#
# Value-shaped and unambiguous on their own: what stands in as the credentials answer when the
# model is unavailable, and what an overrule is recorded against (P8). Every quantifier is
# bounded; every class excludes its own delimiter, so no pattern can backtrack quadratically.

_PEM_PRIVATE_KEY_RE = re.compile(r"-----BEGIN[ A-Z0-9_-]{0,64}PRIVATE KEY(?: BLOCK)?-----")
# Stripe LIVE secret/restricted keys only — `sk_test_…` is not a live credential and must stay
# a Tier B lead at most (it reaches the detector via the name family when assigned).
_STRIPE_LIVE_KEY_RE = re.compile(r"\b(?:sk|rk)_live_[0-9a-zA-Z]{10,99}\b")
_AWS_ACCESS_KEY_RE = re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")
_GITHUB_TOKEN_RE = re.compile(
    r"\b(?:(?:ghp|gho|ghu|ghs|ghr)_[0-9A-Za-z]{36}|github_pat_[0-9A-Za-z_]{82})\b"
)
_SLACK_TOKEN_RE = re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,250}")
# Candidate JWT shape; `_jwt_is_decodable` then requires header AND payload to base64-decode
# into JSON objects — "decodable" is the plan's word and the whole precision case.
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{6,4096}\.[A-Za-z0-9_-]{10,16384}\.[A-Za-z0-9_-]{8,4096}")
_AZURE_ACCOUNT_KEY_RE = re.compile(r"(?i)\bAccountKey=[A-Za-z0-9+/=]{88}")
# The whole SAS token (its value class admits `&`, so an embedded `sig=` is covered by the
# same span) or a bare `sig=` query parameter with a base64/percent-encoded run.
_AZURE_SAS_RE = re.compile(r"(?i)\bSharedAccessSignature=[^\s;,'\"]{16,1024}")
_SAS_SIG_RE = re.compile(r"(?i)\bsig=[A-Za-z0-9%+/]{16,512}={0,3}")
# A password parameter INSIDE a `;`-separated connection string (`Server=…;Password=…;`). The
# fixed-width lookbehind anchors it to connection-string context so an ordinary source
# assignment (`const password = …`) can never reach Tier A through this family.
_CONN_STRING_PASSWORD_RE = re.compile(
    r"(?i)(?<=;)\s{0,8}(?:password|pwd)\s{0,8}=\s{0,8}"
    r"(?:\"[^\"\r\n]{1,256}\"|'[^'\r\n]{1,256}'|[^\s;&,'\"]{1,256})"
)
# Detection-side bearer: same class as the masker's `_BEARER_RE`, floored at 8 chars so header
# placeholders (`Bearer TOKEN`) stay quiet. A lead only — a bearer VALUE that is itself a JWT
# lands Tier A via `_JWT_RE` and outranks this on overlap.
_BEARER_TOKEN_RE = re.compile(r"(?i)\bbearer\s{1,8}[A-Za-z0-9._\-]{8,512}")


def _decodes_to_json_object(segment: str) -> bool:
    padded = segment + "=" * (-len(segment) % 4)
    try:
        return isinstance(json.loads(base64.urlsafe_b64decode(padded)), dict)
    except ValueError:  # binascii.Error and JSONDecodeError are both ValueErrors
        return False


def _jwt_is_decodable(match: re.Match[str]) -> bool:
    header, payload, _signature = match.group(0).split(".", 2)
    return _decodes_to_json_object(header) and _decodes_to_json_object(payload)


_MIN_LITERAL_CHARS = 4  # a Tier B lead needs a value worth reading; `"ok"`-sized noise is not


def _name_literal_hit(match: re.Match[str]) -> bool:
    if not _credential_shaped_name(match.group("name")):
        return False
    content = _literal_secret_content(match.group("value"))
    return content is not None and len(content) >= _MIN_LITERAL_CHARS


class Tier(enum.StrEnum):
    """P8's two-tier split: A is value-shaped and binding when the model is down; B is a
    credential-shaped name with a literal value — a lead for the review, nothing more."""

    A = "A"
    B = "B"


@dataclass(frozen=True)
class CredentialHit:
    """One detection: the pattern family, its tier, and a 1-based line number. Deliberately
    NOWHERE to carry the matched value (R3/R4a — a hit that quoted its secret would leak it
    into prompts and admin-visible records)."""

    family: str
    tier: Tier
    line: int


@dataclass(frozen=True)
class CredentialScan:
    """The per-file scan result. `truncated` means the input exceeded the ceiling and only
    the prefix was scanned — the caller must treat that as an incomplete scan (U6 routes it
    to the review-failed bucket), never as a clean no-hit."""

    hits: tuple[CredentialHit, ...]
    truncated: bool


# Hard per-file input ceiling (bound the INPUT, not the output — the ReDoS learning). 256 KiB
# comfortably covers real generated source files; a bigger file is reported truncated.
SCAN_INPUT_MAX_CHARS = 262_144


@dataclass(frozen=True)
class _Detector:
    family: str
    tier: Tier
    pattern: re.Pattern[str]
    accept: Callable[[re.Match[str]], bool] | None = None


# The named collection the detector runs — every family label here is a stable identifier that
# ends up in review prompts and stored evidence (U6), so renaming one is a data migration.
_DETECTORS: tuple[_Detector, ...] = (
    _Detector("pem-private-key", Tier.A, _PEM_PRIVATE_KEY_RE),
    _Detector("stripe-live-key", Tier.A, _STRIPE_LIVE_KEY_RE),
    _Detector("aws-access-key", Tier.A, _AWS_ACCESS_KEY_RE),
    _Detector("github-token", Tier.A, _GITHUB_TOKEN_RE),
    _Detector("slack-token", Tier.A, _SLACK_TOKEN_RE),
    _Detector("jwt", Tier.A, _JWT_RE, _jwt_is_decodable),
    _Detector("url-credentials", Tier.A, _URL_CRED_RE),
    _Detector("azure-account-key", Tier.A, _AZURE_ACCOUNT_KEY_RE),
    _Detector("azure-sas-signature", Tier.A, _AZURE_SAS_RE),
    _Detector("azure-sas-signature", Tier.A, _SAS_SIG_RE),
    _Detector("connection-string-password", Tier.A, _CONN_STRING_PASSWORD_RE),
    _Detector("credential-name-literal", Tier.B, _NAME_LITERAL_RE, _name_literal_hit),
    _Detector("bearer-token", Tier.B, _BEARER_TOKEN_RE),
)


def _line_starts(text: str) -> list[int]:
    starts = [0]
    idx = text.find("\n")
    while idx != -1:
        starts.append(idx + 1)
        idx = text.find("\n", idx + 1)
    return starts


def detect_credentials(text: str) -> CredentialScan:
    """Scan one file's text for credential-shaped content (R4a/P8): each hit carries its
    pattern family, a Tier label and a 1-based line number — never the matched value. The
    caller attaches the path; this layer only ever sees one file's text.

    Overlapping matches collapse to ONE hit, Tier A outranking Tier B — `apiKey = "sk_live_…"`
    is one Stripe finding, not a finding plus its own echo. Input beyond the per-file ceiling
    is reported via `truncated` rather than silently scanning a prefix.
    """
    truncated = len(text) > SCAN_INPUT_MAX_CHARS
    bounded = text[:SCAN_INPUT_MAX_CHARS]
    spans: list[tuple[int, int, str, Tier]] = []
    for detector in _DETECTORS:
        for match in detector.pattern.finditer(bounded):
            if detector.accept is not None and not detector.accept(match):
                continue
            spans.append((match.start(), match.end(), detector.family, detector.tier))
    # Tier A first, then by position with the longer span winning, so an overlapped lead
    # (the Tier B echo of a Tier A value, a `sig=` inside its own SAS token) is dropped.
    spans.sort(key=lambda s: (s[3] is not Tier.A, s[0], -s[1]))
    accepted: list[tuple[int, int, str, Tier]] = []
    for start, end, family, tier in spans:
        if any(start < a_end and a_start < end for a_start, a_end, _, _ in accepted):
            continue
        accepted.append((start, end, family, tier))
    accepted.sort(key=lambda s: s[0])
    starts = _line_starts(bounded)
    hits = tuple(
        CredentialHit(family=family, tier=tier, line=bisect_right(starts, start))
        for start, _end, family, tier in accepted
    )
    return CredentialScan(hits=hits, truncated=truncated)


async def detect_credentials_off_loop(text: str) -> CredentialScan:
    """`detect_credentials` off the event loop (the `snapshot_read.py` convention:
    `asyncio.to_thread` for work that is not "fast enough to inline" — a full-file regex
    sweep at the ceiling is exactly that)."""
    return await asyncio.to_thread(detect_credentials, text)


def _mask_assignment(match: re.Match[str]) -> str:
    prefix, value = match.group(1), match.group(2)
    quote = value[0] if value[:1] in ("'", '"') else ""
    return f"{prefix}{quote}{_MASK}{quote}"


def _mask_conn_param(match: re.Match[str]) -> str:
    value = match.group(3)
    quote = value[0] if value[:1] in ("'", '"') else ""
    return f"{match.group(1)}{match.group(2)}{quote}{_MASK}{quote}"


def _mask_name_literal(match: re.Match[str]) -> str:
    if not _credential_shaped_name(match.group("name")):
        return match.group(0)
    value = match.group("value")
    content = _literal_secret_content(value)
    if not content:  # a reference, or an empty literal not worth rewriting into a phantom mask
        return match.group(0)
    quote = value[0] if value[0] in "\"'`" else ""
    return f"{match.group(1)}{quote}{_MASK}{quote}"


# Terminal escape sequences and invisible characters, stripped BEFORE any shape matching runs.
#
# THE ORDER IS THE FIX, not a tidy-up. `redact_secrets` matches credentials by SHAPE, so anything
# that splits a token defeats it while leaving the text visually identical: `DB_PASSWORD=\x1b[0m
# hunter2` renders as `DB_PASSWORD=hunter2` in any terminal and in most log viewers, and reaches
# the redactor as two fragments that match nothing. Zero-width and directional-override characters
# do the same job and are legal inside a JS string.
#
# It did not matter while every caller was output WE produced (`tsc`, the dev server, `next
# build` — none of which is adversarial). It matters for every path that carries text written by
# unreviewed code inside a generated app: the browser crash report, and the app's own served HTML.
#
# Not exhaustive against every Unicode trick — shape matching never can be — but these are the
# ones an app can emit without the text looking altered to a human reading the log.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_INVISIBLE_RE = re.compile(r"[\u200b-\u200f\u2028\u2029\u202a-\u202e\u2060-\u2064\ufeff]")


def strip_control_sequences(text: str) -> str:
    """Remove terminal escapes and invisible characters — see the note above on why this must
    run BEFORE `redact_secrets` rather than after it, or instead of it."""
    return _INVISIBLE_RE.sub("", _ANSI_RE.sub("", text))


def scrub_untrusted(text: str, *, limit: int) -> str:
    """The ONE way sandbox-authored text is made safe to store, log or show: capped, then
    de-escaped, then masked — in that order.

    ONE FUNCTION BECAUSE THE ORDER IS THE WHOLE PROPERTY. Three call sites had to get the same
    three steps in the same sequence, and the one that got it wrong was exploitable rather than
    merely untidy. A caller that reaches for `redact_secrets` alone on app-authored text is
    making the same mistake again, so there is now a name for the thing they actually want.

    The cap comes FIRST and is the caller's, because the callers differ on how much they can
    afford to keep: it bounds the work a hostile blob can make a synchronous, event-loop-bound
    scan do (`REDACT_INPUT_MAX_CHARS` is the orchestrator's answer; the served-page probe keeps
    far less)."""
    return redact_secrets(strip_control_sequences(text[:limit]))


def redact_secrets(text: str) -> str:
    """Mask credential-shaped substrings (KD-5): `bial_…` credentials, `NAME<sep>value`
    assignments across the credential families (underscore-suffixed AND, since U2, camelCase
    names with quoted/literal values — `const password = "hunter2"`), URL-embedded
    `user:pass@`, connection-string / SAS parameters (`Password=`/`AccountKey=`/
    `SharedAccessSignature=`/`sig=`, masked even mid-string after a `;` where the assignment
    pass stops, now including quoted values), and `Bearer` tokens. Idempotent and safe on any
    string — used on every egress path (build error envelopes, raw log relay, `run_command`
    stdout) AND at the message store's persistence seam (U4). Every pattern is LINEAR so it
    cannot stall the event loop on an adversarial (app-controlled) blob. The name-literal pass
    runs AFTER the URL pass so a credential-shaped username (`postgres://token:x@h`) is masked
    as userinfo, preserving the URL's structure."""
    masked = _CREDENTIAL_RE.sub(_MASK, text)
    masked = _SECRET_ASSIGN_RE.sub(_mask_assignment, masked)
    masked = _URL_CRED_RE.sub(rf"\g<1>{_MASK}:{_MASK}@", masked)
    masked = _CONN_PARAM_RE.sub(_mask_conn_param, masked)
    masked = _NAME_LITERAL_RE.sub(_mask_name_literal, masked)
    return _BEARER_RE.sub(rf"\g<1>{_MASK}", masked)


def redact_and_cap(text: str | None, max_chars: int) -> str | None:
    """Redact, THEN cap — the shape every stored failure detail wants, in one place.

    THE ORDER IS THE POINT, which is why this is a function rather than a line each
    caller writes for itself: capping first can slice a credential in half and leave its
    recognizable prefix behind, and nothing downstream can un-leak it. Both pipelines
    that store an operator-grade detail (the deploy pipeline and the classification
    review runner) keep their own ceiling — how much diagnostic is worth storing is
    theirs to decide — and share this rule.

    Empty or absent text answers None: "nothing to say" is a state, not an empty string
    that reads as a detail nobody wrote.
    """
    if not text:
        return None
    return redact_secrets(text)[:max_chars]
