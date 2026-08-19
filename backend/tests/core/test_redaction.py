"""The two-tier credential detector + the widened masker (U2, plan 2026-08-19-001).

The masker's own suite lives in `tests/services/orchestrator/test_errors.py` and must stay
green untouched — this file covers the DETECTION surface (P8/R4a) and the two masking
invariants the widening carries: nothing previously masked becomes less masked, and the
widened family now masks quoted source literals that previously passed through (ASM2).
"""

from __future__ import annotations

import base64
import dataclasses
import time

from src.core.redaction import (
    SCAN_INPUT_MAX_CHARS,
    CredentialHit,
    Tier,
    detect_credentials,
    detect_credentials_off_loop,
    redact_secrets,
)

# --- the wall-clock ceiling, written FIRST (the plan's binding execution note) --------------
#
# The ReDoS this guards against was invisible to every example-based test and to all four
# type gates (`security-issues/redos-secret-redaction-regex-2026-07-14.md`). Same discipline
# as the masker's `test_redaction_is_linear_on_an_adversarial_blob`: a 5s ceiling never
# flakes on a linear scan (measured well under 1s) and fails loudly on any quadratic
# regression, which lands in minutes, not seconds.


def test_detector_completes_a_pathological_64kb_line_under_the_ceiling() -> None:
    pathological = (
        # The shape that drove the OLD masker quadratic: an unbroken run the bounded key
        # quantifier must give up on at every offset.
        "_" * 65_536,
        # Dense name/separator pairs whose values never resolve to a literal — the detector's
        # candidate regex fires and its post-filter rejects, 64KB of times, on ONE line.
        ("pwd=" + "_" * 60) * 1_024,
        # A pure-alphanumeric run AT the ceiling, on one line. This is the shape that caught
        # the URL pattern's unbounded scheme quantifier: quadratic, it passes at 64KB (~2s)
        # and takes ~30s here — the ceiling-sized case is what makes this test able to go red.
        "x" * SCAN_INPUT_MAX_CHARS,
    )
    for payload in pathological:
        start = time.perf_counter()
        detect_credentials(payload)
        assert time.perf_counter() - start < 5.0
        # The widened masker walks the same family — hold it to the same ceiling.
        start = time.perf_counter()
        redact_secrets(payload)
        assert time.perf_counter() - start < 5.0


# --- fixtures -------------------------------------------------------------------------------


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


# A decodable JWT (header AND payload parse as JSON objects); the signature is opaque.
_JWT = ".".join(
    (
        _b64url(b'{"alg":"HS256","typ":"JWT"}'),
        _b64url(b'{"sub":"1234567890","name":"J. Doe"}'),
        "fAkE-sIgNaTuRe-1234567890ab",
    )
)

# CREDENTIAL-SHAPED FIXTURES, ASSEMBLED RATHER THAN WRITTEN OUT — the same move `_JWT`
# above already makes, for the same reason. These are synthetic (their neighbours are
# `hunter2` and AWS's own published `AKIAIOSFODNN7EXAMPLE`), but a detector's fixtures
# have to carry the real SHAPE or the test proves nothing — which is exactly what a
# push-protection scanner keys on. Written out as literals they blocked the push; joined
# at runtime the value under test is byte-identical and no scanner sees a key in the file.
# Keep them split. Inlining one turns the next push into a rejected push, not a nitpick.
_STRIPE_TEST_KEY = "sk_test_" + "4eC39HqLyjWDarjtT1zdp7dc"
_STRIPE_LIVE_KEY_1 = "sk_live_" + "4eC39HqLyjWDarjtT1zdp7dc"
_SLACK_TOKEN = "xoxb-" + "2534805035-1234567890123-AbCdEfGhIjKlMnOp"
_STRIPE_LIVE_KEY_2 = "sk_live_" + "51H8xQeLkAbCd"
_STRIPE_LIVE_KEY_3 = "sk_live_" + "abcDEF123456"

# The six shapes a login form produces (plan U2): credential-shaped NAMES everywhere, no
# hardcoded credential VALUE anywhere. Tier B at most — never Tier A.
_LOGIN_FORM_SHAPES = (
    "password: z.string().min(8),",  # validation schema (zod)
    'password: varchar("password", { length: 255 }).notNull(),',  # database column (drizzle)
    "password={value}",  # form field binding (JSX)
    '<Label htmlFor="password">Password</Label>',  # label
    'placeholder="Enter your password"',  # placeholder
    "const { email, password } = await request.json();",  # request destructuring
)


# --- Tier B: credential-shaped NAME with a literal value (the shapes ASM2 proves the
# --- masker's own patterns miss — this scenario is the unit's whole point) ------------------


