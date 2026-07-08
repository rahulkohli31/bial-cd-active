"""Runner-token response schemas — the short-lived token + display user the app
shell injects into the sandboxed frame (the shell fetches these from the cookie-authed
`POST /apps/{id}/runner-token` since the HttpOnly session cookie is unreadable)."""

from __future__ import annotations

import uuid

from src.schemas import CamelModel


class RunnerUser(CamelModel):
    id: uuid.UUID
    email: str
    display_name: str | None


class RunnerTokenResponse(CamelModel):
    """The short-lived token + display user the shell injects into the frame."""

    access_token: str
    user: RunnerUser
