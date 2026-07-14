"""The de-noiser + secret redactor (U3, KD-5/KD-8). Redaction is asserted on the structured-error
egress path here; `test_progress` asserts it on the raw-log egress path."""

from __future__ import annotations

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
