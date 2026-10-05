"""`GET /v1/marketplace` — the catalog of published apps, and keyword search over it.

THE ONE READ THAT DELIBERATELY DROPS THE `user_id` PREDICATE — the deviation the v1
router's isolation rule points here to find. An enterprise platform where no app is a
private document, reading a read-only, non-personal catalog, authenticated but not
admin-gated; recorded beside the code it governs because the platform's isolation
rule has no other file to amend. Because the predicate is absent, the exposure surface
is pinned in `MarketplaceEntry` and this module SELECTs those columns explicitly rather
than returning ORM rows.

Membership is DERIVED, never stored: an app is listed while it has a live deployment.
Pagination is by offset, one of the two deviations `pagination.py` names."""

from __future__ import annotations

import math
from typing import Annotated, Any, Literal

import sqlalchemy as sa
import structlog
from fastapi import APIRouter, Query
from pydantic_ai import Embedder

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
from src.db.models.deployment import Deployment
from src.db.models.project import DESCRIPTION_TSV_REGCONFIG, Project
from src.db.models.user import User
from src.schemas import AUTH_401, ErrorEnvelope, error_responses
from src.schemas.marketplace import MarketplaceEntry, MarketplaceListResponse
from src.services.deploy.liveness import last_success_deployment, live_app_ids
from src.services.embeddings import EmbedderDep

logger = structlog.get_logger()

router = APIRouter(prefix="/marketplace", tags=["marketplace"])

#: What the browse-order control offers. A closed set, validated rather than defaulted: a
#: typo'd `sort` must 422 rather than quietly returning newest-first, which looks to the
#: caller like the control simply does not work.
Sort = Literal["newest", "name"]

PageQuery = Annotated[int, Query()]
SortQuery = Annotated[
    str | None,
    # The closed set is named in the schema even though the type is `str | None`: validation
    # lives in `clean_sort` so the 422 keeps this platform's `ErrorEnvelope` shape rather
    # than FastAPI's, which means OpenAPI would otherwise advertise a free-form string and a
    # generated client could not see the two legal values.
    Query(description="Browse order. One of: newest (default), name."),
]


# Bounds `(page - 1) * limit` comfortably inside int64 so an absurd page number 422s
# instead of overflowing asyncpg's OFFSET parameter (a raw `DataError: value out of int64
# range` reaching the client as an unhandled 500 — contradicting this route's own "a page
# past the end is empty, not an error" contract). Far beyond any realistic catalog depth.
MAX_PAGE = 100_000


