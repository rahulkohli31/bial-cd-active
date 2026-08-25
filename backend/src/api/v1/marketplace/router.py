"""`GET /v1/marketplace` — the catalog of published apps, and keyword search over it (#145).

THE ONE READ ON THIS PLATFORM THAT DELIBERATELY DROPS THE `user_id` PREDICATE. Every other
list is owner-scoped (ADR-0004: cross-user access is normally an explicit, role-gated,
audited action). This route is a DELIBERATE, REASONED DEVIATION from that default — not an
oversight — argued in full in PR #147: an enterprise platform where no app is a private
document, reading a read-only, non-personal catalog, authenticated but not admin-gated.
There is no separate ADR document to amend (ADR-0004 has no standalone file in this repo,
only inline citations like this one); the deviation is recorded here, next to the code it
governs, instead.

Because that predicate is absent on purpose, the exposure surface is pinned in one place —
`MarketplaceEntry` (`schemas/marketplace.py`) — and this module SELECTs those columns
explicitly instead of returning ORM rows, so a column added to `Project` or `Deployment`
later cannot silently widen the response.

MEMBERSHIP IS DERIVED, NEVER STORED. There is no `listed` flag and no owner opt-in: an app
is in the catalog because it currently has a live deployment, and it leaves when an admin
unpublishes it (#113/#120). Nobody has to remember to do anything.

IT PAGINATES BY OFFSET, unlike every other list here. `MarketplaceListResponse`'s docstring
carries the argument; the short version is that KD-1's keyset rule protects a list you are
writing to, this catalog is read-only and small, and page numbers, totals and sort-by-name
are all impossible without it.
"""

from __future__ import annotations

import math
from typing import Annotated, Any, Literal

import sqlalchemy as sa
from fastapi import APIRouter, Query
from sqlalchemy.orm import aliased

from src.api.deps import CurrentUser, DbSession
from src.api.v1.pagination import (
    DEFAULT_PAGE_SIZE,
    LimitQuery,
    SearchQuery,
    clean_limit,
    clean_search,
)
from src.core.errors import AppApiError
from src.db.models.app_registry import AppRegistry, AppStatus
from src.db.models.deployment import Deployment, DeploymentStatus
from src.db.models.project import DESCRIPTION_TSV_REGCONFIG, Project
from src.db.models.user import User
from src.schemas import AUTH_401, ErrorEnvelope, error_responses
from src.schemas.marketplace import MarketplaceEntry, MarketplaceListResponse

router = APIRouter(prefix="/marketplace", tags=["marketplace"])

#: What the browse-order control offers. A closed set, validated rather than defaulted: a
#: typo'd `sort` must 422 rather than quietly returning newest-first, which looks to the
#: caller like the control simply does not work.
Sort = Literal["newest", "name"]

PageQuery = Annotated[int, Query()]
SortQuery = Annotated[str | None, Query()]


# Bounds `(page - 1) * limit` comfortably inside int64 so an absurd page number 422s
# instead of overflowing asyncpg's OFFSET parameter (a raw `DataError: value out of int64
# range` reaching the client as an unhandled 500 — contradicting this route's own "a page
# past the end is empty, not an error" contract). Far beyond any realistic catalog depth:
# #145 sizes the whole catalog at 10-200 rows.
MAX_PAGE = 100_000


def clean_page(value: int) -> int:
    """Reject an out-of-range `?page=` in the same `{error:{message}}` 422 shape as
    `clean_limit`/`clean_search`.

    Lives here rather than in `pagination.py` on purpose: that module is the platform's
    KEYSET contract, and putting an offset helper inside it would blur the one boundary this
    endpoint's deviation depends on staying visible.
    """
    if not 1 <= value <= MAX_PAGE:
        raise AppApiError(422, f"page must be between 1 and {MAX_PAGE}.")
    return value


def clean_sort(value: str | None) -> Sort:
    """Normalize `?sort=`; absent → `newest`, unrecognized → 422 (never a silent default)."""
    # Each branch RETURNS THE LITERAL rather than the argument. Equality against a string
    # does not narrow `str` to a `Literal` for the checkers, and the alternative — a `cast`
    # over an `in` test — would assert the correspondence instead of demonstrating it.
    if value is None or value == "newest":
        return "newest"
    if value == "name":
        return "name"
    raise AppApiError(422, "sort must be one of: newest, name.")


