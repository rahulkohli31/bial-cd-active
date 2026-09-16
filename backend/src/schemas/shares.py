"""Request/response schemas for project sharing.

Lives apart from `schemas/projects.py` the same way `schemas/marketplace.py` does — a related
but distinct concern with its own exposure rules, not folded into the already-dense projects
module.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from src.schemas.base import CamelModel


class ShareRequest(CamelModel):
    """The body both `POST /v1/projects/{id}:share` and `:unshare` take — the colleague's user
    id, resolved from a prior `:colleagues` search result, never a typed email (a citizen
    cannot be trusted to spell a colleague's email correctly, and a share against a typo'd
    address would silently grant nobody anything while the owner believes it worked)."""

    shared_with_user_id: uuid.UUID


class ColleagueResult(CamelModel):
    """One search hit — display name AND the email LOCAL PART only, never the full
    address: enough for an owner to tell two colleagues with the same name apart, not enough
    to hand out a directory of working email addresses through a picker. NOT the admin roster
    shape, which additionally carries token limits, usage and suspension state — none of
    which a citizen picking a colleague to share with has any business seeing."""

    id: uuid.UUID
    display_name: str | None
    email_local_part: str


class ColleagueSearchResponse(CamelModel):
    colleagues: list[ColleagueResult]


class ShareResponse(CamelModel):
    """One row of a project's OWN share panel — who it is shared with, and when.
    Carries the SAME display-name/email-local-part pair `ColleagueResult` does: the owner
    reads this list to decide who to revoke, and needs to disambiguate the same way the
    search that created the share did."""

    id: uuid.UUID
    shared_with_user_id: uuid.UUID
    shared_with_display_name: str | None
    shared_with_email_local_part: str
    created_at: datetime


class ProjectSharesResponse(CamelModel):
    shares: list[ShareResponse]


class SharedProjectResponse(CamelModel):
    """One row of the recipient's "Shared with me" list. `shared_by_display_name` only — no
    email at all, matching the STRICTER attribution rule the marketplace's own
    `MarketplaceEntry.builderDisplayName` already established for showing one citizen's
    identity to another (display name only, `marketplace.py`'s own docstring). The colleague
    PICKER's fuller "display name AND email local part" fits there because an owner is
    choosing among possible strangers with the same name; a recipient is not choosing anyone
    here, just being told who acted, so the narrower exposure applies."""

    project_id: uuid.UUID
    project_name: str
    project_description: str | None
    shared_by_display_name: str | None
    shared_at: datetime


class SharedProjectListResponse(CamelModel):
    """Keyset-paginated, unlike the owner's own numbered-offset project list — see
    `services/projects/shares.py::list_shared_with_me` for why offset's single-writer
    justification does not hold for a list every sharer writes into."""

    items: list[SharedProjectResponse]
    next_cursor: str | None
    has_more: bool
