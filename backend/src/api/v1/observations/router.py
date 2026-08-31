"""The browser's one narrow way to write down something only it could have seen (R104, R105, R106).

TWO OF THE FOUR QUESTIONS ARE NOT ANSWERABLE ON THIS SIDE. How long it takes a citizen to first
LOOK at their own app, and how often a project is opened without any chat being opened, are facts
about a screen. The server never learns either one, so the browser has to say — and this is the
whole of what it is allowed to say.

WHAT BOUNDS A NUMBER THE SERVER CANNOT CHECK. Four things, and the trade one of them makes is
stated rather than papered over:

  1. A SERVER-SIDE NAME ALLOWLIST. Not "the browser is asked not to invent counters" — the name
     does not exist on this side of the call. `_CEILING_BY_NAME` is the vocabulary, and a
     `HarnessCounter` member that is not browser-observable (a start counter, say) is refused
     exactly as a nonsense string is.
  2. A CEILING PER NAME, which REFUSES rather than clamps — the same discipline the message
     bound follows (`api/v1/conversations/_shared.py`, a field constraint that rejects and never
     trims). A clamped duration silently reads as exactly the ceiling and would quietly flatter
     the mean, which is a worse outcome than losing the row.
  3. AUTHENTICATION AND CSRF. `RequireCsrf` is opt-in per route in this codebase and is worth
     opting into here: a forged write pollutes the only measurement the platform has.
  4. A PER-USER RATE LIMIT, on the `api/v1/feedback` pattern.

THE TRADE, NAMED. The identity bounds who may write and is then DISCARDED — no row this route
writes carries a user id, exactly like every other row in `harness_counts`, which has never been
user-scoped. The consequence is real: a poisoned or duplicated number cannot be excluded
retrospectively, because there is nothing to exclude it BY. The defence is the per-user limit in
the moment, not a per-user filter afterwards, and keeping the table un-scoped is worth more than
that filter would be. This is an observability endpoint inside a single-tenant enterprise
deployment; it must not become a way to profile a citizen.

The rule that follows and is binding elsewhere: any user-facing "roughly how long" estimate is
sourced from the SERVER-measured `app_cold_start_ms`, never from the browser-measured duration
this route accepts.

NO READ. There is nothing a browser should learn from a measurement it just made, and the counters
are read where they have always been read — `GET /v1/admin/harness-counters`, behind the superadmin
gate. This route has no sibling.

WHY `/v1/observations` AND NOT `/v1/harness-counters`. The latter matches the storage vocabulary
and collides in a reader's mind with the superadmin read at `/v1/admin/harness-counters`, which is
a different audience behind a different gate.
"""

from __future__ import annotations

from typing import Any, Final

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from src.api.deps import CurrentUser
from src.api.deps_csrf import RequireCsrf
from src.api.v1.observations.schemas import ObservationRequest
from src.core.errors import AppApiError
from src.db.models.harness_counter import HarnessCounter
from src.schemas import AUTH_401, ErrorEnvelope, OkResponse, error_responses
from src.services.build_sessions.counters import count
from src.services.ratelimit import rate_limit

router = APIRouter(prefix="/observations", tags=["observations"])

# The raw-parse route takes a JSON body FastAPI never sees (no Pydantic param), so its request
# shape is documented explicitly from the model — without enabling the 422 path.
_REQUEST_BODY_DOC: dict[str, Any] = {
    "requestBody": {
        "required": True,
        "content": {"application/json": {"schema": ObservationRequest.model_json_schema()}},
    }
}

# The longest a first view of an app can honestly take, in milliseconds.
#
# DERIVED, not picked. The machine half of the worst honest journey is the cold-ready budget
# (`_COLD_READY_BUDGET_SECONDS`, 120 s) plus the frame's own load cap (`FRAME_LOAD_CAP_MS`, 20 s)
# — 140 s. The interval also contains one HUMAN step, choosing which chat to open, so the ceiling
# has to sit above 140 s or it would refuse ordinary slow journeys, which are precisely the ones
# R104 exists to see. Ten minutes is the line: past it, this is a tab that was backgrounded, a
# laptop that slept, or a lie, and refusing is cheaper and more honest than a second client-side
# mechanism trying to detect the same thing.
MAX_OBSERVED_MS: Final = 10 * 60 * 1000

