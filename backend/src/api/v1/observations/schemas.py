"""The `POST /observations` body.

DOCUMENTATION-ONLY, exactly like `FeedbackRequest`: the route parses the raw JSON itself so that
every refusal it RAISES renders the SAME data-plane envelope (`{"error": {"message", "code"}}`) —
its own 400s, the CSRF 403, and the limiter 429. A route whose body errors arrive as FastAPI's
`422 {"detail": [...]}` while its gate errors arrive as the envelope makes a caller parse two
shapes to learn one thing.

The 401 is the exception and is deliberately NOT reshaped: `current_user` raises a bare
`HTTPException`, so an unauthenticated call gets `{"detail": ...}` here exactly as it does on
every other authenticated route in this API. Consistency across the API beats consistency within
one route for the one refusal that means "you are not signed in".
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
