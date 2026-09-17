"""The project description's word bound, on BOTH write paths (#191).

Mirrors `test_project_name_words.py` exactly — same shape, same reasoning, a different
field with a different (and unusual) rule: description has a MINIMUM as well as a maximum.
The issue's own acceptance examples call for both directions to be pinned:

    Given a create form with a five-word description, when it is submitted, then it is
    refused with a message naming the minimum, in both the browser and the API. Given a
    200-word description, then it is refused with a message naming the maximum.

Two things these tests exist to hold:

1.  **Create and edit are the same rule.** `_clean_description` is shared by `ProjectCreate`
    and `ProjectPatch`, so one change covers both.
2.  **The word rule is the shared one.** `count_words` is `str.split()`, which splits on
    RUNS of whitespace — pinned here the same way `test_project_name_words.py` pins it for
    the title, so the two fields cannot quietly drift onto different splitting rules.
"""

from __future__ import annotations

import pytest

from src.core.words import count_words
from src.db.models.project import (
    MAX_PROJECT_DESCRIPTION,
    MAX_PROJECT_DESCRIPTION_WORDS,
    MIN_PROJECT_DESCRIPTION_WORDS,
)
from tests.api.v1.projects.conftest import _VALID_DESCRIPTION
from tests.api.v1.projects.test_projects_crud import _auth
from tests.factories import ProjectFactory

_PROJECTS = "/v1/projects"


def _words(n: int) -> str:
    """A description of exactly `n` words, each distinct so nothing collapses."""
    return " ".join(f"w{i}" for i in range(n))


def test_the_bound_is_15_to_120_words() -> None:
    # Pinned to the LITERALS: every test below parametrizes its own setup AND assertion off
    # these two constants, so all of them stay green no matter what the bound is widened to.
    # This is the one place a change to either constant is visible at all — the issue's own
    # acceptance examples name 15 and 120, not "whatever the constants say".
    assert MIN_PROJECT_DESCRIPTION_WORDS == 15
    assert MAX_PROJECT_DESCRIPTION_WORDS == 120


# --- create ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "n",
    [
        MIN_PROJECT_DESCRIPTION_WORDS,
        MAX_PROJECT_DESCRIPTION_WORDS - 1,
        MAX_PROJECT_DESCRIPTION_WORDS,
    ],
)
async def test_create_accepts_a_description_within_the_word_bound(
    client, db_session, n: int
) -> None:
    """Both boundaries are legal — 15 and 120 words are accepted, not rejected."""
    headers, _ = await _auth(db_session)

    resp = await client.post(
        _PROJECTS, headers=headers, json={"name": "X", "description": _words(n)}
    )

    assert resp.status_code == 201, resp.text
    assert count_words(resp.json()["description"]) == n


async def test_create_refuses_one_word_under_the_minimum(client, db_session) -> None:
    headers, _ = await _auth(db_session)

    resp = await client.post(
        _PROJECTS,
        headers=headers,
        json={"name": "X", "description": _words(MIN_PROJECT_DESCRIPTION_WORDS - 1)},
    )

    assert resp.status_code == 422, resp.text
    # Written for a person and names the bound, per the issue's own acceptance example.
    assert str(MIN_PROJECT_DESCRIPTION_WORDS) in resp.text
    assert "Value error" not in resp.json().get("detail", [{}])[0].get("msg", "")


async def test_create_refuses_one_word_past_the_maximum(client, db_session) -> None:
    headers, _ = await _auth(db_session)

    resp = await client.post(
        _PROJECTS,
        headers=headers,
        json={"name": "X", "description": _words(MAX_PROJECT_DESCRIPTION_WORDS + 1)},
    )

    assert resp.status_code == 422, resp.text
    assert str(MAX_PROJECT_DESCRIPTION_WORDS) in resp.text


async def test_create_refuses_a_missing_description(client, db_session) -> None:
    headers, _ = await _auth(db_session)

    resp = await client.post(_PROJECTS, headers=headers, json={"name": "X"})

    assert resp.status_code == 422, resp.text


async def test_create_refuses_a_blank_description(client, db_session) -> None:
    headers, _ = await _auth(db_session)

    resp = await client.post(_PROJECTS, headers=headers, json={"name": "X", "description": "   "})

    assert resp.status_code == 422, resp.text
    assert "What should this app do?" in resp.text


async def test_the_character_backstop_still_refuses_a_value_the_word_rule_alone_would_accept(
    client, db_session
) -> None:
    """100 words, each 20 characters — inside the word bound (15-120) but over the 2000
    character cap. The word rule ALONE would accept this, so the character bound is not
    redundant: it is what stops long words (URLs, identifiers, a non-English script where
    `count_words` under-counts relative to length) from reaching an unbounded column, and a
    user writing ordinary prose should never meet it."""
    headers, _ = await _auth(db_session)
    long_words = " ".join("x" * 20 for _ in range(100))
    assert len(long_words) > MAX_PROJECT_DESCRIPTION
    assert count_words(long_words) <= MAX_PROJECT_DESCRIPTION_WORDS

    resp = await client.post(
        _PROJECTS, headers=headers, json={"name": "X", "description": long_words}
    )

    assert resp.status_code == 422, resp.text
    assert "characters" in resp.text


# --- edit -------------------------------------------------------------------------


async def test_edit_refuses_one_word_under_the_minimum(client, db_session) -> None:
    headers, user = await _auth(db_session)
    project = await ProjectFactory.create(db_session, user.id, description=_VALID_DESCRIPTION)
    await db_session.commit()

    resp = await client.patch(
        f"{_PROJECTS}/{project.id}",
        headers=headers,
        json={"description": _words(MIN_PROJECT_DESCRIPTION_WORDS - 1)},
    )

    assert resp.status_code == 422, resp.text
    assert str(MIN_PROJECT_DESCRIPTION_WORDS) in resp.text

    # ...and the stored description is untouched by the refusal.
    after = await client.get(f"{_PROJECTS}/{project.id}", headers=headers)
    assert after.json()["description"] == _VALID_DESCRIPTION


async def test_edit_accepts_the_boundary(client, db_session) -> None:
    headers, user = await _auth(db_session)
    project = await ProjectFactory.create(db_session, user.id, description=_VALID_DESCRIPTION)
    await db_session.commit()

    resp = await client.patch(
        f"{_PROJECTS}/{project.id}",
        headers=headers,
        json={"description": _words(MIN_PROJECT_DESCRIPTION_WORDS)},
    )

    assert resp.status_code == 200, resp.text
    assert count_words(resp.json()["description"]) == MIN_PROJECT_DESCRIPTION_WORDS


# --- the splitting rule, where client and server could drift -----------------------


@pytest.mark.parametrize(
    "description",
    [
        "a  b  c  d  e  f  g  h  i  j  k  l  m  n  o",  # double spaces
        "a\tb\tc\td\te\tf\tg\th\ti\tj\tk\tl\tm\tn\to",  # tabs
        "a\nb\nc\nd\ne\nf\ng\nh\ni\nj\nk\nl\nm\nn\no",  # newlines
        "  a b c d e f g h i j k l m n o  ",  # leading/trailing
    ],
)
async def test_whitespace_runs_count_as_one_separator(
    client, db_session, description: str
) -> None:
    """Fifteen words however they are spaced — a paste must not silently drop under the
    minimum. `str.split()` (no argument) collapses runs and drops empty tokens;
    `portal/src/utils/words.ts` pins the mirror image of this list."""
    headers, _ = await _auth(db_session)

    resp = await client.post(
        _PROJECTS, headers=headers, json={"name": "X", "description": description}
    )

    assert resp.status_code == 201, resp.text
