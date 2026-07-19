"""The open-sandbox system prompt + repair template (U3) — a cheap, COARSE guard against prompt
drift on the load-bearing bits (R18): the injected ENV, the don't-restart-dev-server rule, the SAS
server-side rule, and the real-data-only rule (R4). Prompt copy is not behavioral, so the
assertions stay loose to avoid brittleness."""

from __future__ import annotations

from src.api.v1.build_sessions.schemas import BuildError, ErrorSource
from src.services.orchestrator.prompt import BUILD_SYSTEM_PROMPT, build_repair_prompt


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
    """U1 (#46/#47/#45) — three additive rules the generated apps inherit: HONEST UI (no false
    "live"/"shared" claims without a real refetch), REMOVE SCAFFOLDING (drop the example routes),
    and RESPONSIVE (no horizontal overflow at 390px). Coarse marker check — the copy is a
    probabilistic nudge, not a behavioral contract, so assert the load-bearing phrases only."""
    lowered = BUILD_SYSTEM_PROMPT.lower()
    # HONEST UI (#46): names the no-realtime reality and the required refetch remedy.
    assert "honest ui" in lowered
    assert "no realtime channel" in lowered
    assert "refetch" in lowered
    # REMOVE SCAFFOLDING (#47): pairs with U2's template deletion — the model must strip examples.
    assert "remove scaffolding" in lowered
    assert "app/records" in lowered
    # RESPONSIVE (#45): the concrete phone-width target, not a vague "make it responsive".
    assert "responsive" in lowered
    assert "390px" in lowered


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
