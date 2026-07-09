"""Shared keyset (cursor) pagination helpers for the new project + admin lists (KD-1, R5–R7).

Keyset, NOT offset: the origin requires a stable cursor that returns the next page with
no duplicates or skips under concurrent inserts (R5/AE3), which offset structurally cannot
do. Every owned model has a time-sortable UUIDv7 PK (ADR-0006), so the cursor IS the last
row's id and the page is `WHERE id < :cursor ORDER BY id DESC LIMIT :n+1` — the extra row
tells us whether a next page exists. The envelope is `{items, nextCursor, hasMore}`; there
is deliberately no `total`/`totalPages` (keyset does not cheaply provide them and they are
not required, KD-1).

The data-plane `records_router` keeps its own offset scheme (a different requirement); this
module governs only the project + admin roster lists. The page-size cap mirrors that
router's `MAX_PAGE_SIZE` so the platform speaks one page-size ceiling.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from typing import Annotated

from fastapi import Query

from src.core.errors import AppApiError

# One platform-wide page-size ceiling (mirrors `apps/records_router.MAX_PAGE_SIZE`). An
# over-large or non-positive `limit` is REJECTED (422), never silently clamped in a way
# that skips rows (R7).
DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100
# A generous cap on the free-text search token so `q` can't become an abusive scan (mirrors
# `records_router.MAX_SEARCH_Q`).
MAX_SEARCH_Q = 200

# The shared `?limit=` param: FastAPI 422s an out-of-range value on its own (R7). The
# shared `?cursor=` param is validated by `parse_cursor` (a malformed cursor is a 422).
LimitQuery = Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)]
CursorQuery = Annotated[str | None, Query()]
SearchQuery = Annotated[str | None, Query()]


def parse_cursor(cursor: str | None) -> uuid.UUID | None:
    """A cursor is the last-seen row id (a UUID). Absent → first page; malformed → 422
    (rejected, not silently ignored, so a bad cursor never quietly returns page one, R7)."""
    if cursor is None:
        return None
    try:
        return uuid.UUID(cursor)
    except ValueError:
        raise AppApiError(422, "Invalid pagination cursor.") from None


def clean_search(q: str | None) -> str | None:
    """Normalize a `?q=` token: strip, empty → None (no filter), over-long → 422."""
    if q is None:
        return None
    q = q.strip()
    if not q:
        return None
    if len(q) > MAX_SEARCH_Q:
        raise AppApiError(422, f"q must be at most {MAX_SEARCH_Q} characters.")
    return q


def split_keyset[T](
    rows: Sequence[T], limit: int, *, key: Callable[[T], uuid.UUID]
) -> tuple[list[T], str | None, bool]:
    """Split rows fetched with `LIMIT limit + 1` (ordered by id DESC) into the page, the
    next cursor, and `hasMore`. The (limit+1)-th row's presence IS `hasMore`; the cursor is
    the id of the LAST returned row (so the next page continues strictly below it)."""
    has_more = len(rows) > limit
    page = list(rows[:limit])
    next_cursor = str(key(page[-1])) if has_more and page else None
    return page, next_cursor, has_more
