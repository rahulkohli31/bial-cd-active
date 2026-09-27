"""The marketplace catalog's wire shape — THE EXPOSURE BOUNDARY, and why these models are
hand-written rather than derived from ORM rows. Every other list is scoped by `user_id`; the
marketplace is a DELIBERATE, REASONED DEVIATION — no standalone doc carries this policy, so
this docstring is where a reviewer checks it.

FOUR FIELDS, and nothing else: app name, description, builder DISPLAY NAME (never email or
Entra object id, never handed across a user boundary elsewhere), and the live URL. Never app
code, submission ids, `app_key`, per-app DB details, project internals, or the deployment row.

Adding a field here is a deliberate act with a security consequence — the route SELECTs these
columns explicitly, so a column added to `Project`/`Deployment` later cannot silently widen
this response."""

from __future__ import annotations

from src.schemas.base import CamelModel


class MarketplaceEntry(CamelModel):
    """One published app as the catalog shows it — see the module docstring for why this list
    is short and closed. WORTH STATING PLAINLY: this widens a pre-existing accepted risk, not
    a new one. `login_required` has no enforcement reader anywhere, and a published app has
    no auth of its own — so this converts "you need the URL" into "everyone signed in has
    every URL, searchable by what the app does." No `SECURITY.md` carries that line, so it is
    recorded here, next to `url`; mitigation: an app answered Yes on a hard-block class always
    goes through administrator review first."""

    #: The app's name. `app_registry` carries no name of its own — the owning
    #: project's name IS the app name.
    name: str
    #: What the app does. Nullable: an app whose builder never wrote one still appears in
    #: the unfiltered catalog, it simply cannot be found by typing (accepted).
    #:
    #: WORTH KNOWING, and not a leak: `Project.description` was introduced as CHAT
    #: GROUNDING — private context for the builder's own assistant — and it can be
    #: model-written from the app's source. Nothing at the write surface tells the author it
    #: will be republished verbatim to everyone in the org and made searchable by it. It is
    #: the owner's own text and the enterprise-catalog framing is settled, but "the sentence
    #: I typed to orient the assistant" and "my app's public listing copy" are different acts
    #: of writing sharing one field with no notice. THAT NOTICE NOW EXISTS:
    #: `ProjectDescriptionEditor` states, at the write surface, that a published app's
    #: description becomes its Marketplace listing and is searchable org-wide
    #: — deferring the notice would leave a window where descriptions go org-wide with no
    #: warning where they are written.
    description: str | None
    #: WHO BUILT IT, by display name only. Nullable because `users.display_name` is.
    builder_display_name: str | None
    #: The live address. Non-null by construction — an entry only exists because a
    #: deployment has a URL.
    url: str


class MarketplaceListResponse(CamelModel):
    """An OFFSET page envelope — one of the two deviations `offset_pagination.py` holds.

    THE ACCEPTED RISK, stated rather than assumed away: this derived view IS written while a
    caller pages through it. `store.succeed` and `store.unpublish` write it on every deploy and
    takedown, and a UUIDv7 insert lands at position 0 under `ORDER BY id DESC`, so a page
    boundary can duplicate or skip an entry and `total` is a separate read from the page. The
    catalog is tens of rows, where that window is small. Offset also buys this endpoint a
    relevance-ranked search of more than one page, which an id cursor cannot continue."""

    items: list[MarketplaceEntry]
    #: 1-based, echoed back so a client never has to infer which page it is looking at.
    page: int
    page_size: int
    #: Rows matching the CURRENT filter, not rows in the catalog — a searched total that
    #: ignored `q` would render a page count the user can never reach.
    total: int
    total_pages: int
