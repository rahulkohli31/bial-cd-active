"""The marketplace catalog's wire shape (#145).

THIS IS THE EXPOSURE BOUNDARY, and it is the reason these models are hand-written rather
than derived from the ORM rows. Every other list on the platform is scoped by `user_id`
(ADR-0004: cross-user access is normally an explicit, role-gated, audited action); the
marketplace is a DELIBERATE, REASONED DEVIATION from that default, argued in full in PR
#147 rather than an oversight — an enterprise platform where no app is a private document.
There is no separate ADR document to amend for this (ADR-0004 has no standalone file in
this repo, only inline citations); what a caller may see about someone else's app is
instead enumerated in one place a reviewer can check at a glance, here.

FOUR FIELDS, and nothing else: the application name, its description, the display name of
the person who built it, and the address it is live at. Never app code, submission
identifiers, `app_key`, per-app database details, project internals, or the deployment row.
The builder is named by DISPLAY NAME only — never email, never the Entra object id, which
are the two identifiers the rest of the platform is careful never to hand across a user
boundary.

Adding a field here is a deliberate act with a security consequence, which is precisely
why the route SELECTs these columns explicitly instead of returning ORM objects: a column
added to `Project` or `Deployment` later cannot silently widen this response.
"""

from __future__ import annotations

from src.schemas.base import CamelModel


class MarketplaceEntry(CamelModel):
    """One published app as the catalog shows it. See the module docstring for why this
    list is short and closed.

    WORTH STATING PLAINLY: this materially widens a pre-existing accepted risk rather than
    creating a new one. `login_required` has no enforcement reader anywhere on the platform,
    and a published app carries no auth of its own — so this endpoint converts "you need to
    already have the URL" into "everyone signed in has every URL, searchable by what the app
    does." No `SECURITY.md` exists in this repo to carry that line separately, so it is
    recorded here, next to the field that does it (`url`). The mitigating context: #148's
    `AUTO_DEPLOY_MAX_SCORE = 0` gate already routes any sensitive-category app through
    mandatory admin review before it can go live at all."""

    #: The app's name. `app_registry` carries no name of its own (#48) — the owning
    #: project's name IS the app name.
    name: str
    #: What the app does. Nullable: an app whose builder never wrote one still appears in
    #: the unfiltered catalog, it simply cannot be found by typing (#145, accepted).
    description: str | None
    #: WHO BUILT IT, by display name only. Nullable because `users.display_name` is.
    builder_display_name: str | None
    #: The live address. Non-null by construction — an entry only exists because a
    #: deployment has a URL.
    url: str


class MarketplaceListResponse(CamelModel):
    """An OFFSET page envelope — deliberately NOT the keyset one every other list uses.

    `pagination.py` states the platform's position plainly: keyset, not offset, and no
    `total`/`totalPages` (KD-1), because offset cannot guarantee a page with no duplicates
    or skips while rows are being inserted underneath it. That reasoning is about a list YOU
    OWN AND ARE WRITING TO — but it is NOT true that nobody writes this derived view while a
    caller pages through it. `store.succeed` and `store.unpublish` write it on every deploy
    and every takedown, and because ids are UUIDv7 under `ORDER BY id DESC`, an insert lands
    at position 0 — the worst case for OFFSET, not a benign one. The ACCEPTED risk, stated
    honestly rather than assumed away: a concurrent deploy or unpublish during a page walk
    can duplicate or skip an entry at a page boundary, and `total` is a separate read from
    the page itself under READ COMMITTED, not one snapshot. #145 sizes the catalog at
    10-200 rows, where that window is small and `COUNT(*)` stays trivial regardless.

    What offset buys that keyset cannot: a page NUMBER a person can jump to, a total so the
    UI knows how many pages exist, and ordering by something other than the cursor column —
    which is what makes sort-by-name possible at all. It also fixes a real limitation this
    endpoint shipped with: a relevance-ranked search could only ever return ONE page,
    because an id-cursor cannot continue a rank ordering.

    The deviation is contained to this one endpoint and is argued in the PR rather than
    assumed. Every other list on the platform stays keyset."""

    items: list[MarketplaceEntry]
    #: 1-based, echoed back so a client never has to infer which page it is looking at.
    page: int
    page_size: int
    #: Rows matching the CURRENT filter, not rows in the catalog — a searched total that
    #: ignored `q` would render a page count the user can never reach.
    total: int
    total_pages: int