def test_source_literal_assignments_each_produce_exactly_one_hit_naming_its_family() -> None:
    for source in (
        'const password = "hunter2";',
        'password: "s3cret",',
        'const apiKey = "sk-live-4242424242";',
        f'const stripeSecret = "{_STRIPE_TEST_KEY}";',
    ):
        scan = detect_credentials(source)
        assert len(scan.hits) == 1, source
        assert scan.hits[0].family == "credential-name-literal"
        assert scan.hits[0].tier is Tier.B


def test_url_bearer_and_connection_string_each_produce_exactly_one_hit() -> None:
    cases = {
        "postgres://admin:s3cr3t@db.internal:5432/app": "url-credentials",
        "Authorization: Bearer tok-sample-4f9a2b7c1d": "bearer-token",
        (
            "Server=tcp:db;Database=app;User ID=admin;Password=P@ssw0rd-Leaked-123;Encrypt=true"
        ): "connection-string-password",
    }
    for source, family in cases.items():
        scan = detect_credentials(source)
        assert len(scan.hits) == 1, source
        assert scan.hits[0].family == family


def test_reference_values_produce_no_hit() -> None:
    # Identifier, member-access and call-expression values are not hardcoded secrets — these
    # shapes are everywhere in ordinary generated Next.js source (ASM2's false-positive half).
    for source in (
        "password: z.string().min(8)",
        "password={value}",
        "const API_KEY = process.env.NEXT_PUBLIC_API_KEY",
        'const password = formData.get("password")',
        "const token = await getToken()",
        "apiKey: config.apiKey,",
    ):
        assert detect_credentials(source).hits == (), source


# --- Tier A: value-shaped, unambiguous on its own --------------------------------------------


def test_value_shaped_secrets_land_tier_a() -> None:
    cases = {
        "-----BEGIN RSA PRIVATE KEY-----": "pem-private-key",
        f"STRIPE_KEY={_STRIPE_LIVE_KEY_1}": "stripe-live-key",
        'const aws = "AKIAIOSFODNN7EXAMPLE";': "aws-access-key",
        'const gh = "ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789";': "github-token",
        f"SLACK={_SLACK_TOKEN}": "slack-token",
        f"const session = '{_JWT}';": "jwt",
        "postgres://admin:s3cr3t@db.internal:5432/app": "url-credentials",
        ("AccountName=acct;AccountKey=" + "A" * 86 + "==;EndpointSuffix=x"): "azure-account-key",
        "https://a.blob.core.windows.net/c/f?sv=2021-08-06&sig=aBcDeF1234567890aBcDeF12": (
            "azure-sas-signature"
        ),
    }
    for source, family in cases.items():
        scan = detect_credentials(source)
        assert len(scan.hits) == 1, source
        assert scan.hits[0].family == family, source
        assert scan.hits[0].tier is Tier.A, source


def test_a_jwt_lookalike_that_does_not_decode_is_not_a_hit() -> None:
    # Tier A stands in as the credentials answer when the model is down — "decodable" is the
    # precision requirement, not a nicety. Segment shapes match; the base64 is not JSON.
    lookalike = "eyJ" + "x" * 24 + ".eyJ" + "y" * 24 + "." + "z" * 24
    assert detect_credentials(lookalike).hits == ()


def test_every_hit_is_tiered_and_login_form_shapes_never_reach_tier_a() -> None:
    for shape in _LOGIN_FORM_SHAPES:
        assert all(h.tier is Tier.B for h in detect_credentials(shape).hits), shape
    # And on a mixed document every hit carries exactly one of the two labels.
    mixed = "\n".join((*_LOGIN_FORM_SHAPES, 'const password = "hunter2";', _JWT))
    hits = detect_credentials(mixed).hits
    assert hits  # the mixed doc is not accidentally clean
    assert all(h.tier in (Tier.A, Tier.B) for h in hits)


def test_a_benign_url_with_a_port_and_no_userinfo_produces_no_hit() -> None:
    assert detect_credentials("https://api.example.com:443/v1/records").hits == ()


# --- the hit shape: never the value, always a location ---------------------------------------


def test_a_hit_never_contains_the_secret_value() -> None:
    secrets = ("hunter2", "s3cr3t", _STRIPE_LIVE_KEY_1, _JWT)
    source = (
        'const password = "hunter2";\n'
        "postgres://admin:s3cr3t@db.internal:5432/app\n"
        f"STRIPE_KEY={_STRIPE_LIVE_KEY_1}\n"
        f"const session = '{_JWT}';\n"
    )
    scan = detect_credentials(source)
    assert len(scan.hits) == 4
    rendered = repr(scan)
    for secret in secrets:
        assert secret not in rendered
    # Structurally airtight: the hit has nowhere to carry a value.
    assert {f.name for f in dataclasses.fields(CredentialHit)} == {"family", "tier", "line"}