def _live_catalog(search: str | None) -> tuple[sa.Select[Any], Any]:
    """The catalog's membership predicate + the active filter, expressed EXACTLY ONCE.

    Both the page query and the `COUNT(*)` build on this. That is not tidiness: a total
    computed over a different predicate than the page would render page numbers the user can
    click and find empty, and the discrepancy would only appear at a page boundary.

    `deployments` is APPEND-ONLY — one row per deploy attempt, not one row per app — so
    membership is derived from a COLLAPSE, not a flat filter. Two different collapses,
    because they answer two different questions and reading either off the wrong row is a
    real bug (#147 review), not a hypothetical one:

    AN UNPUBLISH COUNTS IF IT LANDED AT OR AFTER THE REVISION BEING SHOWN. `unpublish`
    stamps whichever row was newest at the time, WHATEVER ITS STATUS (`deploy/router.py`:
    "THE ROW TO STAMP IS THE NEWEST ONE, NOT THE NEWEST SUCCEEDED ONE" — a redeploy that
    settles FAILED can still leave a container running, addressable, and billing). So the
    question is not "does the newest row carry a stamp" but "did a takedown happen after the
    thing we are about to advertise". Because ids are UUIDv7 and therefore creation-ordered,
    that is a straight comparison: the newest UNPUBLISHED row must be strictly OLDER than the
    row whose URL is being published.

    Both halves of the comparison are load-bearing, and each has a test:

      * Reading the stamp off only the newest row re-advertises a taken-down app the moment
        ANY new row appears. Unpublish DELETES the container; a redeploy's row is created at
        claim time while it is still RUNNING, so the newest row carries no stamp while the
        newest SUCCEEDED row still names an address that no longer resolves. If that attempt
        then settles FAILED, nothing ever recreates it and the dead listing is permanent.
        Unpublish -> redeploy is the documented recovery path, so this window is reachable,
        not theoretical.
      * Excluding any app with an unpublish anywhere in its history strands it forever. A
        SUCCEEDED redeploy genuinely does recreate the container at the same URL, and the
        app belongs back in the catalog — which is exactly what the comparison allows and a
        blanket exclusion would not.

    What is actually SHOWN is the newest row that SUCCEEDED with a URL. A later FAILED
    redeploy does not retract this: the pipeline creates the container app before it awaits
    the new revision, so a failed attempt commonly leaves the PREVIOUS successful revision
    still running and serving (`deploy/service.py`'s own citizen-facing copy, verbatim in
    five places: "Your previous version is still running"). Collapsing to the newest row
    REGARDLESS of status — the simpler-looking version of this query — would drop that app
    from the catalog on every failed redeploy, which is wrong for the identical reason the
    old flat-filter version could show the same app twice: both read the wrong row.

    `AppRegistry.status != AppStatus.DISABLED` is a separate axis again: `disable` (the
    admin kill-switch for a compromised or data-leaking app) severs the per-app database but
    writes nothing to `deployments`, so an app can be disabled while its most recent
    deployment row still reads `succeeded`. This predicate is scoped entirely to this query;
    `disable()` itself is unchanged — see the PR body for why that boundary was kept.

    SUSPENDED OWNERS are a decision, not an accident of the `User` join: an already-published
    app does not stop being useful to someone else merely because its builder's account is
    suspended, and de-listing on suspension has its own edge cases (an app under active use).
    So this deliberately does NOT filter on `User.suspended_at`.

    KNOWN LIMITATION, decided during planning rather than discovered in production: a
    deployment whose container was torn down out-of-band still reads `succeeded` and stays
    listed. Nothing corrects that today — `heartbeat_at` is renewed only by a RUNNING
    pipeline and freezes at settle, and the deploy reconciler sweeps only RUNNING rows — and
    the alternatives were both worse: probing every candidate on a paginated read turns a
    cheap query into an I/O fan-out, and teaching the reconciler to sweep settled rows is a
    separate piece of work. Admin unpublish is the intended correction — and, since the
    collapse above, it now actually works for a multi-deploy app.

    Returns `(query, deployment)` — `deployment` is the ORM-aliased "newest successful row
    per app" entity the query selects from. Callers need it for ordering/column selection:
    the raw `Deployment` class no longer participates in the FROM clause once the collapse
    is in place.
    """
    last_unpublished = (
        sa.select(Deployment.app_id, Deployment.id)
        .where(Deployment.unpublished_at.is_not(None))
        .distinct(Deployment.app_id)
        .order_by(Deployment.app_id, Deployment.id.desc())
        .subquery()
    )
    last_success = (
        sa.select(Deployment)
        .where(Deployment.status == DeploymentStatus.SUCCEEDED, Deployment.url.is_not(None))
        .distinct(Deployment.app_id)
        .order_by(Deployment.app_id, Deployment.id.desc())
        .subquery()
    )
    deployment = aliased(Deployment, last_success, name="last_success")

    query = (
        sa.select(deployment.id)
        .select_from(deployment)
        # OUTER: an app that has never been unpublished has no row here, and must still list.
        .outerjoin(last_unpublished, last_unpublished.c.app_id == deployment.app_id)
        .join(AppRegistry, AppRegistry.id == deployment.app_id)
        .join(Project, Project.id == AppRegistry.project_id)
        # The builder, for their display name only. INNER join: an app with no owner row is
        # not a catalog entry, it is a data-integrity problem, and it should not be listed.
        .join(User, User.id == deployment.user_id)
        .where(
            sa.or_(
                last_unpublished.c.id.is_(None),
                last_unpublished.c.id < deployment.id,
            ),
            AppRegistry.status != AppStatus.DISABLED,
        )
    )
    if search is not None:
        query = query.where(Project.description_tsv.op("@@")(_tsquery(search)))
    return query, deployment


