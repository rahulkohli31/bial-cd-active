"""The marketplace catalog's wire shape (#145).

THIS IS THE EXPOSURE BOUNDARY, and it is the reason these models are hand-written rather
than derived from the ORM rows. Every other list on the platform is scoped by `user_id`
(ADR-0004); the marketplace is the first read that deliberately drops that predicate, so
what a caller may see about someone else's app has to be enumerated in one place a reviewer
can check at a glance.

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
    list is short and closed."""

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
    """The keyset page envelope, matching `ProjectListResponse` (KD-1): items + next cursor
    + whether more remain.

    `nextCursor` is null on a SEARCH response, and that is not an oversight — see
    `list_marketplace`'s docstring. Keyset pagination orders by id; a relevance-ranked
    search orders by rank, and the two cannot be the same cursor."""

    items: list[MarketplaceEntry]
    next_cursor: str | None
    has_more: bool
