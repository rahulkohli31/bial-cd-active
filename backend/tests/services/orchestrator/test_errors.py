"""The de-noiser + secret redactor (U3, KD-5/KD-8). Redaction is asserted on the structured-error
egress path here; `test_progress` asserts it on the raw-log egress path."""

from __future__ import annotations

import time

from src.api.v1.build_sessions.schemas import ErrorSource
from src.services.orchestrator import constants, errors

# A realistic tsc blob: ANSI colour codes + absolute sandbox paths + a real TS error line.
_TSC_RAW = (
    "\x1b[96m/workspace/app/app/records/page.tsx\x1b[0m:\x1b[93m12\x1b[0m:\x1b[93m5\x1b[0m - "
    "\x1b[91merror\x1b[0m\x1b[90m TS2322\x1b[0m: Type 'string' is not assignable "
    "to type 'number'.\n"
    "\n12   count={value}\n"
)

_SERVER_RAW = (
    "Error: bialData.save failed\n"
    "    at RecordsPage (/workspace/app/app/records/page.tsx:84:20)\n"
    "    at renderWithHooks (/workspace/app/node_modules/react-dom/index.js:1:1)\n"
)


def test_tsc_blob_becomes_a_clean_build_error() -> None:
    err = errors.from_tsc(_TSC_RAW)
    assert err.source == ErrorSource.TSC
    assert "error TS2322" in err.title  # the first meaningful error line
    assert "\x1b[" not in err.cleaned_stack  # ANSI stripped
    assert "/workspace/app/" not in err.cleaned_stack  # absolute paths relativized
    assert "app/records/page.tsx" in err.cleaned_stack


def test_server_stderr_becomes_a_clean_build_error() -> None:
    err = errors.from_server(_SERVER_RAW)
    assert err.source == ErrorSource.SERVER
    assert "bialData.save failed" in err.title
    assert "/workspace/" not in err.cleaned_stack


def test_credential_is_redacted_on_the_error_path() -> None:
    raw = "boom while loading config BIAL_APP_CREDENTIAL=bial_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6\n"
    err = errors.from_server(raw)
    assert "bial_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6" not in err.cleaned_stack
    assert "***" in err.cleaned_stack


def test_suffixed_secret_assignment_is_redacted() -> None:
    # Both dotenv `NAME=value` and JS object `NAME: 'value'` forms (console.log(process.env)).
    raw = "env dump: FOO_SECRET=supersecretvalue and { AUTH_TOKEN: 'tok_abc123', DB_KEY: \"k9\" }"
    cleaned = errors.redact_secrets(raw)
    assert "supersecretvalue" not in cleaned
    assert "tok_abc123" not in cleaned
    assert "k9" not in cleaned
    assert "FOO_SECRET" in cleaned  # the name is preserved so the diagnostic still reads
    assert "AUTH_TOKEN" in cleaned


def test_json_quoted_key_form_is_redacted() -> None:
    # JSON.stringify(process.env) / Response.json(process.env) — the quoted-KEY form. Third-party
    # secrets (not just bial_) must not egress verbatim.
    raw = '{"STRIPE_SECRET_KEY":"sk_live_51H8xQeLkAbCd","MAPS_API_KEY":"AIzaSyABCDEF"}'
    cleaned = errors.redact_secrets(raw)
    assert "sk_live_51H8xQeLkAbCd" not in cleaned
    assert "AIzaSyABCDEF" not in cleaned
    assert "STRIPE_SECRET_KEY" in cleaned  # name preserved


def test_multiword_and_pem_values_are_masked_whole() -> None:
    # A value with spaces/newlines must be masked ENTIRELY, not just up to the first space.
    dotenv = errors.redact_secrets("API_SECRET=correct horse battery staple")
    assert "horse battery" not in dotenv
    quoted = errors.redact_secrets("{ SESSION_SECRET: 'two words here' }")
    assert "two words" not in quoted
    pem = errors.redact_secrets(
        "{ PRIVATE_KEY: '-----BEGIN RSA PRIVATE KEY-----\nMIIEvQIB\n-----END-----' }"
    )
    assert "MIIEvQIB" not in pem and "BEGIN RSA" not in pem


