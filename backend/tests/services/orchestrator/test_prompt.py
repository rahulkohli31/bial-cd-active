"""The open-sandbox system prompt + repair template (U3) — a cheap, COARSE guard against prompt
drift on the load-bearing bits (R18): the injected ENV, the don't-restart-dev-server rule, the SAS
server-side rule, and the real-data-only rule (R4). Prompt copy is not behavioral, so the
assertions stay loose to avoid brittleness."""

from __future__ import annotations

import re
from pathlib import Path

from src.api.v1.build_sessions.schemas import BuildError, ErrorSource
from src.services.orchestrator.prompt import BUILD_SYSTEM_PROMPT, build_repair_prompt

# Repo-root/sandbox/template — the hand-maintained golden template the manifest mirrors (KD-10).
# test file: backend/tests/services/orchestrator/test_prompt.py → parents[4] is the repo root.
_TEMPLATE_ROOT = Path(__file__).resolve().parents[4] / "sandbox" / "template"


def test_system_prompt_reflects_the_open_sandbox_model() -> None:
    prompt = BUILD_SYSTEM_PROMPT
    lowered = prompt.lower()
    # Retired constrained-model language is gone (R18).
    assert "no shell or command access" not in lowered
    assert "never run `npm install`" not in lowered
    assert "single swappable module" not in lowered
    # The open model is documented: a real shell + on-demand install + the new tool.
    assert "run_command" in prompt
    assert "npm install" in prompt  # now a capability, not a prohibition
    # The injected app ENV the model writes its own data/storage code against (R20).
    for name in (
        "BIAL_APP_ID",
        "BIAL_APP_CREDENTIAL",
        "BIAL_DATA_BASE_URL",
        "BIAL_BLOB_CONTAINER_URL",
        "BIAL_BLOB_SAS",
    ):
        assert name in prompt
    # The dev server must still NOT be restarted (load-bearing for the harness verify).
    assert "already running" in lowered and "restart" in lowered
    # The write-capable SAS is flagged server-side-only (R13/R14).
    assert "server-side" in lowered
    assert "declare_done" in prompt


def test_system_prompt_forbids_seeded_dummy_data() -> None:
    """R4 — the build agent must never seed invented records; it builds honest empty/loading/error
    states and lets real data arrive by upload or user entry. This rule existed in the POC prompt,
    was lost in the open-sandbox rewrite, and is a client-collateral promise."""
    lowered = BUILD_SYSTEM_PROMPT.lower()
    assert "data integrity" in lowered
    # The prohibition names the whole family of invented-record words the model reaches for.
    for banned in ("dummy", "sample", "fake", "mock", "placeholder"):
        assert banned in lowered, f"the rule should name {banned!r} records explicitly"
    assert "never hardcode, seed, or generate" in lowered
    # The prescribed alternative: honest states, real data by upload or entry.
    assert "empty state" in lowered
    assert "loading state" in lowered
    assert "error state" in lowered
    assert "uploads it" in lowered and "enters it" in lowered


def test_system_prompt_carries_the_generated_app_quality_rules() -> None:
    """U1/U11 (#46/#47/#45) — the additive rules the generated apps inherit: AFTER A WRITE (the
    user sees their own mutation without a reload), HONEST UI (no false "live"/"shared" claims
    without a real refetch), REMOVE SCAFFOLDING (ship only the requested feature), and RESPONSIVE
    (no horizontal overflow at 390px). Coarse marker check — the copy is a probabilistic nudge,
    not a behavioral contract, so assert the load-bearing phrases only."""
    lowered = BUILD_SYSTEM_PROMPT.lower()
    # AFTER A WRITE (U11): the unconditional own-mutation refetch, hoisted out of HONEST UI.
    assert "after a write" in lowered
    # HONEST UI (#46): names the no-realtime reality and the required refetch remedy.
    assert "honest ui" in lowered
    assert "no realtime channel" in lowered
    assert "refetch" in lowered
    # REMOVE SCAFFOLDING (#47): the model must ship only what the user asked for.
    assert "remove scaffolding" in lowered
    # RESPONSIVE (#45): the concrete phone-width target, not a vague "make it responsive".
    assert "responsive" in lowered
    assert "390px" in lowered


def test_prompt_has_no_stale_app_records_demo_reference() -> None:
    """U11/R16 — the `app/records` demo route was removed from the template (commit d51ebfa), so
    the prompt must no longer tell the model to hunt for and delete it. Only the stale REMOVE
    SCAFFOLDING parenthetical ever referenced it, and it is gone."""
    assert "app/records" not in BUILD_SYSTEM_PROMPT


def test_prompt_keeps_the_live_data_service_records_path() -> None:
    """U11/R16 OVER-DELETION GUARD (matters more than the removal test): `app/records` and the
    live data-service path `/apps/{appId}/records` differ by one character. A careless grep-delete
    of the former breaks the latter — the REST endpoint every generated app writes against. Assert
    the rendered path survives verbatim, which also proves the f-string `${...}` literals came
    through brace-escaping intact."""
    assert "${BIAL_DATA_BASE_URL}/apps/${BIAL_APP_ID}/records" in BUILD_SYSTEM_PROMPT


