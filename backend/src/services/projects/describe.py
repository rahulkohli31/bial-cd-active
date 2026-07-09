"""Project description generation from the app's code (U7, KD-5).

Reuses the existing Claude/agent path (Foundry-only, R11 of the migration doc) as a normal
one-shot model call: it bills against the daily gate like a chat turn (Q5) and shares the
same by-design pre-flight overshoot window. A fresh project (no code) has nothing to
generate from — the caller rejects that BEFORE calling here. When a description already
exists, it is fed in alongside the code so generation *revises* rather than discards it
(R19). The code fed to the model is bounded to the context window (a single app's snapshot
can exceed 200k), and the result is length-capped (KD-8).
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic_ai.models import Model
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.project import MAX_PROJECT_DESCRIPTION
from src.services.agent.agent import ChatDeps, chat_agent
from src.services.usage.gate import record_usage
from src.services.usage.limits import MODEL_CONTEXT_WINDOW

# Bound the code fed to the model so a large snapshot can't blow the window (KD-5). ~3
# chars/token is a conservative heuristic that leaves headroom for the system prompt + the
# generated description within MODEL_CONTEXT_WINDOW tokens.
_CODE_CHAR_BUDGET = MODEL_CONTEXT_WINDOW * 3
# A description is short (2-4 sentences); cap the model's output so it can't run long.
_MAX_DESCRIPTION_TOKENS = 400

_DESCRIBE_SYSTEM = (
    "You are a technical writer. Given a citizen-built app's source code, write a concise, "
    "factual description (2-4 sentences) of what the app does and who it is for. Output ONLY "
    "the description text — no preamble, no markdown, no code fences."
)


def extract_source(current_code: dict[str, Any] | None) -> str:
    """Pull the working source out of a `{current: {source, ...}}` code snapshot (KD-9);
    empty string when absent or malformed (the caller treats empty as 'nothing to generate')."""
    if not isinstance(current_code, dict):
        return ""
    current = current_code.get("current")
    if not isinstance(current, dict):
        return ""
    source = current.get("source")
    return source if isinstance(source, str) else ""


def _build_prompt(source: str, current_description: str | None) -> str:
    bounded = source[:_CODE_CHAR_BUDGET]
    if len(source) > _CODE_CHAR_BUDGET:
        bounded += "\n\n[... code truncated to fit the model context window ...]"
    if current_description:
        return (
            "This is the app's CURRENT description:\n"
            f"{current_description}\n\n"
            "This is the app's CURRENT code:\n"
            f"{bounded}\n\n"
            "Revise the description so it accurately reflects the code, preserving the author's "
            "intent where it still holds. Output only the revised description."
        )
    return (
        "This is the app's code:\n"
        f"{bounded}\n\n"
        "Write a concise description of what this app does. Output only the description."
    )


async def generate_project_description(
    db: AsyncSession,
    model: Model,
    user_id: uuid.UUID,
    *,
    source: str,
    current_description: str | None,
) -> str:
    """Generate (or revise) a project description from its app code (KD-5). Bills the turn
    via `record_usage`; the CALLER owns the daily gate + the commit. Returns the length-capped
    description (KD-8)."""
    prompt = _build_prompt(source, current_description)
    deps = ChatDeps(db=db, user_id=user_id, system=_DESCRIBE_SYSTEM)
    result = await chat_agent.run(
        prompt, deps=deps, model=model, model_settings={"max_tokens": _MAX_DESCRIPTION_TOKENS}
    )
    usage = result.usage
    await record_usage(
        db,
        user_id,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
    )
    return result.output.strip()[:MAX_PROJECT_DESCRIPTION]
