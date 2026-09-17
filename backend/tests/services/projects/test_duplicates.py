"""The duplicate check's confidence bar (#191 slice 4, R34) — the pure, DB-free half of
`find_possible_duplicates`, tested directly against hand-built candidate rows so its
branching does not depend on a live database or real embeddings.
"""

from __future__ import annotations

from collections import namedtuple

from src.services.projects.duplicates import (
    _AGREEMENT_MIN_SIMILARITY,
    _AGREEMENT_TOP_N,
    _KEYWORD_SOLO_RANK,
    _VECTOR_ONLY_FALLBACK_SIMILARITY,
    _VECTOR_SOLO_SIMILARITY,
    MAX_MATCHES,
    _select_confident_matches,
)

# Mirrors `_candidate_query`'s column order/names — a plain namedtuple stands in for a real
# `sa.Row`, which the function under test accesses by attribute, never by SQLAlchemy-specific
# API, so this is a faithful stand-in.
_Row = namedtuple(
    "_Row",
    ["kw_rank", "kw_score", "vec_rank", "vec_score", "name", "description", "display_name", "url"],
)


def _row(*, kw_rank=None, kw_score=None, vec_rank=None, vec_score=None, name="App") -> _Row:
    return _Row(
        kw_rank, kw_score, vec_rank, vec_score, name, "a description", "Builder", "https://x/a"
    )


def test_both_arms_in_top_n_with_a_real_vector_score_is_shown() -> None:
    # Agreement discounts BELOW either solo bar, but is not a free pass (review of #191,
    # round 3, agc129) — `kw_score` stays low (agreement carries no floor of its own on the
    # keyword side), and `vec_score` clears `_AGREEMENT_MIN_SIMILARITY` while staying below
    # `_VECTOR_SOLO_SIMILARITY`: this row is accepted BECAUSE of agreement, not despite it,
    # which is what distinguishes this case from the vacuous one below.
    row = _row(
        kw_rank=_AGREEMENT_TOP_N,
        kw_score=0.0001,
        vec_rank=_AGREEMENT_TOP_N,
        vec_score=(_AGREEMENT_MIN_SIMILARITY + _VECTOR_SOLO_SIMILARITY) / 2,
    )
    assert _select_confident_matches([row]) == [row]


def test_agreement_with_a_native_score_below_the_floor_is_excluded() -> None:
    # THE BUG THIS PINS (review of #191, round 3, agc129): agreement used to be RANK-ONLY, and
    # with the OR-joined keyword arm some row is always inside both arms' own top N — so
    # `both_in_agreement` accepted candidates whose own numbers said they were unrelated.
    # Measured live: four genuinely unrelated apps were all accepted by agreement alone, at
    # cosine similarities of 0.34-0.53, none clearing either arm's own solo bar. This row
    # reproduces that shape — comfortably inside the agreement window, comfortably below
    # `_AGREEMENT_MIN_SIMILARITY` — and must be excluded.
    row = _row(kw_rank=1, kw_score=0.2, vec_rank=1, vec_score=_AGREEMENT_MIN_SIMILARITY - 0.01)
    assert _select_confident_matches([row]) == []


def test_agreement_requires_both_arms_inside_the_agreement_window() -> None:
    # In keyword's top-N but vector's rank is just past the agreement window, and neither
    # arm's own native score clears its solo bar — must NOT be shown.
    row = _row(
        kw_rank=1,
        kw_score=_KEYWORD_SOLO_RANK - 0.001,
        vec_rank=_AGREEMENT_TOP_N + 1,
        vec_score=_VECTOR_SOLO_SIMILARITY - 0.01,
    )
    assert _select_confident_matches([row]) == []


def test_keyword_arm_alone_clears_its_own_threshold() -> None:
    row = _row(kw_rank=1, kw_score=_KEYWORD_SOLO_RANK, vec_rank=None, vec_score=None)
    assert _select_confident_matches([row]) == [row]


def test_keyword_arm_alone_below_threshold_is_excluded() -> None:
    row = _row(kw_rank=1, kw_score=_KEYWORD_SOLO_RANK - 0.0001, vec_rank=None, vec_score=None)
    assert _select_confident_matches([row]) == []


def test_vector_arm_alone_clears_the_solo_bar_when_keyword_arm_is_not_empty() -> None:
    # A second row gives the keyword arm SOMETHING (kw_rank is not None somewhere in the
    # batch), so the row under test is judged against the LOOSER solo bar, not the fallback.
    other = _row(kw_rank=1, kw_score=0.5, name="Other")
    candidate = _row(kw_rank=None, kw_score=None, vec_rank=1, vec_score=_VECTOR_SOLO_SIMILARITY)
    result = _select_confident_matches([other, candidate])
    assert candidate in result


def test_vector_arm_alone_between_the_two_bars_is_excluded_when_keyword_arm_is_empty() -> None:
    # NO row in the batch has a kw_rank at all — the keyword arm returned nothing (R34's
    # "when the keyword arm returns nothing at all" case) — so the STRICTER fallback bar
    # applies, and a score that would have passed the looser solo bar is refused.
    between = (_VECTOR_SOLO_SIMILARITY + _VECTOR_ONLY_FALLBACK_SIMILARITY) / 2
    candidate = _row(kw_rank=None, kw_score=None, vec_rank=1, vec_score=between)
    assert _select_confident_matches([candidate]) == []


def test_vector_arm_alone_clears_the_fallback_bar_when_keyword_arm_is_empty() -> None:
    candidate = _row(
        kw_rank=None, kw_score=None, vec_rank=1, vec_score=_VECTOR_ONLY_FALLBACK_SIMILARITY
    )
    assert _select_confident_matches([candidate]) == [candidate]


def test_neither_agreement_nor_either_solo_bar_is_excluded() -> None:
    row = _row(
        kw_rank=_AGREEMENT_TOP_N + 1,
        kw_score=_KEYWORD_SOLO_RANK - 0.0001,
        vec_rank=_AGREEMENT_TOP_N + 1,
        vec_score=_VECTOR_SOLO_SIMILARITY - 0.01,
    )
    assert _select_confident_matches([row]) == []


def test_max_matches_is_three() -> None:
    # Pinned to the LITERAL, not re-derived from the constant: `test_caps_at_max_matches` below
    # parametrizes its own setup AND assertion off `MAX_MATCHES`, so it stays green no matter
    # what the constant is widened to. This is the one place a change to it is visible at all —
    # #191's own acceptance criterion is "at most three", not "at most whatever this says".
    assert MAX_MATCHES == 3


def test_caps_at_max_matches() -> None:
    rows = [
        _row(kw_rank=n, kw_score=1.0, vec_rank=n, vec_score=1.0, name=f"App {n}")
        for n in range(1, MAX_MATCHES + 3)
    ]
    result = _select_confident_matches(rows)
    assert len(result) == MAX_MATCHES


def test_ranked_by_best_available_rank_across_either_arm() -> None:
    weaker = _row(kw_rank=3, kw_score=1.0, vec_rank=3, vec_score=1.0, name="Weaker")
    stronger = _row(kw_rank=1, kw_score=1.0, vec_rank=1, vec_score=1.0, name="Stronger")
    result = _select_confident_matches([weaker, stronger])
    assert [row.name for row in result] == ["Stronger", "Weaker"]


def test_no_candidates_at_all_returns_no_matches() -> None:
    assert _select_confident_matches([]) == []
