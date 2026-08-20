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
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from fastapi import APIRouter

from src.api.deps import CurrentUser, DbSession
from src.api.v1.pagination import (
    DEFAULT_PAGE_SIZE,
    CursorQuery,
    LimitQuery,
    SearchQuery,
    clean_limit,
    clean_search,
    parse_cursor,
    split_keyset,
)
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


def _live_catalog_query() -> sa.Select[Any]:
    """The catalog's membership predicate, and the ONLY place it is expressed.

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
    return (
        sa.select(
            Project.name,
            Project.description,
            User.display_name,
            Deployment.url,
            Deployment.id,
        )
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
        AUTH_401, (422, ErrorEnvelope, "Invalid pagination cursor or over-long q")
    ),
)
async def list_marketplace(
    user: CurrentUser,
    db: DbSession,
    cursor: CursorQuery = None,
    limit: LimitQuery = DEFAULT_PAGE_SIZE,
    q: SearchQuery = None,
) -> MarketplaceListResponse:
    """Every currently-published app, or those whose description matches `q`.

    `user` is required but unused, and that is the point: the caller must be a signed-in
    BIAL user, and beyond that the catalog is the same for everyone. Authentication without
    ownership scoping is the whole feature.

    TWO ORDERINGS, because the question differs:

    * **No `q`** — the full catalog, newest first, keyset-paginated on `Deployment.id`
      (UUIDv7, time-sortable) exactly like every other list here.
    * **With `q`** — ranked by relevance, best match first, NOT by recency. Parsed with
      `websearch_to_tsquery` so quoted phrases and `-negation` behave the way people expect
      from a search box, and ranked with `ts_rank_cd`.

    A SEARCH RESPONSE IS ONE PAGE, and `nextCursor` is null. This is a deliberate limit, not
    an omission: keyset pagination continues from the last row's id, but a relevance-ranked
    result is not ordered by id, so an id cursor cannot describe "the next page" of it.
    Carrying rank in the cursor would be the general fix; at the ~10-200 published apps this
    catalog is sized for, a single ranked page of up to `limit` best matches answers the
    question a search box is asking, and the unfiltered catalog — the surface that genuinely
    needs to walk everything — keeps full pagination.

    An app with no description is absent from search and present in the unfiltered catalog.
    That falls out of the generated column (`to_tsvector('english', coalesce(description,
    ''))` matches no query) rather than being special-cased here.
    """
    after = parse_cursor(cursor)
    search = clean_search(q)
    limit = clean_limit(limit)

    query = _live_catalog_query()

    if search is not None:
        tsquery = sa.func.websearch_to_tsquery(_REGCONFIG, search)
        # `Project.description_tsv` is the STORED generated column from migration 0029, so
        # the match rides the GIN index rather than re-tokenizing every description per query.
        rank = sa.func.ts_rank_cd(Project.description_tsv, tsquery)
        rows = (
            await db.execute(
                query.where(Project.description_tsv.op("@@")(tsquery))
                .order_by(rank.desc(), Deployment.id.desc())
                .limit(limit)
            )
        ).all()
        # No cursor: see the docstring. `hasMore` stays False rather than lying about a
        # next page the caller has no way to ask for.
        return MarketplaceListResponse(
            items=[_entry(row) for row in rows], next_cursor=None, has_more=False
        )

    if after is not None:
        query = query.where(Deployment.id < after)
    rows = (await db.execute(query.order_by(Deployment.id.desc()).limit(limit + 1))).all()
    page, next_cursor, has_more = split_keyset(rows, limit, key=lambda row: row.id)
    return MarketplaceListResponse(
        items=[_entry(row) for row in page], next_cursor=next_cursor, has_more=has_more
    )
