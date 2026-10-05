"""The browser's one narrow way to write down something only it could have seen.

WHY THIS EXISTS

TWO OF THE FOUR QUESTIONS ARE NOT ANSWERABLE ON THIS SIDE. How long it takes a citizen to
first LOOK at their own app, and how often a project is opened without any chat being
opened, are facts about a screen. The server never learns either one, so the browser has to
say — and this is the whole of what it is allowed to say.

WHAT BOUNDS A NUMBER THE SERVER CANNOT CHECK: a server-side name allowlist
(`_CEILING_BY_NAME` is the vocabulary, and a `HarnessCounter` member that is not
browser-observable is refused exactly as a nonsense string is); a per-name ceiling that
REFUSES rather than clamps, because a clamped duration reads as exactly the ceiling and
would quietly flatter the mean; authentication and CSRF (opt-in per route in this codebase,
never global), since a forged write pollutes the only measurement; and a per-user rate limit.

THE TRADE, NAMED. The identity bounds who may write and is then DISCARDED — no row this
route writes carries a user id, like every other row in `harness_counts`. So a poisoned or
duplicated number cannot be excluded retrospectively: there is nothing to exclude it BY.
The defence is the per-user limit in the moment, not a per-user filter afterwards. This is
observability inside a single-tenant enterprise deployment; it must not become a way to
profile a citizen.

ONE WRITE USES THE IDENTITY. `POST /observations/start-visible` attaches the browser's
click-to-visible time to a `sandbox_starts` row the caller's own start wrote. It never creates a
row and never reads one back: the caller's id is in the update's predicate, which is what keeps
the write on their own start.

NO READ. The counters are read behind the superadmin gate at `GET /v1/admin/harness-counters`,
the start times at `GET /v1/admin/sandbox-starts`."""

# THE BOUND THIS ROUTE DOES NOT ENFORCE, said plainly rather than left to be discovered:
# each name is bounded ALONE. Nothing here relates one to another, so `project_opened_chat`
# can be written with no `project_opened` behind it and the chat-open ratio
# (`project_opened_chat / project_opened`) can come out above 1. The invariant that keeps it at
# or below 1 lives in the BROWSER (`portal/src/utils/observe.ts` only marks a chat open for a
# project already marked open in this page load), which means it holds for the portal and not
# for a hand-made request.
# Enforcing it here would need a server-issued visit token — a session model this service
# deliberately does not build. So the reading rule, which belongs beside the number:
# `1 - (project_opened_chat / project_opened)` outside [0, 1] is not a surprising result, it
# is poisoned or lossy data, and should be read as such rather than reported.
#
# The rule that follows and is binding elsewhere: any user-facing "roughly how long" estimate
# is sourced from the SERVER-measured `app_cold_start_ms`, never from the browser-measured
# duration this route accepts.
#
# WHY `/v1/observations` AND NOT `/v1/harness-counters`: the latter matches the storage
# vocabulary and collides in a reader's mind with the superadmin read at
# `/v1/admin/harness-counters`, which is a different audience behind a different gate.

from __future__ import annotations

import uuid
from typing import Any, Final

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from src.api.deps import CurrentUser, DbSession
from src.api.deps_csrf import RequireCsrf
from src.api.v1.build_sessions.schemas import STARTING_MARKER_TTL_SECONDS
from src.api.v1.observations.schemas import ObservationRequest, StartVisibleRequest
from src.core.errors import AppApiError
from src.db.models.harness_counter import HarnessCounter
from src.db.models.sandbox_start import SandboxStart
from src.schemas import AUTH_401, ErrorEnvelope, OkResponse, error_responses, raw_body_doc
from src.services.build_sessions.counters import count
from src.services.ratelimit import rate_limit

router = APIRouter(prefix="/observations", tags=["observations"])

_REQUEST_BODY_DOC = raw_body_doc(ObservationRequest)

# The longest a first view of an app can honestly take, in milliseconds.
#
# DERIVED, not picked, and deliberately loose at the top end. The machine half of a slow honest
# journey is a cold restore (whose readiness wait alone budgets `_COLD_READY_BUDGET_SECONDS`,
# 120 s, on top of an unbounded ACA create) plus the frame's own load cap (`FRAME_LOAD_CAP_MS`,
# 20 s). The interval then adds one HUMAN step — choosing which chat to open — so a ceiling near
# the machine figure would refuse exactly the slow journeys this measurement exists to see. Ten
# minutes is the line: past it, this is a tab that was backgrounded, a laptop that slept, or a
# lie, and refusing is cheaper and more honest than a second client-side mechanism trying to
# detect the same thing.
# It is a poison bound, NOT a plausibility bound — a value under it is not thereby trustworthy.
MAX_OBSERVED_MS: Final = 10 * 60 * 1000