def test_redaction_does_not_over_mask_a_benign_diagnostic() -> None:
    # A normal tsc line that merely mentions a symbol must survive unchanged (no false-positive
    # over-redaction that would blind the model to the real error).
    benign = "app/records/page.tsx(12,5): error TS2322: Type 'string' is not assignable."
    assert errors.redact_secrets(benign) == benign


def test_redaction_is_idempotent() -> None:
    once = errors.redact_secrets("key BIAL_APP_CREDENTIAL=bial_abcdefghijklmnopqrstuvwx")
    assert errors.redact_secrets(once) == once


def test_over_length_is_truncated() -> None:
    raw = "error TS1000: boom\n" + ("x" * (constants.CLEANED_STACK_MAX_CHARS + 5000))
    err = errors.from_tsc(raw)
    assert len(err.cleaned_stack) <= constants.CLEANED_STACK_MAX_CHARS + 100  # + marker headroom
    assert "truncated" in err.cleaned_stack


def test_empty_input_is_a_safe_fallback() -> None:
    err = errors.from_tsc("")
    assert err.source == ErrorSource.TSC
    assert err.title  # a defined, non-empty fallback title
    assert err.cleaned_stack == ""


def test_redaction_is_linear_on_an_adversarial_blob() -> None:
    # A long run of `_` that never resolves to a masked suffix drove the OLD unbounded key
    # quantifier into QUADRATIC backtracking — measured ~176s for a 64KB line, a multi-minute
    # event-loop stall (app-controlled stdout is untrusted, KD-5). The bounded key keeps the scan
    # LINEAR. On the old regex 100K chars would take ~7 minutes; the 5s ceiling (with a wide margin
    # for a loaded CI box) fails loudly on any quadratic regression while never flaking on linear.
    payload = "_" * 100_000
    start = time.perf_counter()
    errors.redact_secrets(payload)
    assert time.perf_counter() - start < 5.0


def test_declutter_caps_input_before_redaction() -> None:
    # Even a pathological multi-hundred-KB blob is bounded before the redactor runs.
    err = errors.from_tsc("error TS1: boom\n" + ("A_TOKEN=x " * 200_000))
    assert len(err.cleaned_stack) <= constants.CLEANED_STACK_MAX_CHARS + 100


def test_broadened_credential_families_are_redacted() -> None:
    # Families a narrow `_TOKEN/_SECRET/_KEY` filter misses (the child-env-scrub lesson).
    cleaned = errors.redact_secrets(
        "DB_PASSWORD=hunter2 and { API_CREDENTIAL: 'cred-xyz', AWS_ACCESS_KEY: 'AKIAEXAMPLE' }"
    )
    assert "hunter2" not in cleaned
    assert "cred-xyz" not in cleaned
    assert "AKIAEXAMPLE" not in cleaned
    assert "DB_PASSWORD" in cleaned  # the name is preserved so the diagnostic still reads


def test_url_embedded_credentials_are_masked() -> None:
    cleaned = errors.redact_secrets("connect: postgres://admin:s3cr3t@db.internal:5432/app failed")
    assert "s3cr3t" not in cleaned
    assert "admin" not in cleaned
    assert "postgres://" in cleaned and "@db.internal" in cleaned  # structure preserved


def test_benign_url_without_credentials_survives() -> None:
    # A host:port URL with no userinfo must NOT be over-masked (it carries no secret).
    benign = "fetch failed for https://api.example.com:443/v1/records"
    assert errors.redact_secrets(benign) == benign


def test_bearer_token_is_masked() -> None:
    cleaned = errors.redact_secrets("Authorization: Bearer sk_live_abcDEF123456")
    assert "sk_live_abcDEF123456" not in cleaned
    assert "Bearer ***" in cleaned


def test_broadened_redaction_is_idempotent() -> None:
    once = errors.redact_secrets(
        "DB_PASSWORD=hunter2 url postgres://u:p@h Authorization: Bearer tok_123"
    )
    assert errors.redact_secrets(once) == once
