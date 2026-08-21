"""`GET /v1/marketplace` — the catalog of published apps, and keyword search over it (#145).

THE ONE READ ON THIS PLATFORM THAT DELIBERATELY DROPS THE `user_id` PREDICATE. Every other
list is owner-scoped (ADR-0004); this one exists precisely because that scoping means
nothing in the product can answer "what has anyone else already built?", so people rebuild
tools that are already running. It is authenticated but NOT admin-gated: this is an
enterprise platform, no app on it is personal, and the missing catalog is a gap rather than
a privacy feature.

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

from src.api.deps import CurrentUser, DbSession
from src.api.v1.pagination import (
    DEFAULT_PAGE_SIZE,
    LimitQuery,
    SearchQuery,
    clean_limit,
    clean_search,
)
from src.core.errors import AppApiError
from src.db.models.app_registry import AppRegistry
from src.db.models.deployment import Deployment, DeploymentStatus
from src.db.models.project import Project
from src.db.models.user import User
from src.schemas import AUTH_401, ErrorEnvelope, error_responses
from src.schemas.marketplace import MarketplaceEntry, MarketplaceListResponse

router = APIRouter(prefix="/marketplace", tags=["marketplace"])

# The search configuration, named once. It must match the one the generated `description_tsv`
# column was built with (migration 0029) — a query parsed under a different configuration
# stems differently and silently under-matches.
_REGCONFIG = "english"

#: What the browse-order control offers. A closed set, validated rather than defaulted: a
#: typo'd `sort` must 422 rather than quietly returning newest-first, which looks to the
#: caller like the control simply does not work.
Sort = Literal["newest", "name"]

PageQuery = Annotated[int, Query()]
SortQuery = Annotated[str | None, Query()]


def clean_page(value: int) -> int:
    """Reject a non-positive `?page=` in the same `{error:{message}}` 422 shape as
    `clean_limit`/`clean_search`.

    Lives here rather than in `pagination.py` on purpose: that module is the platform's
    KEYSET contract, and putting an offset helper inside it would blur the one boundary this
    endpoint's deviation depends on staying visible.
    """
    if value < 1:
        raise AppApiError(422, "page must be 1 or greater.")
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


def _live_catalog(search: str | None) -> sa.Select[Any]:
    """The catalog's membership predicate + the active filter, expressed EXACTLY ONCE.

    Both the page query and the `COUNT(*)` build on this. That is not tidiness: a total
    computed over a different predicate than the page would render page numbers the user can
    click and find empty, and the discrepancy would only appear at a page boundary.

    An app is listed because it currently has a live deployment: a `succeeded` attempt that
    produced a URL and has not been taken down. `unpublished_at` is a second axis from
    `status` (#113) — a taken-down app keeps its `succeeded` status, so filtering on status
    alone would keep listing an app an admin has deliberately pulled.

    KNOWN LIMITATION, decided during planning rather than discovered in production: a
    deployment whose container was torn down out-of-band still reads `succeeded` and stays
    listed. Nothing corrects that today — `heartbeat_at` is renewed only by a RUNNING
    pipeline and freezes at settle, and the deploy reconciler sweeps only RUNNING rows — and
    the alternatives were both worse: probing every candidate on a paginated read turns a
    cheap query into an I/O fan-out, and teaching the reconciler to sweep settled rows is a
    separate piece of work. Admin unpublish is the intended correction.
    """
    query = (
        sa.select(Deployment.id)
        .select_from(Deployment)
        .join(AppRegistry, AppRegistry.id == Deployment.app_id)
        .join(Project, Project.id == AppRegistry.project_id)
        # The builder, for their display name only. INNER join: an app with no owner row is
        # not a catalog entry, it is a data-integrity problem, and it should not be listed.
        .join(User, User.id == Deployment.user_id)
        .where(
            Deployment.status == DeploymentStatus.SUCCEEDED,
            Deployment.url.is_not(None),
            Deployment.unpublished_at.is_(None),
        )
    )
    if search is not None:
        query = query.where(Project.description_tsv.op("@@")(_tsquery(search)))
    return query


def _tsquery(search: str) -> sa.Function[Any]:
    return sa.func.websearch_to_tsquery(_REGCONFIG, search)


def _entry(row: sa.Row[Any]) -> MarketplaceEntry:
    return MarketplaceEntry(
        name=row.name,
        description=row.description,
        builder_display_name=row.display_name,
        url=row.url,
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

    # COUNT over the same predicate as the page — see `_live_catalog`.
    total = await db.scalar(
        sa.select(sa.func.count()).select_from(_live_catalog(search).subquery())
    )
    total = int(total or 0)

    query = _live_catalog(search).with_only_columns(
        Project.name,
        Project.description,
        User.display_name,
        Deployment.url,
        Deployment.id,
    )

    if search is not None:
        # Rank first; `id` breaks ties so a page boundary cannot interleave two runs of the
        # same query differently.
        query = query.order_by(
            sa.func.ts_rank_cd(Project.description_tsv, _tsquery(search)).desc(),
            Deployment.id.desc(),
        )
    elif order == "name":
        # `lower()` makes the ordering COLLATION-INDEPENDENT rather than case-insensitive
        # per se. Under this database's `en_US.utf8` it changes nothing — that collation
        # already sorts linguistically, so a bare `ORDER BY name` gives the same answer, and
        # no test can tell the two apart here. Under `C` collation it would matter: byte
        # ordering puts every capital ahead of every lowercase, so "Zebra" would sort before
        # "apple". Kept so the answer does not depend on how a database was initialised.
        query = query.order_by(sa.func.lower(Project.name).asc(), Deployment.id.desc())
    else:
        query = query.order_by(Deployment.id.desc())

    rows = (await db.execute(query.offset((page - 1) * limit).limit(limit))).all()

    return MarketplaceListResponse(
        items=[_entry(row) for row in rows],
        page=page,
        page_size=limit,
        total=total,
        total_pages=max(1, math.ceil(total / limit)),
    )