def test_prompt_makes_no_claim_that_the_starter_ships_demo_routes() -> None:
    """U11/R16 — the template ships only `app/{globals.css,layout.tsx,page.tsx}` plus lib/config,
    NO example or demo routes. REMOVE SCAFFOLDING's old premise ("the starter ships example and
    demo routes") is false and must not send the model hunting scaffolding that does not exist."""
    lowered = BUILD_SYSTEM_PROMPT.lower()
    assert "example and demo routes" not in lowered
    assert "ships example" not in lowered


def test_refetch_after_every_write_appears_exactly_once() -> None:
    """U11/R15 — exactly ONE rule owns the after-write mechanic. The clause was hoisted OUT of
    HONEST UI into the unconditional AFTER A WRITE rule; if a future edit re-adds it to HONEST UI
    (two rules prescribing the same behaviour) this count trips."""
    assert BUILD_SYSTEM_PROMPT.lower().count("refetch after every write") == 1


def test_after_write_requirement_is_unconditional() -> None:
    """U11/R14 — the after-write refetch applies to EVERY app, not only ones that claim liveness.
    It lives in its own AFTER A WRITE rule, above HONEST UI, and is NOT gated behind the
    live/shared/real-time conditional that owns interval/focus refetch."""
    prompt = BUILD_SYSTEM_PROMPT
    after_write = prompt[prompt.index("AFTER A WRITE") : prompt.index("HONEST UI")].lower()
    honest_ui = prompt[prompt.index("HONEST UI") : prompt.index("REMOVE SCAFFOLDING")].lower()
    assert "refetch after every write" in after_write
    # The AFTER A WRITE rule carries no "if your copy claims ..." guard — it is unconditional.
    assert "if your copy" not in after_write
    # And HONEST UI no longer owns the mechanic — it moved out.
    assert "refetch after every write" not in honest_ui


def test_honest_ui_keeps_its_claim_matching_argument() -> None:
    """U11 — HONEST UI keeps ONLY the claim-matching argument: interval and window-focus refetch
    stay CONDITIONAL on actually claiming a view is live/shared/real-time (the price of the
    claim), which is distinct from the unconditional own-mutation rule."""
    prompt = BUILD_SYSTEM_PROMPT
    honest_ui = prompt[prompt.index("HONEST UI") : prompt.index("REMOVE SCAFFOLDING")].lower()
    assert "if your copy" in honest_ui
    assert "interval" in honest_ui
    assert "focus" in honest_ui
    assert "real-time" in honest_ui


def test_every_golden_template_manifest_file_exists() -> None:
    """U11/R16 durable guard: every path the manifest advertises as an editable starting point
    must actually exist under `sandbox/template/`, so a future template change that drops or
    renames a file cannot leave the prompt pointing at a phantom. Walks the manifest text in the
    rendered prompt and stats each path — the `components/ui/*.tsx` glob and the comma-list line
    included."""
    manifest = BUILD_SYSTEM_PROMPT[BUILD_SYSTEM_PROMPT.index("The app starts from a minimal") :]
    tokens = re.findall(r"[\w./*-]+\.(?:tsx|ts|css|json|mjs)", manifest)
    assert tokens, "manifest path extraction found nothing — the regex drifted from the manifest"
    for token in tokens:
        if "*" in token:
            assert list(_TEMPLATE_ROOT.glob(token)), (
                f"manifest glob {token!r} matched no file under {_TEMPLATE_ROOT}"
            )
        else:
            assert (_TEMPLATE_ROOT / token).is_file(), (
                f"manifest lists {token!r} but it is missing from {_TEMPLATE_ROOT}"
            )


def test_system_prompt_never_instructs_the_app_to_authenticate() -> None:
    """Regression guard for the opaque-origin sandbox learning
    (docs/solutions/architecture-patterns/sandboxed-app-auth-session-injection-2026-07-09.md):
    the host owns authentication and injects identity downward. A prompt that tells generated code
    to sign users in produces an in-sandbox login form that can never reach an auth endpoint from
    `origin: null`. The prompt is part of the trust boundary — keep sign-in out of it entirely."""
    lowered = BUILD_SYSTEM_PROMPT.lower()
    for banned in (
        "login",
        "log in",
        "sign in",
        "sign-in",
        "signin",
        "username",
        "password",
        "authenticate",
        "authentication",
    ):
        assert banned not in lowered, f"the prompt must not instruct the app about {banned!r}"


def test_repair_prompt_embeds_the_redacted_diagnostic() -> None:
    error = BuildError(
        source=ErrorSource.TSC,
        title="app/records/page.tsx(12,5): error TS2322: Type mismatch.",
        cleaned_stack="app/records/page.tsx(12,5): error TS2322: Type mismatch.",
    )
    repair = build_repair_prompt(error)
    assert "tsc" in repair
    assert error.title in repair
    assert error.cleaned_stack in repair
    assert "declare_done" in repair
    # The retired 'do not run any commands' line is gone — run_command exists now (R18).
    assert "run any commands" not in repair.lower()
