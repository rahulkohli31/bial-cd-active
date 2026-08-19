"""Ceilings and model settings for the classification review loop (U5).

These live HERE, not in `config.py`, for the same reason the build harness's own constants
file records: caching, effort and the output clamp are properties of how THIS loop is
shaped, not per-deployment knobs. The review runs on whatever Foundry deployment the
platform is configured with (Opus today, Sonnet 5 in its own later PR) — the SETTINGS
travel with the loop, and U14 re-measures the ceilings when the deployment changes.
"""

from __future__ import annotations

from typing import Final, Literal

MAX_TOKENS: Final = 8_000
"""Per-model-STEP output clamp, set EXPLICITLY. The framework's Anthropic default is 4096,
which sits right on top of the sized pathological case (six verdicts with long reasons and
many cited locations come to ~4,000 tokens) — inheriting it would make the worst legitimate
answer truncate. The model's own ceiling is 128k output tokens on both the current and the
intended deployment, so 8k is a self-imposed guard, not a platform limit; a larger cap
costs nothing unless used (billing is on tokens produced), and only the final structured
output is large — tool-call steps are tiny, so the cap binds on exactly one step.
Truncation at this cap is a FAILURE (U6 catches `finish_reason == "length"` and runs the
one guided retry), never something to salvage partial verdicts from."""

TEMPERATURE: Final = 0.0
"""Set for consistency with the two existing run sites, and DOCUMENTED AS UNSUPPORTED on
the current model generation — it is silently ignored. That is why R6 caches the review's
RESULT against the version rather than trusting two runs to agree."""

CACHE_TTL: Final[Literal["1h"]] = "1h"
"""TTL for every Anthropic prompt-cache breakpoint the review sets
(`anthropic_cache_instructions`, `anthropic_cache_tool_definitions`, `anthropic_cache`) —
the 1-HOUR tier, mirroring the build harness's block exactly. The economics here are
BETTER than the harness's: the six-question rubric, the output schema and the four tool
definitions are byte-identical not merely across the steps of one run but across EVERY
review of EVERY app, so that prefix is a shared cache hit platform-wide. Foundry prices
cache reads at a tenth of base input; this is the largest cost lever available without
changing model, and it is why the prompt is ordered static-first — anything app-specific
placed above a breakpoint destroys the hit (`prompts.py` owns that ordering)."""

REVIEW_EFFORT: Final[Literal["low"]] = "low"
"""Effort, set explicitly — and NOT a free knob. The parameter takes
`low | medium | high | xhigh | max`, and thinking-disabled is only honoured up to `high`:
`xhigh` and `max` force extended thinking back on, which reroutes output handling onto
the fragile provider-native path this module already refuses (see `agent.py`). So effort
is part of how the thinking-off requirement is ENFORCED, not a performance dial. Six
bounded classification questions over a small tree do not need more than `low`."""

THINKING_FORCING_EFFORT: Final[frozenset[str]] = frozenset({"xhigh", "max"})
"""The effort levels that silently re-enable extended thinking. `ensure_thinking_off`
raises on these — raising an effort level must never smuggle thinking back on."""

LISTING_MAX_FILES: Final = 500
"""Cap on the file listing embedded in the review prompt (mirrors the read toolset's
`LIST_MAX_ENTRIES`); a deeper tree gets an explicit truncation marker and the model still
holds `list_files` to see the rest."""
