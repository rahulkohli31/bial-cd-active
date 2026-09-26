"""Ceilings and model settings for the classification review loop.

These live HERE, not in `config.py`, for the same reason the build harness's own constants
file records: caching, effort and the output clamp are properties of how THIS loop is
shaped, not per-deployment knobs. The review runs on whatever Foundry deployment the
platform is configured with (Opus today, Sonnet 5 in its own later PR) — the SETTINGS
travel with the loop, and the ceilings get re-measured whenever the deployment changes.
"""

from __future__ import annotations

from typing import Final, Literal

OUTPUT_TOKENS_BASE: Final = 2_000
OUTPUT_TOKENS_PER_CLASS: Final = 1_000


def max_output_tokens(class_count: int) -> int:
    """The per-model-step output clamp, set explicitly and scaled with the active class count.

    Only the final structured output is large, and a long reason with several cited locations
    comes to roughly 700 tokens per class, so each class is budgeted 1,000 above a fixed base. The
    framework's own default of 4,096 would truncate the worst legitimate answer. The model's
    ceiling is far above any realistic class count, so this is a self-imposed guard; truncation
    at it is a failure that runs the one guided retry, never something to salvage from."""
    return OUTPUT_TOKENS_BASE + OUTPUT_TOKENS_PER_CLASS * class_count


CACHE_TTL: Final[Literal["1h"]] = "1h"
"""TTL for every Anthropic prompt-cache breakpoint the review sets
(`anthropic_cache_instructions`, `anthropic_cache_tool_definitions`, `anthropic_cache`) —
the 1-HOUR tier, mirroring the build harness's block exactly. The economics here are
BETTER than the harness's: the class definitions, the output schema and the four tool
definitions are byte-identical not merely across the steps of one run but across EVERY
review of EVERY app under one class-definition fingerprint, so that prefix is a shared cache
hit platform-wide. Foundry prices cache reads at a tenth of base input; this is the largest
cost lever available without changing model, and it is why the prompt is ordered static-first
— anything app-specific placed above a breakpoint destroys the hit (`prompts.py` owns that
ordering)."""

REVIEW_EFFORT: Final[Literal["low"]] = "low"
"""Effort, set explicitly — and NOT a free knob. The parameter takes
`low | medium | high | xhigh | max`, and thinking-disabled is only honoured up to `high`:
`xhigh` and `max` force extended thinking back on, which reroutes output handling onto
the fragile provider-native path this module already refuses (see `agent.py`). So effort
is part of how the thinking-off requirement is ENFORCED, not a performance dial. Bounded
Yes/No classification over a small tree does not need more than `low`."""

THINKING_FORCING_EFFORT: Final[frozenset[str]] = frozenset({"xhigh", "max"})
"""The effort levels that silently re-enable extended thinking. `ensure_thinking_off`
raises on these — raising an effort level must never smuggle thinking back on."""

LISTING_MAX_FILES: Final = 500
"""Cap on the file listing embedded in the review prompt (mirrors the read toolset's
`LIST_MAX_ENTRIES`); a deeper tree gets an explicit truncation marker and the model still
holds `list_files` to see the rest."""

REVIEW_WALL_CLOCK_CEILING_S: Final = 120.0
"""The review's wall-clock ceiling — abandoned once a stated deadline passes — measured
from the ROW's `started_at` — never from a dialog opening, so a reload cannot extend it
and a control-plane restart leaves a row that AGES OUT rather than hangs. PROVISIONAL:
this value belongs to the Opus deployment the nine measured runs were taken on (typical
run ~18s, near-empty apps 54-57s) and gets re-measured — and again when the review
moves to its own Sonnet deployment. 120s leaves the slowest measured shape a 2x margin
without letting a wedged run hold the publish dialog for minutes."""

REVIEW_REQUEST_BUDGET: Final = 25
"""The run's model-request budget (`UsageLimits.request_limit`) — the hard bound on what
one review may spend, alongside the store's three-attempts-per-version cap. PROVISIONAL,
same ownership as the wall-clock ceiling above: the observed hard failure was a
near-empty app exhausting a request ceiling, so the budget is
sized for the measured behaviours — a listing, a handful of directed verifications, a
read per file of a small app, the final structured output — with room for the guided
truncation retry, WHICH DRAWS FROM THIS SAME BUDGET (a truncation with no budget left is
a review-failed, never a usage-limit error; `service.py` owns that arithmetic)."""
