"""The `POST /observations` body.

DOCUMENTATION-ONLY, exactly like `FeedbackRequest`: the route parses the raw JSON itself so that
every refusal it can produce renders the SAME data-plane envelope
(`{"error": {"message", "code"}}`) — including the ones it does not raise itself, the CSRF 403
and the limiter 429. A route whose body errors arrive as FastAPI's `422 {"detail": [...]}` while
its gate errors arrive as the envelope makes a caller parse two shapes to learn one thing.
"""

from __future__ import annotations

from src.schemas import CamelModel


class ObservationRequest(CamelModel):
    """One named, bounded observation that only the browser could have measured.

    `name` must be on the route's server-side allowlist — a browser cannot invent a counter, and
    the reason it cannot is that the name does not exist on this side of the call. `value` is
    optional: a plain occurrence omits it and is recorded as 1.
    """

    name: str
    value: int | None = None
