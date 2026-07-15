"""The open-sandbox system prompt + repair template (U3) — a cheap, COARSE guard against prompt
drift on the load-bearing bits (R18): the injected ENV, the don't-restart-dev-server rule, and the
SAS server-side rule. Prompt copy is not behavioral, so the assertions stay loose to avoid
brittleness."""

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
