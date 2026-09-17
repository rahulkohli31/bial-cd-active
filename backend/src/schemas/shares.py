"""Request/response schemas for project sharing (#198 R1-R14).

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
    """One search hit (R5) — display name AND the email LOCAL PART only, never the full
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
    """One row of a project's OWN share panel (R12) — who it is shared with, and when.
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
    """One row of the recipient's "Shared with me" list (R12). `shared_by_display_name` only
    — no email at all, matching the STRICTER attribution rule the marketplace's own
    `MarketplaceEntry.builderDisplayName` already established for showing one citizen's
    identity to another (display name only, `marketplace.py`'s own docstring). R5's fuller
    "display name AND email local part" is specific to the colleague PICKER, where an owner
    is choosing among possible strangers with the same name; a recipient is not choosing
    anyone here, just being told who acted, so the narrower exposure applies.

    `shared_by_user_id` IS THE FILTER'S HANDLE AND THE NAME IS ONLY ITS LABEL. `display_name` is
    nullable and not unique, so a list filtered by name would collapse two colleagues who share
    one and hand the recipient the other's applications.

    `project_updated_at` IS THE OWNER'S, not the recipient's, and it is the only date on this row
    that is not about the share: `shared_at` says when access was granted, and this says when the
    application behind it last changed."""

    project_id: uuid.UUID
    project_name: str
    project_description: str | None
    project_updated_at: datetime
    shared_by_user_id: uuid.UUID
    shared_by_display_name: str | None
    shared_at: datetime


class SharedProjectSharer(CamelModel):
    """One option of the "Shared by" filter, with how many of the recipient's rows are that
    colleague's.

    THE COUNT DESCRIBES THE SEARCH, NOT THE WHOLE LIST — `q` narrows these numbers the same way
    it narrows `total`, so the filter never offers a colleague with no matching rows behind them.
    The sharer filter itself is deliberately NOT applied to its own options; see
    `services/projects/shares.py::list_shared_with_me`."""

    user_id: uuid.UUID
    display_name: str | None
    share_count: int


class SharedProjectListResponse(CamelModel):
    """An OFFSET page of the recipient's shared list, with the "Shared by" filter's options
    alongside it.

    THE COST IS REAL AND IT IS NOT THE OWNER LIST'S COST. `pagination.py` refuses offset because
    a row written underneath a page walk can duplicate or skip an entry at a boundary, and the
    owner's project list accepts that only because it is effectively single-writer. This list is
    not: every colleague who shares or revokes writes into it. What makes it acceptable anyway is
    argued at `list_shared_with_me`, and `total` is what lets a reader left past the end step
    back to a page that exists.

    `total` counts AFTER `q` and the sharer filter are applied — it describes the filtered list,
    never the whole of what the recipient holds."""

    items: list[SharedProjectResponse]
    sharers: list[SharedProjectSharer]
    page: int
    page_size: int
    total: int
    total_pages: int
