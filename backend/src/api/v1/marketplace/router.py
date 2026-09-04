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
from src.db.models.app_registry import AppRegistry
from src.db.models.deployment import Deployment, DeploymentStatus
from src.db.models.project import DESCRIPTION_TSV_REGCONFIG, Project
from src.db.models.user import User
from src.schemas import AUTH_401, ErrorEnvelope, error_responses
from src.schemas.marketplace import MarketplaceEntry, MarketplaceListResponse
from src.services.deploy.liveness import live_app_ids

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
    # generated client could not see the two legal values (#147 round 3).
    Query(description="Browse order. One of: newest (default), name."),
]


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


def _live_catalog(search: str | None) -> tuple[sa.Select[Any], type[Deployment]]:
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

    THE COMPARISON RESTS ON AN INVARIANT WORTH NAMING, because it lives nowhere near this
    line and breaks silently: Postgres `uuid` ordering equals creation order only because
    every row here is minted by CPython's in-process monotonic `uuid7()` via
    `store._try_claim` (the only insert path), serialised per app by
    `uq_deployments_one_in_flight`, on a control plane that is SINGLE-REPLICA BY DESIGN.
    A second API replica reopens this: two hosts minting ids from independent clocks can
    interleave out of order, and an out-of-order id breaks the comparison in BOTH directions
    (a taken-down app listed at a dead URL, or a live one hidden). Whoever scales the control
    plane needs to revisit this predicate, not just the deployment topology.

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

    THE REGISTRY PREDICATES ARE A SEPARATE AXIS, and this is where the containment story is
    weaker than it looks — stated plainly here because the previous version of this docstring
    overstated it (#147 round 3).

    Two lifecycle facts are read, because one of them cannot be trusted alone:

      * `status` NOT IN (DISABLED, REJECTED). `disable` severs the per-app database and
        `reject` writes REJECTED; neither writes anything to `deployments`, so either can be
        true while the newest deployment row still reads `succeeded`.
      * `rejection_standing IS FALSE`. `status` is MUTABLE lifecycle state and cannot carry a
        policy fact: `app_registry.py` says the column exists because reading the fact off
        `status` "is what let a reject->publish->withdraw round trip launder a rejection and
        publish unattended." That round trip ends with `status` back at DRAFT, which the
        first predicate would list.

    WHAT THIS DOES NOT GIVE YOU: `disable` and `reject` are NOT available for the ordinary
    member of this catalog. `STATUS_TRANSITIONS[DISABLED] == {APPROVED}`, so
    `admin/router.py` answers 409 "Only an approved app can be disabled" for anything else
    — and a one-click deploy never writes `status` at all (`deployment.py`: "there is no
    admin approval on this path... a self-deployed app is still `draft`"). A DRAFT app
    cannot be rejected either (`STATUS_TRANSITIONS[DRAFT] == {PENDING}`).

    WHAT AN ADMIN CAN DO TODAY, stated precisely because this is the paragraph someone reads
    during an incident: `unpublish` + `deactivate` IS a working, durable takedown.
    `POST /v1/admin/apps/{id}/unpublish` carries no `AppStatus` guard at all, so it returns
    200 on a self-published DRAFT app, deletes the container, and drops it from browse and
    search. `deploy/router.py` disclaims it as "AN OPERATOR CONVENIENCE, NOT AN ENFORCEMENT
    LEVER" because on its own the owner can republish one click later — so pair it with
    `POST /v1/admin/users/{id}/deactivate`, which stamps `suspended_at`, bumps
    `token_version` and revokes every refresh family, and the redeploy fails in the shared
    auth dependency. (An earlier draft of this docstring said `unpublish` was the only lever
    and undersold it; that was wrong, and it is corrected here rather than left to mislead.)

    Widening `STATUS_TRANSITIONS[DISABLED]` to accept DRAFT/PENDING/REJECTED would give an
    admin the ADVERTISING switch directly instead of via the takedown pair. Filed as #163,
    and NOT a one-liner: `enable` returns DISABLED -> APPROVED guarded on
    `approved_submission_id IS NOT NULL`, which a self-published app never has, so widening
    `disable` alone strands the app in DISABLED for good. Un-sticking that needs DISABLED in
    `STATUS_TRANSITIONS[DRAFT]` — which `withdraw` also reads, and `withdraw` is
    citizen-facing, so it would let an app's OWNER undo an admin kill switch. The fix is a
    lifecycle decision, not a predicate change, which is why it is not in a catalog PR.

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
    # MEMBERSHIP IS `live_app_ids()`, NOT A SECOND COPY OF IT. This function used to carry its
    # own `last_unpublished` collapse and its own registry predicates, which is how the
    # catalog could drift into disagreeing with the projects list and the dashboard count
    # about whether an app is live — silently, and only for some apps. `liveness.py` says it
    # answers that question "in one place"; this is what makes the sentence true.
    #
    # `last_success` BELOW STAYS, and it is not a second membership rule: it is the row
    # PROJECTION. The catalog lists a deployment's `url` and its builder, so it needs the
    # actual newest-succeeded row, which a select of `app_id`s cannot give it. Membership
    # decides WHICH apps; this decides WHAT is shown for each.
    last_success = (
        sa.select(Deployment)
        # THE `status` PREDICATE MUST RENDER AS A LITERAL, which is what `literal_execute`
        # buys and a plain `Deployment.status == ...` does not. That renders `status = $1`,
        # and asyncpg prepares server-side against a long-lived pool (`pool_size=20`, no
        # recycle), so from the 6th execution on a connection Postgres plans generically. A
        # generic plan cannot prove `status = $1` implies `ix_deployments_success_collapse`'s
        # `status = 'succeeded'` predicate, so it drops the index and falls back to a Seq
        # Scan — measured at 5.2k apps / 52k rows as 13-15ms for executions 1-5 and 27-30ms
        # from execution 6 (#147 round 3). `deploy/store.py`'s `_IN_FLIGHT_PREDICATE` renders
        # a literal too, but for a DIFFERENT reason: it is only ever an `index_where=` on an
        # ON CONFLICT, and arbiter inference is a compile-time syntactic match, so it faces
        # no plan-cache risk at all. Same remedy, different cause — neither one is evidence
        # that the other is handled.
        #
        # `literal_execute` rather than that module's `sa.text`: it renders the identical
        # SQL while keeping the value the ENUM, so renaming `SUCCEEDED` moves the predicate
        # with it instead of leaving a string that silently matches nothing.
        #
        # Nothing here fails loudly if this regresses — the answer stays correct and only
        # the plan degrades — so `test_the_success_collapse_predicate_renders_a_literal`
        # pins the compiled SQL.
        .where(
            Deployment.status
            == sa.bindparam("succeeded", DeploymentStatus.SUCCEEDED, literal_execute=True),
            Deployment.url.is_not(None),
        )
        .distinct(Deployment.app_id)
        .order_by(Deployment.app_id, Deployment.id.desc())
        .subquery()
    )
    deployment = aliased(Deployment, last_success, name="last_success")

    query = (
        sa.select(deployment.id)
        .select_from(deployment)
        .join(AppRegistry, AppRegistry.id == deployment.app_id)
        .join(Project, Project.id == AppRegistry.project_id)
        # The builder, for their display name only. INNER join: an app with no owner row is
        # not a catalog entry, it is a data-integrity problem, and it should not be listed.
        .join(User, User.id == deployment.user_id)
        # The takedown comparison and both registry predicates now live in ONE place. A
        # semi-join, so Postgres still uses the same two partial indexes (migration 0034).
        .where(AppRegistry.id.in_(live_app_ids()))
    )
    if search is not None:
        query = query.where(Project.description_tsv.op("@@")(_tsquery(search)))
    return query, deployment


def _tsquery(search: str) -> sa.Function[Any]:
    return sa.func.websearch_to_tsquery(DESCRIPTION_TSV_REGCONFIG, search)


def _entry(row: sa.Row[Any]) -> MarketplaceEntry:
    # `row._tuple()`, not attribute access — but be precise about what that buys, because
    # this comment is the module's stated defence and the previous version overclaimed it
    # (#147 round 3). On an `Any`-parameterised `Row`, `_tuple()` is itself typed `Any`, so
    # NEITHER the arity nor the order below is checked statically; swapping two same-typed
    # columns in `with_only_columns` passes mypy clean.
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
