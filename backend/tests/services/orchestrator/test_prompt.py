"""The frozen system prompt + repair template (U1) — a cheap guard against prompt drift on the
load-bearing environment constraints (KD-9)."""

from __future__ import annotations

from src.api.v1.build_sessions.schemas import BuildError, ErrorSource
from src.services.orchestrator.prompt import BUILD_SYSTEM_PROMPT, build_repair_prompt


def test_system_prompt_pins_the_environment_constraints() -> None:
    prompt = BUILD_SYSTEM_PROMPT
    assert "npm install" in prompt  # the "never npm install" rule (KD-9)
    assert "already running" in prompt.lower() and "restart" in prompt.lower()
    assert "@/lib/bial-data" in prompt  # the one data path
    assert "lib/bial-data.ts" in prompt  # the never-edit list
    assert "components/ui" in prompt
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
