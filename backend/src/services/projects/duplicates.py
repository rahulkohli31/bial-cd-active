"""The duplicate check at project creation (#191 slice 4, R31-R37).

Searches the LIVE MARKETPLACE SET (R32 — exactly `live_app_ids()`, never reimplemented) for
apps that look like the description a citizen just typed, before their project is created.
Built on the same two-arm hybrid shape `api/v1/marketplace/router.py::_hybrid_catalog` uses
for marketplace search, but answers a different question: search asks "rank everything by
relevance", this asks "is any ONE of these confidently the same app" — so it keeps each arm's
OWN NATIVE SCORE (not just its RRF-fused rank) and applies a confidence bar per candidate
(R34) instead of returning a ranked page.

NEVER RAISES (R37): a search or embedding failure here must never be the reason a citizen
cannot start a project, so every failure mode collapses to "no duplicate found".
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
import structlog
from pydantic_ai import Embedder
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.app_registry import AppRegistry
from src.db.models.project import DESCRIPTION_TSV_REGCONFIG, Project
from src.db.models.user import User
from src.schemas.marketplace import MarketplaceEntry
from src.services.deploy.liveness import last_success_deployment, live_app_ids

logger = structlog.get_logger()

#: R39's two pinned events — greppable constants, imported rather than retyped, mirroring
#: `core/alarms.py`'s doctrine. Not IN that module: these are not alarms (nothing failed),
#: and both the log site and every test importing them live in this one feature's own
#: package, so the cross-package leaf-module treatment `core/alarms.py` exists for buys
#: nothing here.
DUPLICATE_MATCHES_SHOWN_EVENT = "duplicate_check_matches_shown"
DUPLICATE_CHECK_RESOLVED_EVENT = "duplicate_check_resolved"

MAX_MATCHES = 3
"""R34 — "at most three matches are shown"."""

_CANDIDATE_WINDOW = 10
"""Each arm's own top-N considered for the confidence bar (NOT the marketplace search's
200 — this only ever needs to find up to `MAX_MATCHES` confident matches, not rank a whole
result page)."""

_AGREEMENT_TOP_N = 5
"""Both arms placing a candidate in their OWN top-N is a confident match on its own — no
native-score threshold needed. Tighter than `_CANDIDATE_WINDOW` so "agreement" means a real
top tier in both rankings, not merely "present somewhere in the fetched window"."""

# SINGLE-ARM THRESHOLDS, each in that arm's OWN units — never a fused/RRF score (R34
# forbids a flat fused-score cutoff: "RRF scores are rank-derived with no natural zero").
#
# Cosine similarity is `1 - cosine_distance`, bounded [-1, 1] for normalised embeddings, so
# a threshold here is at least dimensionally meaningful. 0.75 is a commonly-cited "clearly
# related" zone for OpenAI's `text-embedding-3-small` on short text; the ACCEPTANCE EXAMPLE
# this guards (a semantically related but vocabulary-disjoint match must surface) sets the
# floor this cannot be tightened past. NOT measured against this platform's real catalog —
# there is none yet (#191's own Dependencies section: "the marketplace is empty at time of
# build") — so this is the best available prior, not a live calibration, and should be
# revisited once real descriptions exist to tune against.
_VECTOR_SOLO_SIMILARITY = 0.75

# STRICTER — used only when the keyword arm returned NOTHING AT ALL (not merely "didn't
# include this candidate"): with no keyword signal to cross-check against, a lone vector
# match needs to clear a higher bar before it is worth interrupting someone's create flow.
_VECTOR_ONLY_FALLBACK_SIMILARITY = 0.85

# Measured against the `english` config on real descriptions (review of #191, round 3, agc129):
# `ts_rank_cd` scores ~0.1-0.2 per incidental shared lexeme under the OR-joined query `_tsquery`
# now builds — three incidental words (e.g. "staff"/"terminal"/"log", routine in one catalog's
# vocabulary) already reach 0.30, while a genuine reworded near-duplicate reaches 0.70-0.80 and
# an identical description 1.70+. 0.5 sits in the empty band between the two, clear of both. THIS
# BAR IS PAIRED WITH THE OR-QUERY — the old 0.01 was calibrated against `websearch_to_tsquery`'s
# AND semantics, where clearing it meant matching every stemmed term; if `_tsquery` ever goes
# back to AND, this number is wrong again and must be re-measured against that shape instead.
_KEYWORD_SOLO_RANK = 0.5

# Agreement across both arms relaxes the solo bars above, but RANK-ONLY agreement is VACUOUS:
# any row that clears the keyword arm's `@@` predicate enters it, and with the OR-join one
# shared word is enough — so some row is always inside both arms' own top `_AGREEMENT_TOP_N`,
# and `both_in_agreement` would accept it regardless of how unrelated its actual scores say it
# is (review of #191, round 3, agc129 — measured live: four genuinely unrelated apps all
# accepted by agreement alone, at cosine similarities of 0.34-0.53, none clearing either arm's
# own solo bar). This floor is deliberately LOWER than `_VECTOR_SOLO_SIMILARITY` — agreement is
# still worth a discount, that is the whole point of the rule — but it is not a free pass:
# measured unrelated apps topped out at 0.53, a genuine near-duplicate reached ~0.84, so 0.6
# sits clear of the unrelated band without encroaching on the solo bar it discounts.
_AGREEMENT_MIN_SIMILARITY = 0.6


@dataclass(frozen=True)
class DuplicateCheckResult:
    matches: list[MarketplaceEntry]


def _tsquery(search: str) -> sa.Function[Any]:
    """OR-joined lexeme tsquery for description-AGAINST-description matching (R31) — NOT
    `websearch_to_tsquery`, the marketplace search box's own helper (`marketplace/router.py`),
    which ANDs every significant term. That is correct for a short search-box query; it is
    the wrong shape here, where the input is the citizen's own 15-120 WORD description: an
    AND-query only matches a stored description containing every one of those stemmed terms
    — near-verbatim copy-paste — so any real second author describing the same app in their
    own words left the keyword arm permanently EMPTY (review of #191, agc129 — measured: a
    genuine duplicate scored 0.84 cosine similarity, matched nothing on the keyword arm, and
    was rejected by the stricter vector-only fallback bar `keyword_arm_is_empty` forces
    `_select_confident_matches` into, leaving three of its four confidence rules dead code
    in practice).

    BUILT FROM `plainto_tsquery`'s OWN AND-JOINED OUTPUT, converted to OR by a plain text
    substitution — not a second, per-lexeme round trip. `plainto_tsquery` GUARANTEES its
    `::text` form is always exactly `'lex1' & 'lex2' & ... & 'lexN'` (single lexemes only —
    no phrase operators, no other punctuation): that guarantee is specifically what
    `websearch_to_tsquery`'s richer output does NOT make (`<->` phrase operators from quoted
    input would survive a blind `&`->`|` swap as nonsense), so this substitution is safe only
    starting from `plainto_tsquery`. The description was already sanitized INTO the tsquery
    by `plainto_tsquery` before this ever touches it as text, so there is no injection surface
    — this rewrites Postgres's own output, never the citizen's raw input.
    """
    plain = sa.func.plainto_tsquery(DESCRIPTION_TSV_REGCONFIG, search)
    or_joined = sa.func.replace(sa.cast(plain, sa.Text), " & ", " | ")
    return sa.func.to_tsquery(DESCRIPTION_TSV_REGCONFIG, or_joined)


async def find_possible_duplicates(
    db: AsyncSession, description: str, embedder: Embedder | None
) -> DuplicateCheckResult:
    """Search the live marketplace catalog for apps that look like `description` (R31),
    before a new project is created. NEVER RAISES — see the module docstring; any internal
    failure is logged and answered as "no duplicate found" rather than propagated.
    """
    try:
        return await _find_possible_duplicates(db, description, embedder)
    except Exception:  # noqa: BLE001 — R37: a failed check must never block a create
        logger.warning("duplicate_check_failed", exc_info=True)
        return DuplicateCheckResult(matches=[])


def _candidate_query(description: str, query_embedding: list[float] | None) -> sa.Select[Any]:
    """Build the candidate query: up to `_CANDIDATE_WINDOW` rows per arm, FULL OUTER joined,
    carrying each arm's own rank AND native score (never a fused RRF score — R34 forbids a
    flat fused-score cutoff), joined onto the display columns `MarketplaceEntry` needs.

    Separated from `_find_possible_duplicates` so the query itself is compile-testable
    without a live database — the same reason `marketplace/router.py::_hybrid_catalog`
    returns a query object rather than executing inline.
    """
    live = live_app_ids()
    kw_rank_expr = sa.func.ts_rank_cd(Project.description_tsv, _tsquery(description)).desc()
    kw_arm = (
        sa.select(
            AppRegistry.id.label("app_id"),
            sa.func.row_number().over(order_by=kw_rank_expr).label("rank"),
            sa.func.ts_rank_cd(Project.description_tsv, _tsquery(description)).label("score"),
        )
        .select_from(AppRegistry)
        .join(Project, Project.id == AppRegistry.project_id)
        .where(
            AppRegistry.id.in_(live),
            Project.description_tsv.op("@@")(_tsquery(description)),
        )
        .order_by(kw_rank_expr)
        .limit(_CANDIDATE_WINDOW)
        .cte("kw_arm")
    )

    if query_embedding is not None:
        vec_distance = Project.description_embedding.cosine_distance(query_embedding)
        vec_arm = (
            sa.select(
                AppRegistry.id.label("app_id"),
                sa.func.row_number().over(order_by=vec_distance).label("rank"),
                (1.0 - vec_distance).label("score"),
            )
            .select_from(AppRegistry)
            .join(Project, Project.id == AppRegistry.project_id)
            .where(
                AppRegistry.id.in_(live),
                Project.description_embedding.is_not(None),
            )
            .order_by(vec_distance)
            .limit(_CANDIDATE_WINDOW)
            .cte("vec_arm")
        )
        candidates = (
            sa.select(
                sa.func.coalesce(kw_arm.c.app_id, vec_arm.c.app_id).label("app_id"),
                kw_arm.c.rank.label("kw_rank"),
                kw_arm.c.score.label("kw_score"),
                vec_arm.c.rank.label("vec_rank"),
                vec_arm.c.score.label("vec_score"),
            )
            .select_from(kw_arm.outerjoin(vec_arm, kw_arm.c.app_id == vec_arm.c.app_id, full=True))
            .cte("candidates")
        )
    else:
        # No embedder configured, or the embed call already failed (caught in
        # `_find_possible_duplicates`, which degrades to `None` rather than raising) — either
        # way this is only ever called with a real embedding or none at all, never a partial
        # one.
        candidates = (
            sa.select(
                kw_arm.c.app_id.label("app_id"),
                kw_arm.c.rank.label("kw_rank"),
                kw_arm.c.score.label("kw_score"),
                sa.null().label("vec_rank"),
                sa.null().label("vec_score"),
            )
            .select_from(kw_arm)
            .cte("candidates")
        )

    deployment = last_success_deployment()
    return (
        sa.select(
            candidates.c.kw_rank,
            candidates.c.kw_score,
            candidates.c.vec_rank,
            candidates.c.vec_score,
            Project.name,
            Project.description,
            User.display_name,
            deployment.url,
        )
        .select_from(candidates)
        .join(deployment, deployment.app_id == candidates.c.app_id)
        .join(AppRegistry, AppRegistry.id == candidates.c.app_id)
        .join(Project, Project.id == AppRegistry.project_id)
        .join(User, User.id == deployment.user_id)
    )


def _select_confident_matches(rows: Sequence[Any]) -> list[Any]:
    """Apply R34's confidence bar to the candidate rows and cap at `MAX_MATCHES` — the pure,
    DB-free half of the duplicate check, so its branching is unit-testable directly against
    hand-built rows rather than only through a live query.

    Each `row` is expected to expose `kw_rank`, `kw_score`, `vec_rank`, `vec_score` (any may
    be `None` — a row absent from an arm never populated it) as its first four positional
    values, matching `_candidate_query`'s column order. Typed `Any` rather than `sa.Row[Any]`
    DELIBERATELY: the real caller passes an actual `Row`, but this function only ever reads
    named attributes off it, so `tests/services/projects/test_duplicates.py` pins the
    branching against a plain `namedtuple` stand-in instead — narrowing the parameter to
    `sa.Row` would make that a `ty`/pyright nominal-typing violation for a substitution the
    function itself never required.
    """
    keyword_arm_is_empty = not any(row.kw_rank is not None for row in rows)
    vector_solo_bar = (
        _VECTOR_ONLY_FALLBACK_SIMILARITY if keyword_arm_is_empty else _VECTOR_SOLO_SIMILARITY
    )

    accepted: list[tuple[int, Any]] = []
    for row in rows:
        both_in_agreement = (
            row.kw_rank is not None
            and row.kw_rank <= _AGREEMENT_TOP_N
            and row.vec_rank is not None
            and row.vec_rank <= _AGREEMENT_TOP_N
            and row.vec_score is not None
            and row.vec_score >= _AGREEMENT_MIN_SIMILARITY
        )
        keyword_alone_clears = (
            row.kw_rank is not None
            and row.kw_score is not None
            and row.kw_score >= _KEYWORD_SOLO_RANK
        )
        vector_alone_clears = (
            row.vec_rank is not None
            and row.vec_score is not None
            and row.vec_score >= vector_solo_bar
        )
        if not (both_in_agreement or keyword_alone_clears or vector_alone_clears):
            continue
        # Best available rank in either arm — ties among confident matches broken by
        # whichever ranking placed the candidate closest to the top, not by arithmetic on
        # two differently-scaled native scores.
        best_rank = min(
            row.kw_rank or _CANDIDATE_WINDOW + 1, row.vec_rank or _CANDIDATE_WINDOW + 1
        )
        accepted.append((best_rank, row))

    accepted.sort(key=lambda pair: pair[0])
    return [row for _, row in accepted[:MAX_MATCHES]]


async def _find_possible_duplicates(
    db: AsyncSession, description: str, embedder: Embedder | None
) -> DuplicateCheckResult:
    # THE CHEAP QUESTION FIRST (review of #191, agc129 — blocker #7). An empty marketplace
    # is not an edge case: it is the NORMAL state of production on day one and for as long as
    # nothing has been published, and unconditionally embedding before asking "is there
    # anything at all to search" spent a full Foundry round trip — plus its own outage mode —
    # on a query guaranteed to return nothing (measured: ~1.1s per create). An indexed EXISTS
    # against the SAME `live_app_ids()` predicate every arm already scopes to, so this can
    # never drift from the corpus definition it is answering for.
    if not await db.scalar(sa.select(sa.exists().where(AppRegistry.id.in_(live_app_ids())))):
        return DuplicateCheckResult(matches=[])

    query_embedding: list[float] | None = None
    if embedder is not None:
        # R21: "document" on BOTH sides of the duplicate check — this is description-
        # against-description, never a search-box "query" embedding.
        #
        # ONLY THE EMBED CALL IS GUARDED HERE, mirroring `marketplace/router.py
        # ::_embed_query_or_none` (review of #191, agc129 — a real production defect this
        # closes): the OUTER `except` in `find_possible_duplicates` above used to be the
        # only guard, which caught this call TOGETHER WITH the query below — so a Foundry
        # failure abandoned the keyword arm as well as the vector one, answering "no
        # duplicates found" instead of R26's documented "falls back to keyword-only".
        try:
            result = await embedder.embed_documents(description)
            query_embedding = list(result.embeddings[0])
        except Exception:  # noqa: BLE001 — degrade to keyword-only, never fail the check
            logger.warning("duplicate_check_embedding_unavailable")

    rows = (await db.execute(_candidate_query(description, query_embedding))).all()
    top = _select_confident_matches(rows)

    matches = [
        MarketplaceEntry(
            name=row.name,
            description=row.description,
            builder_display_name=row.display_name,
            url=row.url,
        )
        for row in top
    ]
    return DuplicateCheckResult(matches=matches)


def log_matches_shown(*, match_count: int) -> None:
    """How many matches the check surfaced, every time it runs (including zero — the
    day-one, empty-catalog case is itself worth counting, since "the check ran and found
    nothing" is a different fact from "the check never ran"). No project id: this runs
    BEFORE a project exists, to check a description against the live marketplace before
    creating one — there is nothing to hang the event off yet."""
    logger.info(DUPLICATE_MATCHES_SHOWN_EVENT, match_count=match_count)


def log_resolution(*, resolution: str) -> None:
    """R39's second event: what the citizen did once shown matches — opened an existing
    app, or created anyway. `resolution` is a plain string rather than an enum import here
    to keep this module independent of the API schema layer; the router validates the
    closed set before calling this."""
    logger.info(DUPLICATE_CHECK_RESOLVED_EVENT, resolution=resolution)