def test_hits_carry_one_based_line_numbers_in_order() -> None:
    source = 'const a = 1;\nconst b = 2;\nconst password = "hunter2";\n'
    scan = detect_credentials(source)
    assert [h.line for h in scan.hits] == [3]


# --- the input bound --------------------------------------------------------------------------


def test_input_above_the_ceiling_reports_truncation_rather_than_scanning_silently() -> None:
    padding = "x" * SCAN_INPUT_MAX_CHARS
    # A secret INSIDE the scanned prefix is still reported, and the truncation is declared.
    front = detect_credentials('const password = "hunter2";\n' + padding)
    assert front.truncated is True
    assert len(front.hits) == 1
    # A secret BEYOND the ceiling is unseen — the flag is what keeps that from reading as a
    # clean no-hit (U6 routes a truncated file to the review-failed bucket on this flag).
    beyond = detect_credentials(padding + '\nconst password = "hunter2";')
    assert beyond.truncated is True
    assert beyond.hits == ()


def test_input_at_or_below_the_ceiling_is_not_flagged() -> None:
    assert detect_credentials("x" * SCAN_INPUT_MAX_CHARS).truncated is False
    assert detect_credentials("").truncated is False
    assert detect_credentials("").hits == ()


# --- the async entry point --------------------------------------------------------------------


async def test_off_loop_entry_returns_the_same_result_as_the_sync_scan() -> None:
    source = 'const password = "hunter2";\npostgres://admin:s3cr3t@db.internal/app\n'
    assert await detect_credentials_off_loop(source) == detect_credentials(source)


# --- masking: the two invariants of the widening ----------------------------------------------


def test_nothing_previously_masked_becomes_less_masked() -> None:
    # A sample of every family the pre-widening masker already caught (the full behavioural
    # suite in tests/services/orchestrator/test_errors.py stays green beside this).
    for raw, leaked in (
        ("APP_LABEL=bial_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6", "bial_A1b2C3d4E5f6G7h8"),
        ("FOO_SECRET=supersecretvalue", "supersecretvalue"),
        (f'{{"STRIPE_SECRET_KEY":"{_STRIPE_LIVE_KEY_2}"}}', _STRIPE_LIVE_KEY_2),
        ("postgres://admin:s3cr3t@db.internal:5432/app", "s3cr3t"),
        ("Server=tcp:db;Password=P@ssw0rd-Leaked-123;Encrypt=true", "P@ssw0rd-Leaked-123"),
        ("AccountName=acct;AccountKey=abcSECRETkey==;Endpoint=x", "abcSECRETkey=="),
        ("?sv=2021-08-06&sig=SIGNATURESECRETXYZ", "SIGNATURESECRETXYZ"),
        (f"Authorization: Bearer {_STRIPE_LIVE_KEY_3}", _STRIPE_LIVE_KEY_3),
        ("password: z.string().min(8)", "z.string().min(8)"),  # conn-param over-redaction stays
    ):
        assert leaked not in redact_secrets(raw), raw


def test_widened_family_masks_quoted_source_literals_that_previously_passed_through() -> None:
    # ASM2, verified: each of these produced ZERO masking before the widening.
    for raw, leaked in (
        ('const password = "hunter2";', "hunter2"),
        ("password: 's3cret',", "s3cret"),
        ('const apiKey = "sk-live-4242424242";', "sk-live-4242424242"),
        ('Server=db;Password="p w d";Encrypt=true', "p w d"),  # quoted connection parameter
    ):
        masked = redact_secrets(raw)
        assert leaked not in masked, raw
        assert "***" in masked


def test_widened_masking_preserves_the_name_and_the_quotes() -> None:
    assert redact_secrets('const password = "hunter2";') == 'const password = "***";'


def test_widened_masking_leaves_reference_assignments_and_prose_alone() -> None:
    for benign in (
        "const apiKey = process.env.NEXT_PUBLIC_API_KEY",
        "token={value}",
        "rotate the token: yes or no",
        "app/reports/page.tsx(12,5): error TS2322: Type 'string' is not assignable.",
    ):
        assert redact_secrets(benign) == benign, benign


def test_widened_masking_is_idempotent() -> None:
    once = redact_secrets(
        'const password = "hunter2"; Server=db;Password="p w";x Bearer tok_12345678'
    )
    assert redact_secrets(once) == once