def clean_page(value: int) -> int:
    """Reject an out-of-range `?page=` in the same `{error:{message}}` 422 shape as
    `clean_limit`/`clean_search`.

    Lives here rather than in `pagination.py`: that module is the keyset contract, and an
    offset helper inside it would blur the boundary this endpoint deviates across.
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


def _live_catalog() -> tuple[sa.Select[*tuple[Any, ...]], type[Deployment]]:
    """The catalog's membership predicate, expressed EXACTLY ONCE — the BROWSE path only
    (no `q`). Search (#191 slice 3) no longer filters this query; it is answered by
    `_hybrid_catalog` below instead of a `WHERE ... @@ ...` bolted onto this one, because a
    flat filter on the membership query is exactly the shape that GATES the candidate set
    on the keyword predicate — the thing requirement 27 says a hybrid search must not do.

    Both the page query and the `COUNT(*)` build on this: a total computed over a different
    predicate than the page renders page numbers a reader can click and find empty.

    Returns `(query, deployment)` — `deployment` is the ORM-aliased newest-successful-row-per-app
    entity the query selects from, which callers need for ordering and column selection.
    """
    # MEMBERSHIP IS `live_app_ids()`, NOT A SECOND COPY OF IT — and the row projection below
    # is `last_success_deployment()`, not a third copy of ITS collapse either (moved to
    # `services/deploy/liveness.py` so the duplicate check reuses the identical thing rather
    # than a second inline copy of it; see that function's docstring for the full history —
    # the literal-status-predicate reasoning, the measured plan-cache regression, and why the
    # membership/projection split is what keeps this catalog from silently disagreeing with
    # the projects list and the dashboard count about whether an app is live).
    deployment = last_success_deployment()

    query = (
        sa.select(deployment.id)
        .select_from(deployment)
        .join(AppRegistry, AppRegistry.id == deployment.app_id)
        .join(Project, Project.id == AppRegistry.project_id)
        # The builder, for their display name only. INNER join: an app with no owner row is
        # not a catalog entry, it is a data-integrity problem, and it should not be listed.
        # `User.suspended_at` is deliberately NOT filtered: an app stays useful to everyone
        # else when its builder's account is suspended.
        .join(User, User.id == deployment.user_id)
        # The takedown comparison and both registry predicates now live in ONE place. A
        # semi-join, so Postgres still uses the same two partial indexes (migration 0034).
        # A container torn down outside the platform still reads `succeeded` and stays
        # listed; nothing sweeps settled rows, and admin unpublish is the correction.
        .where(AppRegistry.id.in_(live_app_ids()))
    )
    return query, deployment


def _tsquery(search: str) -> sa.Function[Any]:
    return sa.func.websearch_to_tsquery(DESCRIPTION_TSV_REGCONFIG, search)


# RRF's own constant (#191 R27) — NOT a tuning knob, a property of the formula: it
# dampens how much a #1 rank in one arm can dominate the fused score over a #1-in-both
# case, and 60 is the value the RRF literature (and Microsoft's/Supabase's own hybrid-
# search reference implementations, which R27's docstring below cites) converges on.
_RRF_K = 60

# Each arm's own top-N before fusion. Sized to the WHOLE catalog (#145/#191 size it at
# 10-200 rows) rather than tuned down, so for any realistic catalog size neither arm ever
# actually truncates real candidates — R28's "cap the fused pool" is satisfied by this
# being a generous ceiling, not by it binding in practice.
_ARM_POOL_CAP = 200


def _hybrid_catalog(
    search: str, query_embedding: list[float] | None
) -> tuple[sa.Select[*tuple[Any, ...]], type[Deployment]]:
    """The fused (keyword + semantic) ranking for one search query (#191 slice 3, R27),
    joined onto the same last-success deployment/project/user projection `_live_catalog`
    uses for browsing — everything downstream of `(query, deployment)` is identical
    between the two paths.

    TWO INDEPENDENT ARMS, each its own top-`_ARM_POOL_CAP` candidate set from its own
    index: the keyword arm bounded by `description_tsv @@ tsquery` and ordered by
    `ts_rank_cd`, the vector arm by an UNFILTERED `cosine_distance` ordering (no similarity
    floor — that belongs to the duplicate check, slice 4, not to marketplace search). THE
    `@@` PREDICATE LIVES ONLY INSIDE THE KEYWORD ARM'S OWN CTE, never as a filter on the
    fused query itself — a row absent from the keyword arm (no shared vocabulary with the
    query) can still surface purely through the vector arm, which is the entire point of
    hybrid search. This is the documented pattern in Microsoft's own Azure AI Search hybrid
    guidance and Supabase's hybrid-search reference implementation, not a workaround.

    THE TWO ID SETS ARE COMBINED WITH A FULL OUTER JOIN so a row present in only one arm
    still surfaces (R30 — every pre-#191 project has no embedding at all, and must not be
    excluded from keyword results by the fusion), and reciprocal rank fusion scores the
    union: `1/(k+rank)` per arm the row appears in, `0` for an arm it is absent from
    (COALESCE — "treating an arm a row is absent from as unranked", not zero-similarity,
    which would be a claim the missing arm never made).

    `query_embedding` IS `None` when embeddings are unconfigured or the query's own embed
    call failed (both R26-style accepted degrades) — the vector arm is simply omitted, and
    the fused score collapses to the keyword arm's own RRF contribution alone. Built as ONE
    fused shape either way (not two separate query-building code paths) so keyword-only
    degrade can never quietly drift from the real hybrid ranking.
    """
    deployment = last_success_deployment()
    live = live_app_ids()

    kw_rank_expr = sa.func.ts_rank_cd(Project.description_tsv, _tsquery(search)).desc()
    kw_arm = (
        sa.select(
            AppRegistry.id.label("app_id"),
            sa.func.row_number().over(order_by=kw_rank_expr).label("rank"),
        )
        .select_from(AppRegistry)
        .join(Project, Project.id == AppRegistry.project_id)
        .where(
            AppRegistry.id.in_(live),
            Project.description_tsv.op("@@")(_tsquery(search)),
        )
        .order_by(kw_rank_expr)
        .limit(_ARM_POOL_CAP)
        .cte("kw_arm")
    )

    if query_embedding is not None:
        vec_rank_expr = Project.description_embedding.cosine_distance(query_embedding)
        vec_arm = (
            sa.select(
                AppRegistry.id.label("app_id"),
                sa.func.row_number().over(order_by=vec_rank_expr).label("rank"),
            )
            .select_from(AppRegistry)
            .join(Project, Project.id == AppRegistry.project_id)
            .where(
                AppRegistry.id.in_(live),
                Project.description_embedding.is_not(None),
            )
            .order_by(vec_rank_expr)
            .limit(_ARM_POOL_CAP)
            .cte("vec_arm")
        )
        fused = (
            sa.select(
                sa.func.coalesce(kw_arm.c.app_id, vec_arm.c.app_id).label("app_id"),
                (
                    sa.func.coalesce(1.0 / (_RRF_K + kw_arm.c.rank), 0.0)
                    + sa.func.coalesce(1.0 / (_RRF_K + vec_arm.c.rank), 0.0)
                ).label("rrf_score"),
            )
            .select_from(kw_arm.outerjoin(vec_arm, kw_arm.c.app_id == vec_arm.c.app_id, full=True))
            .cte("fused")
        )
    else:
        fused = sa.select(
            kw_arm.c.app_id.label("app_id"),
            (1.0 / (_RRF_K + kw_arm.c.rank)).label("rrf_score"),
        ).cte("fused")

    query = (
        sa.select(deployment.id, fused.c.rrf_score)
        .select_from(fused)
        .join(deployment, deployment.app_id == fused.c.app_id)
        .join(AppRegistry, AppRegistry.id == fused.c.app_id)
        .join(Project, Project.id == AppRegistry.project_id)
        .join(User, User.id == deployment.user_id)
        # Fused score, always — a search box that returned anything else while `q` is set
        # is not a search box (the same rule the pre-#191 `ts_rank_cd`-only ordering
        # already applied). `id` breaks ties so a page boundary cannot interleave two runs
        # differently. Baked in HERE, not left to the caller, because relevance ordering
        # is not something a search result should ever vary by `sort`.
        .order_by(fused.c.rrf_score.desc(), deployment.id.desc())
    )
    return query, deployment


def _entry(row: sa.Row[*tuple[Any, ...]]) -> MarketplaceEntry:
    # `row._tuple()`, not attribute access — but be precise about what that buys: on a `Row`
    # of `Any` columns, `_tuple()` is typed `tuple[Any, ...]`, so NEITHER the arity nor
    # the order below is checked statically; swapping two same-typed columns in
    # `with_only_columns` passes mypy clean.
    #
    # What it does buy is still worth having, and it is runtime + tests rather than types:
    # an arity change raises a loud `ValueError` here instead of a silent `AttributeError`
    # at attribute-access time, and a reorder is caught by
    # `test_a_signed_in_user_sees_an_app_built_by_someone_else`. The order here must match
    # `with_only_columns`'s order exactly.
    name, description, display_name, url = row._tuple()
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
    embedder: EmbedderDep,
    page: PageQuery = 1,
    limit: LimitQuery = DEFAULT_PAGE_SIZE,
    q: SearchQuery = None,
    sort: SortQuery = None,
) -> MarketplaceListResponse:
    """Every currently-published app, or those whose description matches `q` — HYBRID
    (keyword + semantic) as of #191 slice 3, not `ts_rank_cd` alone.

    `user` is required but unused, and that is the point: the caller must be a signed-in
    BIAL user, and beyond that the catalog is the same for everyone. Authentication without
    ownership scoping is the whole feature.

    RELEVANCE OUTRANKS `sort` WHILE SEARCHING. With `q` set the order is the fused RRF
    score (`_hybrid_catalog`), whatever `sort` says — a search box that returned
    alphabetical matches instead of good ones is not a search box. `sort` governs BROWSING,
    which is the mode where "newest" and "A-Z" are genuinely different questions.

    THE QUERY IS EMBEDDED HERE, ONCE, before either arm runs — never inside `_hybrid_catalog`
    itself, which stays a pure query builder. A failed embed call (network, rate limit, a
    misconfigured deployment) degrades to keyword-only rather than 500ing the search: the
    same accepted-degrade shape R26 already established for the WRITE path, applied here to
    the READ side of the same feature.

    An app with no description is absent from search and present in the unfiltered catalog
    — true of both arms: an empty `description_tsv` matches no `tsquery`, and a NULL
    `description_embedding` is excluded from the vector arm's own `IS NOT NULL` filter.

    NO CORPUS-WIDE `total` WHILE SEARCHING (R28). RRF has no honest notion of "how many
    rows match" — only "how many candidates the two bounded arms produced" — so `total`
    here counts the FUSED POOL itself (bounded by `_ARM_POOL_CAP` per arm), not a second,
    differently-shaped COUNT query. For this catalog's documented 10-200 row size that pool
    is the whole catalog in practice, so paging within it behaves exactly like paging a
    real total; it just does not claim to be one for a catalog that outgrows the cap.

    TWO 422 ENVELOPES REACH THIS ROUTE, and `responses=` can only document one. An
    out-of-range `page`/`limit`/`sort` raises through `clean_*` and carries this platform's
    `{"error":{"message":...}}`; a NON-NUMERIC `?page=abc` never reaches `clean_page` at all,
    because FastAPI's own int coercion fails first and emits `{"detail":[...]}`. So the
    declaration below is accurate for out-of-range and inaccurate for non-numeric. Inherited
    from `pagination.py`'s `LimitQuery` rather than invented here, and the portal's
    `apiError.ts` already tolerates both shapes (#147 round 3).

    A page past the end returns an empty `items` with the real `total`, rather than 404:
    "you scrolled past the last page" is a normal thing for a client to do while the catalog
    shrinks under it, not an error the user should be shown.
    """
    page = clean_page(page)
    limit = clean_limit(limit)
    search = clean_search(q)
    order = clean_sort(sort)

    if search is not None:
        # THE CHEAP QUESTION FIRST (review of #191, agc129 — the same fix as the duplicate
        # check's blocker #7). An empty catalog is the normal day-one state, not an edge
        # case, and embedding a search box query before asking "is there anything published
        # at all" spends a Foundry round trip — and its own outage mode — ranking nothing.
        # Same `live_app_ids()` predicate both arms already scope to, so this can never
        # drift from the corpus the search itself answers over.
        catalog_has_any_app = await db.scalar(
            sa.select(sa.exists().where(AppRegistry.id.in_(live_app_ids())))
        )
        query_embedding = (
            await _embed_query_or_none(embedder, search) if catalog_has_any_app else None
        )
        catalog, deployment = _hybrid_catalog(search, query_embedding)
        # The fused pool IS the bounded universe a page walks while searching — see the
        # docstring above on why this is not a second, corpus-wide COUNT (R28).
        total = int(
            await db.scalar(sa.select(sa.func.count()).select_from(catalog.subquery())) or 0
        )
        query = catalog.with_only_columns(
            Project.name, Project.description, User.display_name, deployment.url
        )
    else:
        # COUNT over the same predicate as the page — see `_live_catalog`. A fresh call,
        # not a shared query object: each call builds its own independent collapse
        # subqueries, so the COUNT and the page can never accidentally share (and corrupt)
        # state.
        count_query, _ = _live_catalog()
        total = int(
            await db.scalar(sa.select(sa.func.count()).select_from(count_query.subquery())) or 0
        )
        catalog, deployment = _live_catalog()
        query = catalog.with_only_columns(
            Project.name, Project.description, User.display_name, deployment.url
        )
        if order == "name":
            # `lower()` makes the ordering COLLATION-INDEPENDENT rather than case-insensitive
            # per se. Under this database's `en_US.utf8` it changes nothing — that collation
            # already sorts linguistically, so a bare `ORDER BY name` gives the same answer,
            # and no test can tell the two apart here. Under `C` collation it would matter:
            # byte ordering puts every capital ahead of every lowercase, so "Zebra" would
            # sort before "apple". Kept so the answer does not depend on how a database was
            # initialised.
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


async def _embed_query_or_none(embedder: Embedder | None, search: str) -> list[float] | None:
    """Embed the search box's text as a QUERY (R21 — `input_type="query"`, distinct from
    the `"document"` type the write path and the duplicate check use on stored
    descriptions), or `None` on any failure. NEVER RAISES: the marketplace must still
    answer with keyword-only results when embeddings are unconfigured or the call itself
    fails — a search box going down because an embedding model hiccuped would be a worse
    failure than a temporarily keyword-only one.
    """
    if embedder is None:
        return None
    try:
        result = await embedder.embed_query(search)
        return list(result.embeddings[0])
    except Exception:  # noqa: BLE001 — degrade to keyword-only, never fail the search
        logger.warning("marketplace_search_embedding_unavailable")
        return None
