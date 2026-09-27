"""The classification review agent — one Yes or No per configured class, from the saved code alone.

ONE module-level `Agent`, built without a bound model or instructions: the Foundry model and the
class definitions are passed per run, the definitions as a STATIC instruction part so the cache
breakpoint lands after them (`prompts.py`). The output model and the tool definitions are fixed,
so they are byte-identical across every configuration; an output validator holds each run to
exactly its own classes. TOOL-CALLING OUTPUT (`ToolOutput`), not provider-native: the latter is
selected by matching the deployment name string, so a rename would silently downgrade it.
EXTENDED THINKING MUST STAY OFF — `ensure_thinking_off` RAISES (never `assert`) rather than
assuming it, because thinking reroutes output onto that fragile native path.

Tools are read-only over the extracted saved version — no sandbox, no write, no network."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic_ai import Agent, ModelRetry, RunContext, ToolOutput
from pydantic_ai.messages import InstructionPart, ModelMessage
from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModelSettings
from pydantic_ai.run import AgentRunResult
from pydantic_ai.usage import UsageLimits

from src.services.agent.read_tools import (
    ExtractedSnapshotWorkspace,
    ReadOnlyWorkspace,
    read_only_toolset,
)
from src.services.classification.config import LiveClass
from src.services.classification.constants import (
    CACHE_TTL,
    REVIEW_EFFORT,
    THINKING_FORCING_EFFORT,
    max_output_tokens,
)
from src.services.classification.prompts import (
    CREDENTIALS_KEY,
    LocatedHit,
    build_review_prompt,
    review_instructions,
)
from src.services.classification.schema import ReviewOutput


class ThinkingEnabledError(RuntimeError):
    """Raised when the review would run with extended thinking enabled — directly, or
    through an effort level that forces it back on. Thinking reroutes output handling
    onto the provider-native path this module exists to avoid, so the combination is
    refused outright rather than run degraded."""


@dataclass(frozen=True)
class ReviewDeps:
    """Per-run agent dependencies. `user_id` is the owning citizen (attribution, and
    the user-scope convention every agent deps carries); `workspace` is the extracted
    snapshot the read tools resolve through; `class_keys` are the keys this run must answer,
    in key order. DELIBERATELY no sandbox field: the review reads saved code only, and a
    surface that is not in the deps cannot be reached by any tool."""

    user_id: uuid.UUID
    workspace: ReadOnlyWorkspace
    class_keys: tuple[str, ...]


def _workspace_of(ctx: RunContext[ReviewDeps]) -> ReadOnlyWorkspace:
    return ctx.deps.workspace


OUTPUT_TOOL_NAME = "record_classification_review"
"""The output tool's name — static, so its definition sits behind the tool-definitions
cache breakpoint unchanged across every review."""

# The constructor is parametrized explicitly: the output type is carried by the
# `ToolOutput` marker, which not every checker resolves through the overloads.
review_agent = Agent[ReviewDeps, ReviewOutput](
    deps_type=ReviewDeps,
    output_type=ToolOutput(
        ReviewOutput,
        name=OUTPUT_TOOL_NAME,
        description=(
            "Record the completed classification review. Call exactly once, after every "
            "class has been examined."
        ),
    ),
    toolsets=[read_only_toolset(_workspace_of)],
    retries=2,
)


@review_agent.output_validator
def _answers_exactly_the_run_classes(
    ctx: RunContext[ReviewDeps], output: ReviewOutput
) -> ReviewOutput:
    """Every class this run was given, exactly once, and nothing else; a missing, extra or
    repeated key goes back to the model through the library's own retry. Answers come back in
    key order."""
    expected = ctx.deps.class_keys
    keys = [answer.key for answer in output.answers]
    problems: list[str] = []
    repeated = sorted({key for key in keys if keys.count(key) > 1})
    if repeated:
        problems.append(f"answered more than once: {', '.join(repeated)}")
    missing = [key for key in expected if key not in keys]
    if missing:
        problems.append(f"missing: {', '.join(missing)}")
    unknown = sorted(set(keys) - set(expected))
    if unknown:
        problems.append(f"not a class in this review: {', '.join(unknown)}")
    if problems:
        raise ModelRetry(
            "Answer each class exactly once, by the key its definition gives "
            f"({'; '.join(problems)})."
        )
    rank = {key: index for index, key in enumerate(expected)}
    ordered = sorted(output.answers, key=lambda answer: rank[answer.key])
    return ReviewOutput(answers=ordered)


def ensure_thinking_off(settings: AnthropicModelSettings) -> None:
    """Fail closed on any thinking-enabling combination (a RAISING runtime check, never
    `assert`). Three doors are guarded: the base `thinking` knob, an Anthropic thinking
    config that is not explicitly disabled, and the effort levels (`xhigh`, `max`) that
    force thinking back on whatever the config says."""
    thinking = settings.get("thinking")
    if thinking:  # True, or any adaptive-thinking level string — every truthy value enables it
        raise ThinkingEnabledError(
            "the classification review runs with extended thinking OFF; "
            f"`thinking={thinking!r}` would enable it."
        )
    anthropic_thinking = settings.get("anthropic_thinking")
    if anthropic_thinking is not None and anthropic_thinking["type"] != "disabled":
        raise ThinkingEnabledError(
            "the classification review runs with extended thinking OFF; "
            f"`anthropic_thinking` is configured `{anthropic_thinking['type']}`."
        )
    effort = settings.get("anthropic_effort")
    if effort is not None and effort in THINKING_FORCING_EFFORT:
        raise ThinkingEnabledError(
            f"`anthropic_effort={effort!r}` silently re-enables extended thinking "
            "(thinking-disabled is only honoured up to `high`); the review refuses it."
        )


def review_model_settings(class_count: int) -> AnthropicModelSettings:
    """The review's settings block — the harness's shape with its own values. Three of
    these are load-bearing (see `constants.py` for the reasoning each carries): the
    three cache breakpoints at the 1-hour tier, the explicit `low` effort, and the
    explicit `max_tokens`, scaled with the class count. Guarded on the way out so a drifted
    constant can never ship a thinking-enabled block."""
    # NO `temperature`: the deployed model's profile strips sampling settings and warns on
    # every call, so sending it bought a log line and nothing else. See the turn engine's note.
    settings = AnthropicModelSettings(
        max_tokens=max_output_tokens(class_count),
        anthropic_effort=REVIEW_EFFORT,
        anthropic_cache_instructions=CACHE_TTL,
        anthropic_cache_tool_definitions=CACHE_TTL,
        anthropic_cache=CACHE_TTL,
    )
    ensure_thinking_off(settings)
    return settings


async def run_review(
    *,
    model: Model,
    user_id: uuid.UUID,
    snapshot_root: Path,
    classes: Sequence[LiveClass],
    scan_hits: Sequence[LocatedHit] = (),
    prompt: str | None = None,
    message_history: list[ModelMessage] | None = None,
    model_settings: AnthropicModelSettings | None = None,
    usage_limits: UsageLimits | None = None,
) -> AgentRunResult[ReviewOutput]:
    """One review run over an extracted snapshot against `classes` — the entry the runner calls.

    `scan_hits` reach the prompt only while the credentials class is among `classes`, as
    directed evidence: location and family, never a value. On the guided truncation retry the
    runner passes `message_history` + a constraining `prompt`, skipping default assembly; the
    instructions are resent, since history never carries them. A `model_settings` override is
    guarded like the default."""
    keys = tuple(sorted(entry.key for entry in classes))
    settings = review_model_settings(len(keys)) if model_settings is None else model_settings
    ensure_thinking_off(settings)
    workspace = ExtractedSnapshotWorkspace(root=snapshot_root)
    if prompt is None:
        files = await workspace.list_files()
        hits = scan_hits if CREDENTIALS_KEY in keys else None
        prompt = build_review_prompt(files=files, scan_hits=hits)
    return await review_agent.run(
        prompt,
        instructions=[InstructionPart(content=review_instructions(classes), dynamic=False)],
        deps=ReviewDeps(user_id=user_id, workspace=workspace, class_keys=keys),
        model=model,
        model_settings=settings,
        message_history=message_history,
        usage_limits=usage_limits,
    )
