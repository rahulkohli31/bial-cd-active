"""The classification review agent (U5): schema discipline, settings, prompt split, and
the snapshot-only toolset — all under scripted models, never a live call."""

from __future__ import annotations

import dataclasses
import uuid
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from pydantic_ai import models
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    RetryPromptPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.anthropic import AnthropicModelSettings
from pydantic_ai.models.function import AgentInfo, FunctionModel

from src.core.redaction import CredentialHit, Tier
from src.services.agent.read_tools import ExtractedSnapshotWorkspace
from src.services.classification.agent import (
    OUTPUT_TOOL_NAME,
    ReviewDeps,
    ThinkingEnabledError,
    ensure_thinking_off,
    review_agent,
    review_model_settings,
    run_review,
)
from src.services.classification.constants import LISTING_MAX_FILES, MAX_TOKENS
from src.services.classification.prompts import (
    REVIEW_INSTRUCTIONS,
    LocatedHit,
    build_review_prompt,
    format_scan_hits,
)
from src.services.classification.schema import (
    Completeness,
    QuestionVerdict,
    ReviewOutput,
    Verdict,
)
from src.services.deploy.classification import CLASSIFICATION_KEYS


@pytest.fixture(autouse=True)
def _no_live_model():
    # Same guard as tests/services/agent: an accidental real model call fails loudly.
    previous = models.ALLOW_MODEL_REQUESTS
    models.ALLOW_MODEL_REQUESTS = False
    yield
    models.ALLOW_MODEL_REQUESTS = previous


# ---------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------


def _tree(tmp_path: Path) -> Path:
    """A minimal 'extracted snapshot' the workspace can really read."""
    root = tmp_path / "extract"
    (root / "app").mkdir(parents=True)
    (root / "app" / "page.tsx").write_text("export default () => <div>VISITOR-LOG</div>\n")
    (root / "app" / "db.ts").write_text('const conn = "server=x"  // fixture\n')
    return root


def _question(key: str, verdict: str = "no", **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "key": key,
        "evidence": [],
        "reason": "Nothing of this kind was found in the app.",
        "verdict": verdict,
    }
    payload.update(overrides)
    return payload


def _six_questions() -> list[dict[str, Any]]:
    return [_question(key) for key in CLASSIFICATION_KEYS]


def _output_response(args: dict[str, Any]) -> ModelResponse:
    return ModelResponse(parts=[ToolCallPart(OUTPUT_TOOL_NAME, args)])


def _scripted(args: dict[str, Any]) -> FunctionModel:
    """A model that always answers with one output-tool call carrying `args`."""

    async def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return _output_response(args)

    return FunctionModel(respond)


def _all_request_text(messages: list[ModelMessage]) -> str:
    """Every user-prompt string across the model input (the volatile prompt lives here)."""
    out: list[str] = []
    for message in messages:
        for part in getattr(message, "parts", []):
            if isinstance(part, UserPromptPart) and isinstance(part.content, str):
                out.append(part.content)
    return "\n".join(out)


# ---------------------------------------------------------------------------------------
# Schema discipline
# ---------------------------------------------------------------------------------------


def test_field_order_is_evidence_then_reason_then_verdict() -> None:
    # The order the model produces IS the declaration order — a verdict-first schema
    # would yield post-hoc justification, so this ordering is pinned as load-bearing.
    props = list(QuestionVerdict.model_json_schema()["properties"])
    assert props == ["key", "evidence", "reason", "verdict", "agreed_with_scan"]


def test_unanswered_with_a_reason_is_accepted() -> None:
    # R5's abstention shape: a returned `unanswered` carrying its reason is valid.
    output = ReviewOutput.model_validate(
        {
            "completeness": "complete",
            "questions": [
                _question(key, verdict="unanswered", reason="Could not determine this.")
                for key in CLASSIFICATION_KEYS
            ],
        }
    )
    assert all(question.verdict is Verdict.UNANSWERED for question in output.questions)
    assert output.completeness is Completeness.COMPLETE


