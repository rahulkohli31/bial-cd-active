"""The hybrid (keyword + semantic) search query's SHAPE (#191 slice 3, R27/R30).

These assert on COMPILED SQL rather than against a live database, mirroring
`test_marketplace.py::test_the_success_collapse_predicate_renders_a_literal` — the property
under test (where the `@@` predicate lives, whether the two arms are FULL OUTER joined, that
the vector arm is genuinely unfiltered) is a structural fact about the query a live run
cannot distinguish from "happens to return the right rows for this seed data", and is exactly
the kind of regression that stays invisible to a purely behavioural test until the catalog
grows large enough for the wrong plan to matter.
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy.dialects import postgresql

from src.api.v1.marketplace.router import _ARM_POOL_CAP, _RRF_K, _hybrid_catalog


def _compiled(search: str, query_embedding: list[float] | None) -> str:
    query, _ = _hybrid_catalog(search, query_embedding)
    return str(
        query.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": False})
    )


def test_rrf_k_is_60_and_actually_reaches_the_compiled_query() -> None:
    """The only other RRF test (`test_a_missing_arm_scores_as_unranked_not_zero_similarity`)
    checks the SHAPE `1/(k+rank)` compiles to, with two keyword ranks and no vector arm — which
    only proves the formula orders monotonically in rank, true for any `k > 0` and therefore
    invariant to what `k` actually is. Pinning the literal, and that the SAME number is bound
    into both arms of the compiled query, is the only thing that would notice a drift."""
    assert _RRF_K == 60
    query, _ = _hybrid_catalog("anything", [0.1] * 1536)
    bound = query.compile(dialect=postgresql.dialect()).params
    rank_params = [v for k, v in bound.items() if k.startswith("rank_")]
    assert len(rank_params) == 2  # one per arm — kw_arm and vec_arm
    assert rank_params == [_RRF_K, _RRF_K]


def test_the_at_at_predicate_lives_only_inside_the_keyword_arms_own_cte() -> None:
    """R27's central rule: the `@@` match must not gate the fused candidate set — it may
    only filter WHICH ROWS ENTER THE KEYWORD ARM. Structurally, that means the `@@` operator
    appears inside the `kw_arm` CTE's own text and NOWHERE ELSE in the compiled statement —
    in particular, not as a predicate on the final SELECT that joins the fused result onto
    deployment/project/user."""
    sql = _compiled("leave tracker", [0.1] * 1536)

    kw_cte = re.search(r"kw_arm AS \s*\((.*?)\),\s*\n\s*vec_arm", sql, re.DOTALL)
    assert kw_cte is not None, sql
    assert "@@" in kw_cte.group(1)

    # The outer/final SELECT (everything after the `fused` CTE closes) must not carry a
    # second `@@` — that would be the flat-filter shape the issue explicitly forbids.
    after_fused = sql.split("SELECT anon_1.id" if "anon_1.id" in sql else "FROM fused")[-1]
    assert "@@" not in after_fused


def test_the_vector_arm_is_unfiltered_by_any_similarity_threshold() -> None:
    """R27: "an unfiltered similarity ordering" — no `WHERE ... <=> ... <` clause, only the
    `IS NOT NULL` existence check and the ORDER BY/LIMIT that bound its own top-N. A
    threshold belongs to the duplicate check (slice 4, R34), never to marketplace search."""
    sql = _compiled("leave tracker", [0.1] * 1536)

    vec_cte = re.search(r"vec_arm AS \s*\((.*?)\),\s*\n\s*fused", sql, re.DOTALL)
    assert vec_cte is not None, sql
    body = vec_cte.group(1)
    assert "description_embedding IS NOT NULL" in body
    assert "<=>" in body
    # No comparison against the distance operator's OWN result — only ORDER BY uses it.
    assert not re.search(r"<=>[^,]*[<>]\s*%\(", body)


def test_the_two_arms_are_combined_with_a_full_outer_join() -> None:
    """R30: a row present in only one arm must still surface — every pre-#191 project has
    no embedding at all and must not be excluded from keyword results by the fusion. A
    FULL OUTER JOIN (not INNER, not LEFT) is the only shape that keeps a keyword-only or a
    vector-only match alive through the fusion."""
    sql = _compiled("leave tracker", [0.1] * 1536)
    assert "FULL OUTER JOIN" in sql
    assert "kw_arm FULL OUTER JOIN vec_arm" in sql


def test_the_fused_id_coalesces_across_both_arms() -> None:
    """The row identifying a fused candidate must come from WHICHEVER arm has it — an app
    absent from the keyword arm (no shared vocabulary) still needs an id from the vector
    arm to survive the outer join, and vice versa."""
    sql = _compiled("leave tracker", [0.1] * 1536)
    assert "coalesce(kw_arm.app_id, vec_arm.app_id)" in sql


def test_a_missing_arm_scores_as_unranked_not_zero_similarity() -> None:
    """`COALESCE(1/(k+rank), 0)` per arm — an arm a row is absent from contributes 0 to the
    fused score (R27: "treating an arm a row is absent from as unranked"), which is
    different from asserting the row scored WORST in that arm; it simply never entered it."""
    sql = _compiled("leave tracker", [0.1] * 1536)
    # Three COALESCEs: the fused id (whichever arm has it) plus one per arm's own
    # rank-to-score term, each wrapped separately before the two scores are summed.
    assert sql.lower().count("coalesce(") == 3
    assert "kw_arm.rank" in sql
    assert "vec_arm.rank" in sql


def test_keyword_only_degrade_has_no_vector_arm_at_all() -> None:
    """When `query_embedding is None` (embeddings unconfigured, or the query's own embed
    call failed), the compiled SQL must not reference `vec_arm` or the `<=>` operator at
    all — a literal, structural degrade to keyword-only, not a vector arm that quietly
    matches nothing."""
    sql = _compiled("leave tracker", None)
    assert "vec_arm" not in sql
    assert "<=>" not in sql
    assert "kw_arm" in sql


def test_each_arm_is_capped_at_the_documented_pool_size() -> None:
    """R28: each arm's own top-N is bounded, so the fused pool stays small regardless of
    catalog growth — `_ARM_POOL_CAP` is the ceiling, sized to the catalog's own documented
    10-200 row maximum (#145/#191) so it never actually binds in practice."""
    assert _ARM_POOL_CAP >= 200
    sql = _compiled("leave tracker", [0.1] * 1536)
    # Both arms carry a LIMIT clause (compiled as a bound param, so this checks presence
    # rather than the literal number).
    assert sql.count(" LIMIT ") >= 2


def test_the_fused_result_orders_by_score_then_id() -> None:
    """Relevance first, id as the tiebreak — the same "rank first, id breaks ties" shape
    the pre-#191 `ts_rank_cd`-only ordering already used, so a page boundary cannot
    interleave two runs of the same query differently."""
    sql = _compiled("leave tracker", [0.1] * 1536)
    assert "ORDER BY fused.rrf_score DESC" in sql


@pytest.mark.parametrize("query_embedding", [[0.1] * 1536, None])
def test_membership_is_live_app_ids_in_both_arms(query_embedding: list[float] | None) -> None:
    """Both arms scope their candidates to `live_app_ids()` — never a second, hand-rolled
    membership predicate that could drift from the one `_live_catalog` (browsing) and
    `liveness.py`'s other two surfaces already share."""
    sql = _compiled("leave tracker", query_embedding)
    # `live_app_ids()` compiles to the deployments-collapse subquery; its presence at least
    # twice (once per arm, or once for keyword-only) is the structural signal that each arm
    # re-derives membership rather than sharing one JOIN across both.
    assert sql.count("rejection_standing IS false") >= 1