# The server stops holding a start as in flight once its starting marker expires, so a wait from
# the click past that is a tab that slept rather than a start.
MAX_START_VISIBLE_MS: Final = STARTING_MARKER_TTL_SECONDS * 1000

# THE ALLOWLIST, and it is a mapping rather than a set because the ceiling is per name.
#
# AN OCCURRENCE COUNTER'S CEILING IS 1, because an occurrence IS one. A browser reporting
# `project_opened` with a value of 40 is not reporting an occurrence, it is inflating the
# chat-open ratio's denominator — and one comparison refuses that without a second code path
# for "this name is an occurrence". A ceiling of 1 is also what makes a MISSING value legible: it
# means "one of these happened", which is the whole payload an occurrence has. For a DURATION
# the same omission means nothing at all, and is refused rather than defaulted — see
# `_bounded_value`.
_CEILING_BY_NAME: Final[dict[str, int]] = {
    HarnessCounter.PROJECT_TO_APP_VISIBLE_MS.value: MAX_OBSERVED_MS,
    HarnessCounter.PROJECT_OPENED.value: 1,
    HarnessCounter.PROJECT_OPENED_CHAT.value: 1,
}

# Per-user rate limit, shared by both routes. A whole project visit produces at most four of
# these, so this is roughly fifteen visits in five minutes — far above ordinary use, and still a
# bound.
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
    if raw is None:
        # An OCCURRENCE (ceiling 1) says everything it has to say by arriving, so a missing value
        # is the ordinary call. A DURATION with no value is a measurement that did not happen, and
        # defaulting it to 1 would file a one-millisecond first-view — the same silent corruption
        # of the mean this module refuses a too-LARGE value to avoid, arriving from the other end
        # and, with no user id on the row, just as impossible to exclude afterwards.
        if ceiling == 1:
            return 1
        raise AppApiError(400, "Observation value is required.", code="invalid_value")
    # `bool` is an `int` in Python, and `True` would otherwise sail through as 1. A boolean is
    # not a measurement.
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise AppApiError(400, "Observation value must be a whole number.", code="invalid_value")
    if raw < 1 or raw > ceiling:
        raise AppApiError(400, "Observation value is out of range.", code="value_out_of_range")
    return raw


async def _json_object(request: Request, refusal: str) -> dict[str, Any]:
    """The body as a JSON object, or a 400 `invalid_body` carrying `refusal`."""
    try:
        body: Any = await request.json()
    except (ValueError, TypeError):  # fmt: skip  # ruff py314 strips parens
        raise AppApiError(400, refusal, code="invalid_body") from None
    if not isinstance(body, dict):
        raise AppApiError(400, refusal, code="invalid_body")
    return body


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
    body = await _json_object(request, "Observation name is required.")

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


@router.post(
    "/start-visible",
    response_model=OkResponse,
    dependencies=[RequireCsrf, Depends(_observation_limiter)],
    openapi_extra=raw_body_doc(StartVisibleRequest),
    responses=error_responses(
        (400, ErrorEnvelope, "A start id that is not a UUID, or a duration outside its bound"),
        AUTH_401,
        (403, ErrorEnvelope, "CSRF check failed"),
        (404, ErrorEnvelope, "No start of the caller's is waiting for a time under this id"),
        (429, ErrorEnvelope, "Too many observations"),
    ),
)
async def record_start_visible(request: Request, user: CurrentUser, db: DbSession) -> JSONResponse:
    """Attach the browser's click-to-visible time to one of the caller's own sandbox starts.

    The first report wins. A start that is someone else's, that does not exist, or that already
    has its time is refused alike and nothing changes, so the refusal says nothing about whether
    a start exists."""
    body = await _json_object(request, "A start id is required.")
    raw_id = body.get("startId")
    try:
        start_id = uuid.UUID(raw_id) if isinstance(raw_id, str) else None
    except ValueError:
        start_id = None
    if start_id is None:
        raise AppApiError(400, "A start id is required.", code="invalid_body")
    duration_ms = _bounded_value(body.get("durationMs"), MAX_START_VISIBLE_MS)

    # The caller's id in the predicate is the isolation: without it anyone signed in could time
    # anyone's start.
    written = await db.scalar(
        sa.update(SandboxStart)
        .where(
            SandboxStart.id == start_id,
            SandboxStart.user_id == user.id,
            SandboxStart.browser_visible_ms.is_(None),
        )
        .values(browser_visible_ms=duration_ms)
        .returning(SandboxStart.id)
    )
    if written is None:
        raise AppApiError(404, "Unknown start.", code="unknown_start")
    await db.commit()
    return JSONResponse(content={"ok": True})