def test_missing_question_is_rejected_as_incomplete_not_defaulted_to_no() -> None:
    five = [_question(key) for key in CLASSIFICATION_KEYS if key != "health_data"]
    with pytest.raises(ValidationError, match="health_data"):
        ReviewOutput.model_validate({"completeness": "complete", "questions": five})


def test_duplicate_question_is_rejected() -> None:
    questions = _six_questions()
    questions[1] = _question("credentials_secrets")  # a second credentials, no health_data
    with pytest.raises(ValidationError, match="duplicated|missing"):
        ReviewOutput.model_validate({"completeness": "complete", "questions": questions})


def test_unknown_question_key_is_rejected() -> None:
    with pytest.raises(ValidationError, match="unknown question key"):
        QuestionVerdict.model_validate(_question("favourite_colour"))


def test_questions_normalise_into_questionnaire_order() -> None:
    output = ReviewOutput.model_validate(
        {"completeness": "complete", "questions": list(reversed(_six_questions()))}
    )
    assert [question.key for question in output.questions] == list(CLASSIFICATION_KEYS)


# ---------------------------------------------------------------------------------------
# Settings: the load-bearing block and the thinking-off guard
# ---------------------------------------------------------------------------------------


def test_max_tokens_is_explicit_not_inherited() -> None:
    settings = review_model_settings()
    # The framework's 4096 default sits on top of the pathological six-answer case.
    assert settings.get("max_tokens") == MAX_TOKENS == 8_000


def test_three_cache_breakpoints_at_the_one_hour_tier() -> None:
    settings = review_model_settings()
    assert settings.get("anthropic_cache_instructions") == "1h"
    assert settings.get("anthropic_cache_tool_definitions") == "1h"
    assert settings.get("anthropic_cache") == "1h"


def test_effort_is_explicitly_low() -> None:
    assert review_model_settings().get("anthropic_effort") == "low"


def test_thinking_enabled_is_rejected_by_the_runtime_guard() -> None:
    for block in (
        AnthropicModelSettings(anthropic_thinking={"type": "enabled", "budget_tokens": 2048}),
        AnthropicModelSettings(anthropic_thinking={"type": "adaptive"}),
        AnthropicModelSettings(thinking=True),
        AnthropicModelSettings(thinking="low"),
    ):
        with pytest.raises(ThinkingEnabledError):
            ensure_thinking_off(block)
    # Explicitly disabled, and effort levels that honour thinking-off, pass.
    ensure_thinking_off(AnthropicModelSettings(anthropic_thinking={"type": "disabled"}))
    ensure_thinking_off(AnthropicModelSettings(anthropic_effort="low"))
    ensure_thinking_off(AnthropicModelSettings(anthropic_effort="high"))


def test_effort_above_high_is_rejected_because_it_reenables_thinking() -> None:
    for effort in ("xhigh", "max"):
        with pytest.raises(ThinkingEnabledError):
            ensure_thinking_off(AnthropicModelSettings(anthropic_effort=effort))


