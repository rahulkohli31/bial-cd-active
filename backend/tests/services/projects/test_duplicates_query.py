"""`_candidate_query`'s SQL SHAPE (#191 slice 4) — compiled-SQL assertions, mirroring
`tests/api/v1/marketplace/test_hybrid_search.py`: the property under test (each arm's
membership, that both native scores survive to the outer query) is structural and a live
run cannot distinguish "correct shape" from "happens to return the right rows for this
seed data".
"""

from __future__ import annotations

import re

from sqlalchemy.dialects import postgresql

from src.services.projects.duplicates import _CANDIDATE_WINDOW, _candidate_query


def _compiled(description: str, query_embedding: list[float] | None) -> str:
    query = _candidate_query(description, query_embedding)
    return str(
        query.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": False})
    )


def test_both_native_scores_survive_to_the_outer_query() -> None:
    # Unlike marketplace search (which only needs a fused RRF score), the duplicate check's
    # confidence bar needs each arm's OWN score — both must reach the final SELECT.
    sql = _compiled("leave request tracker", [0.1] * 1536)
    assert "kw_score" in sql
    assert "vec_score" in sql
    assert "kw_rank" in sql
    assert "vec_rank" in sql


def test_the_at_at_predicate_lives_only_inside_the_keyword_arm() -> None:
    sql = _compiled("leave request tracker", [0.1] * 1536)
    assert sql.count("@@") == 1


def test_the_vector_arm_is_unfiltered_by_a_similarity_threshold() -> None:
    # No threshold belongs in SQL — R34's confidence bar is applied entirely in Python by
    # `_select_confident_matches`, against the native scores this query hands back.
    sql = _compiled("leave request tracker", [0.1] * 1536)
    assert "description_embedding IS NOT NULL" in sql
    assert not re.search(r"<=>[^,)]*[<>]\s*%\(", sql)


def test_keyword_only_degrade_has_no_vector_arm() -> None:
    sql = _compiled("leave request tracker", None)
    assert "vec_arm" not in sql
    assert "<=>" not in sql
    assert "kw_arm" in sql
    # The keyword-only shape still carries NULL placeholders for the vector columns, so
    # `_select_confident_matches` sees the same four-column shape either way.
    assert "NULL AS vec_rank" in sql
    assert "NULL AS vec_score" in sql


def test_each_arm_is_bounded_by_the_candidate_window() -> None:
    sql = _compiled("leave request tracker", [0.1] * 1536)
    assert sql.count(" LIMIT ") == 2
    assert _CANDIDATE_WINDOW < 200  # smaller than the marketplace search's own pool cap


def test_membership_reuses_live_app_ids_not_a_second_predicate() -> None:
    sql = _compiled("leave request tracker", [0.1] * 1536)
    assert sql.count("rejection_standing IS false") >= 1


def test_the_keyword_arm_is_or_joined_not_websearch_to_tsquery() -> None:
    # R31 vs the marketplace's own search box (review of #191, agc129 — blocker #6):
    # `websearch_to_tsquery` ANDs every term, which only matches a near-verbatim copy of the
    # citizen's word-bounded description — the keyword arm was PERMANENTLY EMPTY for any real
    # second author describing the same app differently. `plainto_tsquery`'s AND-joined
    # output, text-substituted from `&` to `|`, is what actually lets a partial-vocabulary
    # match through; `websearch_to_tsquery` must not appear in this query at all.
    sql = _compiled("leave request tracker", [0.1] * 1536)
    assert "websearch_to_tsquery" not in sql
    assert "plainto_tsquery" in sql
    assert "to_tsquery" in sql
    assert "replace(" in sql
