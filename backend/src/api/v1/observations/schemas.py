"""The `POST /observations` and `POST /observations/start-visible` bodies.

DOCUMENTATION-ONLY, exactly like `FeedbackRequest`: the route parses the raw JSON itself so every
refusal it RAISES renders the SAME data-plane envelope (`{"error": {"message", "code"}}`) — its
400s, the CSRF 403, and the limiter 429 — rather than mixing in FastAPI's `422 {"detail": [...]}`
shape for body errors.

The 401 is the deliberate exception: `current_user` raises a bare `HTTPException`, so an
unauthenticated call gets `{"detail": ...}` exactly as every other authenticated route does.
Consistency across the API beats consistency within one route for "you are not signed in".
"""

from __future__ import annotations

import uuid

from src.schemas import CamelModel


class ObservationRequest(CamelModel):
    """One named, bounded observation that only the browser could have measured.

    `name` must be on the route's server-side allowlist — a browser cannot invent a counter, and
    the reason it cannot is that the name does not exist on this side of the call. `value` is
    optional: a plain occurrence omits it and is recorded as 1.
    """

    name: str
    value: int | None = None


class StartVisibleRequest(CamelModel):
    """How long the caller waited, in milliseconds, from the click that began one of their own
    sandbox starts to its app showing in their browser. `startId` is the id the server handed back
    for that start; the duration is a whole number above zero and within the route's ceiling."""

    start_id: uuid.UUID
    duration_ms: int