# THE ALLOWLIST, and it is a mapping rather than a set because the ceiling is per name.
#
# AN OCCURRENCE COUNTER'S CEILING IS 1, because an occurrence IS one. A browser reporting
# `project_opened` with a value of 40 is not reporting an occurrence, it is inflating R105's
# denominator — and one comparison refuses that without a second code path for "this name is an
# occurrence". A missing value is 1, so the ordinary occurrence call needs no value at all.
_CEILING_BY_NAME: Final[dict[str, int]] = {
    HarnessCounter.PROJECT_TO_APP_VISIBLE_MS.value: MAX_OBSERVED_MS,
    HarnessCounter.PROJECT_OPENED.value: 1,
    HarnessCounter.PROJECT_OPENED_CHAT.value: 1,
}

# Per-user rate limit. A whole project visit produces at most three of these, so this is roughly
# twenty visits in five minutes — far above ordinary use, and still a bound.
OBSERVATION_RATE_LIMIT: Final = 60
OBSERVATION_RATE_WINDOW_SECONDS: Final = 5 * 60


async def _observation_rate_key(user: CurrentUser) -> str:
    # Per-user bucket. Declaring `CurrentUser` here resolves identity BEFORE the limiter runs —
    # the "limiter after key" ordering the substrate depends on.
    return f"observations:{user.id}"


_observation_limiter = rate_limit(
    _observation_rate_key,
    limit=OBSERVATION_RATE_LIMIT,
    window_seconds=OBSERVATION_RATE_WINDOW_SECONDS,
    message="Too many observations. Please try again later.",
)


def _bounded_value(raw: Any, ceiling: int) -> int:
    """The observation's value, or a refusal. Never clamps — see the module docstring."""
    # `bool` is an `int` in Python, and `True` would otherwise sail through as 1. A boolean is
    # not a measurement.
    if raw is None:
        return 1
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise AppApiError(400, "Observation value must be a whole number.", code="invalid_value")
    if raw < 1 or raw > ceiling:
        raise AppApiError(400, "Observation value is out of range.", code="value_out_of_range")
    return raw


@router.post(
    "",
    status_code=201,
    response_model=OkResponse,
    dependencies=[RequireCsrf, Depends(_observation_limiter)],
    openapi_extra=_REQUEST_BODY_DOC,
    responses=error_responses(
        (400, ErrorEnvelope, "Unknown counter name, or a value outside its bound"),
        AUTH_401,
        (403, ErrorEnvelope, "CSRF check failed"),
        (429, ErrorEnvelope, "Too many observations"),
    ),
)
async def record_observation(request: Request, user: CurrentUser) -> JSONResponse:
    """Record one named, bounded observation. Writes nothing about who sent it.

    NO `DbSession`. `count(...)` owns its own session on purpose — a count is a historical fact
    about something that HAPPENED and must not disappear because a surrounding transaction did —
    and taking a request session here just to not use it would invite someone to write through it.
    """
    try:
        body: Any = await request.json()
    except (ValueError, TypeError):  # fmt: skip  # ruff py314 strips parens
        raise AppApiError(400, "Observation name is required.", code="invalid_body") from None
    if not isinstance(body, dict):
        raise AppApiError(400, "Observation name is required.", code="invalid_body")

    name = body.get("name")
    if not isinstance(name, str):
        raise AppApiError(400, "Observation name is required.", code="invalid_body")
    ceiling = _CEILING_BY_NAME.get(name)
    if ceiling is None:
        # Deliberately does NOT echo the name back, and deliberately does not say which names
        # exist: the allowlist is a server-side fact, not a discovery surface.
        raise AppApiError(400, "Unknown observation.", code="unknown_counter")

    # The user has been resolved (it is what the limiter keyed on) and is now DISCARDED. The row
    # records what happened, never who it happened to.
    await count(name, value=_bounded_value(body.get("value"), ceiling))
    return JSONResponse(status_code=201, content={"ok": True})