async def test_run_review_refuses_a_thinking_enabling_override(tmp_path: Path) -> None:
    async def must_not_run(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise AssertionError("the model must never be called with thinking enabled")

    with pytest.raises(ThinkingEnabledError):
        await run_review(
            model=FunctionModel(must_not_run),
            user_id=uuid.uuid7(),
            snapshot_root=_tree(tmp_path),
            model_settings=AnthropicModelSettings(anthropic_effort="max"),
        )


# ---------------------------------------------------------------------------------------
# The agent under scripted models
# ---------------------------------------------------------------------------------------


def test_importable_and_constructed_with_no_foundry_config() -> None:
    # The module was imported at the top of this file with no FOUNDRY__* configured;
    # the agent binds NO model — one is passed per run.
    assert review_agent.model is None


async def test_six_well_formed_verdicts_parse_in_questionnaire_order(tmp_path: Path) -> None:
    shuffled = list(reversed(_six_questions()))  # credentials_secrets now sits LAST
    shuffled[-1] = _question(
        "credentials_secrets",
        verdict="yes",
        evidence=[{"path": "app/db.ts", "kind": "hardcoded-value"}],
        reason="The app stores a fixed access credential inside its own code.",
        agreed_with_scan=True,
    )
    result = await run_review(
        model=_scripted({"completeness": "complete", "questions": shuffled}),
        user_id=uuid.uuid7(),
        snapshot_root=_tree(tmp_path),
    )
    output = result.output
    assert isinstance(output, ReviewOutput)
    assert [question.key for question in output.questions] == list(CLASSIFICATION_KEYS)
    credentials = output.questions[0]
    assert credentials.verdict is Verdict.YES
    assert credentials.agreed_with_scan is True
    assert credentials.evidence[0].path == "app/db.ts"


async def test_malformed_output_is_a_parse_failure_not_an_empty_review(tmp_path: Path) -> None:
    # The scripted model never produces a valid review; the run must FAIL, not return
    # an empty answer set.
    with pytest.raises(UnexpectedModelBehavior):
        await run_review(
            model=_scripted({"completeness": "complete", "questions": "not-a-list"}),
            user_id=uuid.uuid7(),
            snapshot_root=_tree(tmp_path),
        )


async def test_missing_question_surfaces_to_the_model_as_incomplete(tmp_path: Path) -> None:
    # Agent-level twin of the schema test: a five-question response draws a retry that
    # NAMES the missing key (never a silent No), and a model that persists fails the run.
    retry_texts: list[str] = []

    async def five_answers(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        for message in messages:
            for part in getattr(message, "parts", []):
                if isinstance(part, RetryPromptPart):
                    retry_texts.append(str(part.content))
        return _output_response(
            {
                "completeness": "complete",
                "questions": [_question(k) for k in CLASSIFICATION_KEYS if k != "health_data"],
            }
        )

    with pytest.raises(UnexpectedModelBehavior):
        await run_review(
            model=FunctionModel(five_answers),
            user_id=uuid.uuid7(),
            snapshot_root=_tree(tmp_path),
        )
    assert any("health_data" in text for text in retry_texts)


async def test_static_instructions_ahead_of_volatile_content(tmp_path: Path) -> None:
    # The static half (rubric + discipline) rides `instructions` byte-identically; the
    # app-specific half (listing + scan hits) rides the user prompt. App content above
    # the breakpoints would destroy the platform-wide cache hit.
    captured: dict[str, Any] = {}

    async def capture(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        captured["instructions"] = info.instructions
        captured["settings"] = info.model_settings
        captured["request_text"] = _all_request_text(messages)
        return _output_response({"completeness": "complete", "questions": _six_questions()})

    hit = LocatedHit(
        path="app/db.ts", hit=CredentialHit(family="fixture-family", tier=Tier.A, line=1)
    )
    await run_review(
        model=FunctionModel(capture),
        user_id=uuid.uuid7(),
        snapshot_root=_tree(tmp_path),
        scan_hits=[hit],
    )
    assert captured["instructions"] == REVIEW_INSTRUCTIONS  # byte-identical, never composed
    # The volatile content is BELOW the breakpoints (user prompt), and only there.
    assert "app/page.tsx" in captured["request_text"]
    assert "fixture-family" in captured["request_text"]
    assert "app/page.tsx" not in REVIEW_INSTRUCTIONS
    assert "fixture-family" not in REVIEW_INSTRUCTIONS
    # And the run really carried the settings block.
    settings = captured["settings"]
    assert settings["max_tokens"] == MAX_TOKENS
    assert settings["anthropic_cache_instructions"] == "1h"
    assert settings["anthropic_cache_tool_definitions"] == "1h"
    assert settings["anthropic_cache"] == "1h"
    assert settings["anthropic_effort"] == "low"


async def test_toolset_resolves_a_real_snapshot_and_cannot_reach_a_sandbox(
    tmp_path: Path,
) -> None:
    # An escape attempt is refused, a real read lands inside the extraction, and the
    # deps STRUCTURALLY carry no sandbox to reach.
    (tmp_path / "outside.txt").write_text("OUTSIDE-SECRET")
    root = _tree(tmp_path)
    calls = {"count": 0}

    async def scripted(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        calls["count"] += 1
        if calls["count"] == 1:
            return ModelResponse(parts=[ToolCallPart("read_file", {"path": "../outside.txt"})])
        if calls["count"] == 2:
            return ModelResponse(parts=[ToolCallPart("read_file", {"path": "app/page.tsx"})])
        return _output_response({"completeness": "complete", "questions": _six_questions()})

    result = await run_review(
        model=FunctionModel(scripted),
        user_id=uuid.uuid7(),
        snapshot_root=root,
    )
    transcript = str(result.all_messages())
    assert "VISITOR-LOG" in transcript  # the real file's content flowed back
    assert "OUTSIDE-SECRET" not in transcript  # the escape never produced content
    assert any(
        isinstance(part, RetryPromptPart) and "escapes the workspace" in str(part.content)
        for message in result.all_messages()
        for part in getattr(message, "parts", [])
    )
    assert any(
        isinstance(part, ToolReturnPart) and part.tool_name == "read_file"
        for message in result.all_messages()
        for part in getattr(message, "parts", [])
    )
    # No sandbox field exists on the deps — nothing a tool could resolve one from.
    assert {field.name for field in dataclasses.fields(ReviewDeps)} == {"user_id", "workspace"}
    assert isinstance(
        ReviewDeps(
            user_id=uuid.uuid7(), workspace=ExtractedSnapshotWorkspace(root=root)
        ).workspace,
        ExtractedSnapshotWorkspace,
    )


# ---------------------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------------------


def test_instructions_carry_the_whole_rubric() -> None:
    for key in CLASSIFICATION_KEYS:
        assert f"`{key}`" in REVIEW_INSTRUCTIONS


def test_scan_hits_format_carries_location_and_family_never_a_value() -> None:
    hits = [
        LocatedHit(
            path="app/db.ts", hit=CredentialHit(family="stripe-live-key", tier=Tier.A, line=12)
        ),
        LocatedHit(
            path="app/login.tsx",
            hit=CredentialHit(family="credential-name-literal", tier=Tier.B, line=3),
        ),
    ]
    rendered = format_scan_hits(hits)
    assert "app/db.ts" in rendered and "line 12" in rendered and "stripe-live-key" in rendered
    assert "high-confidence" in rendered  # Tier A framing
    assert "lead" in rendered  # Tier B framing
    # Structural: the hit shape has no value field to leak.
    assert {field.name for field in dataclasses.fields(CredentialHit)} == {
        "family",
        "tier",
        "line",
    }


def test_no_hits_is_a_signal_not_an_answer() -> None:
    assert "found no hits" in format_scan_hits(())


def test_prompt_lists_files_and_asks_to_verify_hits() -> None:
    prompt = build_review_prompt(
        files=["app/page.tsx", "app/db.ts"],
        scan_hits=[
            LocatedHit(path="app/db.ts", hit=CredentialHit(family="jwt", tier=Tier.A, line=7))
        ],
    )
    assert "app/page.tsx" in prompt and "app/db.ts" in prompt
    assert "Verify each scan finding" in prompt
    # The static rubric is NOT duplicated below the cache breakpoints.
    assert REVIEW_INSTRUCTIONS not in prompt


def test_prompt_listing_is_capped_with_a_marker() -> None:
    files = [f"app/file{index}.tsx" for index in range(LISTING_MAX_FILES + 3)]
    prompt = build_review_prompt(files=files, scan_hits=())
    assert f"app/file{LISTING_MAX_FILES - 1}.tsx" in prompt
    assert f"app/file{LISTING_MAX_FILES}.tsx" not in prompt
    assert "3 more files" in prompt