def _tsquery(search: str) -> sa.Function[Any]:
    return sa.func.websearch_to_tsquery(DESCRIPTION_TSV_REGCONFIG, search)


def _entry(row: sa.Row[Any]) -> MarketplaceEntry:
    # `row._tuple()`, not attribute access: `Row.__getattr__` is typed to return `Any`
    # unconditionally, so `row.name`/`row.url`/etc type-check clean no matter how the row
    # is shaped — a rename or reorder in `with_only_columns` below becomes a runtime
    # `AttributeError`, not a type error. `_tuple()` carries the real tuple type, so the
    # order here must match `with_only_columns`'s order exactly.
    name, description, display_name, url, _id = row._tuple()
    return MarketplaceEntry(
        name=name,
        description=description,
        builder_display_name=display_name,
        url=url,
    )


@router.get(
    "",
    responses=error_responses(
        AUTH_401, (422, ErrorEnvelope, "Invalid page, limit, sort, or over-long q")
    ),
)
async def list_marketplace(
    user: CurrentUser,
    db: DbSession,
    page: PageQuery = 1,
    limit: LimitQuery = DEFAULT_PAGE_SIZE,
    q: SearchQuery = None,
    sort: SortQuery = None,
) -> MarketplaceListResponse:
    """Every currently-published app, or those whose description matches `q`.

    `user` is required but unused, and that is the point: the caller must be a signed-in
    BIAL user, and beyond that the catalog is the same for everyone. Authentication without
    ownership scoping is the whole feature.

    RELEVANCE OUTRANKS `sort` WHILE SEARCHING. With `q` set the order is `ts_rank_cd`
    descending, whatever `sort` says — a search box that returned alphabetical matches
    instead of good ones is not a search box. `sort` governs BROWSING, which is the mode
    where "newest" and "A-Z" are genuinely different questions.

    An app with no description is absent from search and present in the unfiltered catalog.
    That falls out of the generated column (`to_tsvector('english', coalesce(description,
    ''))` matches no query) rather than being special-cased here.

    A page past the end returns an empty `items` with the real `total`, rather than 404:
    "you scrolled past the last page" is a normal thing for a client to do while the catalog
    shrinks under it, not an error the user should be shown.
    """
    page = clean_page(page)
    limit = clean_limit(limit)
    search = clean_search(q)
    order = clean_sort(sort)

    # COUNT over the same predicate as the page — see `_live_catalog`. A fresh call, not a
    # shared query object: each call to `_live_catalog` builds its own independent collapse
    # subqueries, so the COUNT and the page can never accidentally share (and corrupt) state.
    count_query, _ = _live_catalog(search)
    total = await db.scalar(sa.select(sa.func.count()).select_from(count_query.subquery()))
    total = int(total or 0)

    catalog, deployment = _live_catalog(search)
    query = catalog.with_only_columns(
        Project.name,
        Project.description,
        User.display_name,
        deployment.url,
        deployment.id,
    )

    if search is not None:
        # Rank first; `id` breaks ties so a page boundary cannot interleave two runs of the
        # same query differently.
        query = query.order_by(
            sa.func.ts_rank_cd(Project.description_tsv, _tsquery(search)).desc(),
            deployment.id.desc(),
        )
    elif order == "name":
        # `lower()` makes the ordering COLLATION-INDEPENDENT rather than case-insensitive
        # per se. Under this database's `en_US.utf8` it changes nothing — that collation
        # already sorts linguistically, so a bare `ORDER BY name` gives the same answer, and
        # no test can tell the two apart here. Under `C` collation it would matter: byte
        # ordering puts every capital ahead of every lowercase, so "Zebra" would sort before
        # "apple". Kept so the answer does not depend on how a database was initialised.
        query = query.order_by(sa.func.lower(Project.name).asc(), deployment.id.desc())
    else:
        query = query.order_by(deployment.id.desc())

    rows = (await db.execute(query.offset((page - 1) * limit).limit(limit))).all()

    return MarketplaceListResponse(
        items=[_entry(row) for row in rows],
        page=page,
        page_size=limit,
        total=total,
        total_pages=max(1, math.ceil(total / limit)),
    )
