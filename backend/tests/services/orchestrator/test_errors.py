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

# A realistic dev-server stderr tail: the app queried a table its migrations never created —
# the most common runtime failure now that the app owns its own schema through Drizzle.
_SERVER_RAW = (
    'Error: relation "audit_events" does not exist\n'
    "    at ItemsPage (/workspace/app/app/page.tsx:84:20)\n"
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
    assert 'relation "audit_events" does not exist' in err.title
    assert "/workspace/" not in err.cleaned_stack


# --- `next build` (the production build, ACR-side) --------------------------------
#
# The title heuristic is the whole job here: `next build` opens with a banner and progress
# ticks, so a naive "first non-empty line" titles every failure "▲ Next.js 16.2.10".

_NEXT_BUILD_BANNER = (
    "   \x1b[1m▲ Next.js 16.2.10\x1b[0m\n"
    "\n"
    "   Creating an optimized production build ...\n"
    " ✓ Compiled successfully\n"
)


def test_next_build_type_error_titles_on_the_type_error_line() -> None:
    raw = (
        _NEXT_BUILD_BANNER + "   Linting and checking validity of types  ...\n"
        "Failed to compile.\n"
        "\n"
        "./app/records/page.tsx:12:5\n"
        "Type error: Type 'string' is not assignable to type 'number'.\n"
    )
    err = errors.from_next_build(raw)
    assert err.source == ErrorSource.NEXT_BUILD
    assert err.title.startswith("Type error:")
    assert "\x1b[" not in err.cleaned_stack


def test_next_build_prerender_failure_is_titled() -> None:
    """The headline case `tsc --noEmit` cannot see: it type-checks clean and then throws
    while Next renders the page at build time."""
    raw = (
        _NEXT_BUILD_BANNER + "   Generating static pages (0/5)  ...\n"
        'Error occurred prerendering page "/dashboard". '
        "Read more: https://nextjs.org/docs/messages/prerender-error\n"
        "TypeError: Cannot read properties of undefined (reading 'map')\n"
        "    at DashboardPage (/workspace/app/app/dashboard/page.tsx:18:22)\n"
    )
    err = errors.from_next_build(raw)
    assert err.title.startswith("Error occurred prerendering page")
    # Paths are still relativized on this source.
    assert "/workspace/app/" not in err.cleaned_stack
    assert "app/dashboard/page.tsx" in err.cleaned_stack


def test_next_build_missing_suspense_boundary_is_titled() -> None:
    """Has no dedicated marker and no `Failed to compile` header — it survives only
    because the broad `Error:` marker is scanned last."""
    raw = (
        _NEXT_BUILD_BANNER + "   Collecting page data  ...\n"
        'Error: useSearchParams() should be wrapped in a suspense boundary at page "/search". '
        "Read more: https://nextjs.org/docs/messages/missing-suspense-with-csr-bailout\n"
    )
    err = errors.from_next_build(raw)
    assert "useSearchParams()" in err.title
    assert "Next.js 16.2.10" not in err.title


def test_next_build_out_of_memory_is_not_reported_as_a_code_error() -> None:
    """Told "your code has an error", a citizen edits code that is fine. The heap message
    must win the title outright."""
    raw = (
        _NEXT_BUILD_BANNER
        + "<--- Last few GCs --->\n"
        + "FATAL ERROR: Reached heap limit Allocation failed - "
        + "JavaScript heap out of memory\n"
    )
    err = errors.from_next_build(raw)
    assert "JavaScript heap out of memory" in err.title


def test_next_build_falls_back_past_the_banner_when_nothing_matches() -> None:
    raw = _NEXT_BUILD_BANNER + "   Collecting build traces  ...\nsomething unfamiliar broke\n"
    err = errors.from_next_build(raw)
    assert err.title == "something unfamiliar broke"


def test_next_build_secrets_are_redacted_like_every_other_source() -> None:
    raw = _NEXT_BUILD_BANNER + (
        "Failed to compile.\n\n"
        "./app/api/route.ts\n"
        "Type error: cannot connect to "
        "postgresql://appuser:sup3rs3cr3t@db.example.com:5432/app_x\n"
    )
    err = errors.from_next_build(raw)
    assert "sup3rs3cr3t" not in err.cleaned_stack
    assert "sup3rs3cr3t" not in err.title


def test_credential_is_redacted_on_the_error_path() -> None:
    # Any `bial_`-shaped token, wherever it surfaces — the shape is what the redactor keys on,
    # not the variable name (which is why retiring one injected name changed nothing here).
    raw = "boom while loading config APP_LABEL=bial_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6\n"
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
    benign = "app/reports/page.tsx(12,5): error TS2322: Type 'string' is not assignable."
    assert errors.redact_secrets(benign) == benign


def test_redaction_is_idempotent() -> None:
    once = errors.redact_secrets("key APP_LABEL=bial_abcdefghijklmnopqrstuvwx")
    assert errors.redact_secrets(once) == once


def test_over_length_is_truncated() -> None:
    raw = "error TS1000: boom\n" + ("x" * (constants.CLEANED_STACK_MAX_CHARS + 5000))
    err = errors.from_tsc(raw)
    assert len(err.cleaned_stack) <= constants.CLEANED_STACK_MAX_CHARS + 100  # + marker headroom
    assert "truncated" in err.cleaned_stack


def test_an_over_long_title_cuts_at_a_word_boundary_with_a_marker() -> None:
    """U4: the round-3 projector showed '…renders a blank page witho' — a bare [:200] slice
    lands mid-word and reads as a rendering bug. The title is the ONE line the portal's retry
    framing shows, so the cut must land between words and SAY it is a cut."""
    line = ("error: the page " + "renders blank " * 20).strip()  # single line, well over 200
    err = errors.from_server(line)
    assert err.title.endswith("…")
    stem = err.title.removesuffix("…")
    assert line.startswith(stem)
    assert line[len(stem)] == " "  # the cut landed BETWEEN words, never inside one
    assert len(err.title) <= 201  # the 200-char budget plus the marker


def test_a_short_title_is_untouched_by_the_word_boundary_cut() -> None:
    err = errors.from_server("error: something small broke")
    assert err.title == "error: something small broke"


def test_a_long_unbroken_token_still_gets_cut_and_marked() -> None:
    # No space in the first 200 chars (one giant minified identifier): the hard cut stays,
    # but the marker still says the title continues.
    err = errors.from_server("e" * 400)
    assert err.title == "e" * 200 + "…"


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


def test_connection_string_and_sas_params_are_masked() -> None:
    # A connection string packs many `key=value;` pairs on one line, so the assignment pass masks
    # only the first segment — the embedded Password / AccountKey after the first `;` must not leak
    # (the run_command egress-surface gap, R3). A raw SAS query-string has no assignment prefix at
    # all, so its `sig=` must be masked on its own.
    conn = (
        "SQL_CONNECTION_STRING=Server=tcp:db;Database=app;User ID=admin;"
        "Password=P@ssw0rd-Leaked-123;Encrypt=true"
    )
    cleaned = errors.redact_secrets(conn)
    assert "P@ssw0rd-Leaked-123" not in cleaned
    assert "Password=***" in cleaned
    assert "Database=app" in cleaned  # a non-secret param survives

    azure = "AZURE_STORAGE_CONNECTION_STRING=AccountName=acct;AccountKey=abcSECRETkey==;Endpoint=x"
    az_cleaned = errors.redact_secrets(azure)
    assert "abcSECRETkey==" not in az_cleaned
    assert "AccountKey=***" in az_cleaned
    assert "Endpoint=x" in az_cleaned  # a non-secret param after the key survives

    sas = "blob: https://acct.blob.core.windows.net/c/f?sv=2021-08-06&sig=SIGNATURESECRETXYZ"
    sas_cleaned = errors.redact_secrets(sas)
    assert "SIGNATURESECRETXYZ" not in sas_cleaned
    assert "sig=***" in sas_cleaned
    assert "sv=2021-08-06" in sas_cleaned  # a non-secret SAS param survives


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


# --- the TypeScript-CLI diagnostic shape ---------------------------------------------------
#
# A framework minor can turn on its own TypeScript CLI, at which point `next build` shells out to
# the project's tsc and prints the RAW diagnostic — `file(line,col): error TSxxxx: …` — with
# neither a `Type error:` prefix nor a `Failed to compile.` header. Every marker above misses it,
# so the fallback picks the newest non-noise line and the citizen's failure is titled
# "Running TypeScript ..." — a progress spinner. The self-heal model reads the same field, so it
# is handed the spinner and tries to repair the app from it.
#
# These pin the fix ahead of that move rather than after it. They fail against the pre-patch
# marker table, which is the point.

_TS_CLI_BUILD = (
    "   \x1b[1m▲ Next.js 16.3.0 (Turbopack)\x1b[0m\n"
    " ✓ Running next.config.ts took 4.3s\n"
    "\n"
    "   Creating an optimized production build ...\n"
    " ✓ Compiled successfully in 7.4s\n"
    "   Running TypeScript ...\n"
    "\n"
    "We detected TypeScript in your project and reconfigured your tsconfig.json file for you.\n"
    "The following mandatory changes were made:\n"
    "- jsx was set to react-jsx\n"
    "\n"
    "components/booking-form.tsx(2,9): error TS2322: "
    "Type 'string' is not assignable to type 'number'.\n"
    "Failed to type check.\n"
)


def test_a_raw_tsc_diagnostic_is_titled_on_the_error_not_on_the_spinner() -> None:
    err = errors.from_next_build(_TS_CLI_BUILD)

    assert err.source == ErrorSource.NEXT_BUILD
    assert "error TS2322" in err.title, (
        "the build failure must be titled on the compiler diagnostic; got: " + err.title
    )
    # The exact regression: a progress line must never become the title.
    assert not err.title.startswith("Running TypeScript")
    assert "Failed to type check" not in err.title


def test_a_real_diagnostic_outranks_an_incidental_typeerror_elsewhere_in_the_log() -> None:
    """Marker PRECEDENCE, not merely presence.

    Without this, an `error TS` marker placed below `TypeError:` in the table passes every other
    test in this file while still mistitling any build whose log happens to mention a TypeError —
    and application logs mention TypeError constantly.
    """
    raw = _TS_CLI_BUILD + "TypeError: something unrelated in a downstream log line\n"

    err = errors.from_next_build(raw)

    assert "error TS2322" in err.title
    assert not err.title.startswith("TypeError:")


def test_a_missing_module_reported_as_a_tsc_diagnostic_is_still_titled() -> None:
    """The same shape carries import failures, which are the most common thing a generated app
    gets wrong. Before the patch this titled on the spinner too."""
    raw = (
        "   \x1b[1m▲ Next.js 16.3.0 (Turbopack)\x1b[0m\n"
        "   Creating an optimized production build ...\n"
        " ✓ Compiled successfully in 5.1s\n"
        "   Running TypeScript ...\n"
        "components/ui/chart.tsx(4,23): error TS2307: Cannot find module 'recharts'.\n"
        "Failed to type check.\n"
    )

    err = errors.from_next_build(raw)

    assert "error TS2307" in err.title
    assert "recharts" in err.title


def test_the_patch_does_not_retitle_the_shape_the_current_framework_prints() -> None:
    """Version-agnostic, verified rather than assumed: the marker must change nothing about the
    output the framework shipped today, or this becomes a behaviour change disguised as a fix."""
    raw = (
        _NEXT_BUILD_BANNER + "   Linting and checking validity of types  ...\n"
        "Failed to compile.\n"
        "\n"
        "./app/records/page.tsx:12:5\n"
        "Type error: Type 'string' is not assignable to type 'number'.\n"
    )

    err = errors.from_next_build(raw)

    assert err.title.startswith("Type error:")


def test_the_typescript_phase_chatter_never_becomes_a_title_via_the_fallback() -> None:
    """Pins the NOISE half of the patch, which the marker half does not cover.

    Established by mutation: with the `error TS` marker present, removing the TypeScript-phase
    noise prefixes breaks nothing — every diagnostic-shaped failure is caught by the marker
    first. The prefixes only earn their place on the FALLBACK path, i.e. a build that fails with
    no recognised marker anywhere. That is reachable: a TypeScript phase can fail without
    emitting an `error TSxxxx` line at all (a config fault, an OOM inside the checker, a crash).

    Without the prefixes, the newest non-noise line in that case is the spinner, and the citizen
    is told their build failed because "Running TypeScript ...".
    """
    raw = (
        "   \x1b[1m▲ Next.js 16.3.0 (Turbopack)\x1b[0m\n"
        "   Creating an optimized production build ...\n"
        " ✓ Compiled successfully in 5.1s\n"
        "We detected TypeScript in your project and reconfigured your tsconfig.json file for you.\n"
        "The following mandatory changes were made:\n"
        "- jsx was set to react-jsx\n"
        "   Running TypeScript ...\n"
        "Finished TypeScript in 1074ms\n"
        "Failed to type check.\n"
    )

    err = errors.from_next_build(raw)

    for chatter in (
        "Running TypeScript",
        "Finished TypeScript",
        "We detected TypeScript",
        "The following mandatory changes",
        "- jsx was set to",
        "Failed to type check",
    ):
        assert not err.title.startswith(chatter), (
            f"progress chatter became the failure title: {err.title!r}"
        )
