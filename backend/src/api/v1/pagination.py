"""Shared keyset (cursor) pagination for the platform's list endpoints.

Keyset, NOT offset: a page must come back with no duplicates and no skips while rows are
inserted underneath it, which offset structurally cannot promise. Every owned model has a
time-sortable UUIDv7 primary key, so the cursor IS the last row's id and a page is
`WHERE id < :cursor ORDER BY id DESC LIMIT :n+1` — the extra row is how `hasMore` is known.
The envelope is `{items, nextCursor, hasMore}`, with no `total`/`totalPages`.

THREE SURFACES PAGE BY OFFSET INSTEAD — the marketplace catalog, the projects list and the
shared-with-me list, all because their designs specify numbered pages and a `Showing 1-8 of 12`
count that keyset can't cheaply compute. Each carries its OWN argument for paying the cost, and
the third one's is the weakest of the three: it is the only one with many writers. Their helpers
live in `offset_pagination.py`. This module stays the single source of truth for the platform's
page-size ceiling, which offset callers import."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from typing import Annotated

from fastapi import Query

from src.core.errors import AppApiError

DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100

# A generous cap on the free-text search token (200 chars) so `q` can't become an abusive
# scan. Longer than any real search term, short enough to bound the LIKE pattern.
MAX_SEARCH_Q = 200

# `Query()` deliberately carries no `ge`/`le` bound: a FastAPI bound emits the native
# `{detail:[...]}` 422, so one endpoint would carry two 422 body shapes and
# `error_responses(...)` cannot document both. Range and format are checked by `clean_limit`
# and `parse_cursor` instead, in the one `{error:{message}}` envelope.
LimitQuery = Annotated[int, Query()]
CursorQuery = Annotated[str | None, Query()]
SearchQuery = Annotated[str | None, Query()]


def parse_cursor(cursor: str | None) -> uuid.UUID | None:
    """A cursor is the last-seen row id (a UUID). Absent → first page; malformed → 422 —
    rejected, not silently ignored, so a bad cursor never quietly returns page one."""
    if cursor is None:
        return None
    try:
        return uuid.UUID(cursor)
    except ValueError:
        raise AppApiError(422, "Invalid pagination cursor.") from None


def clean_limit(value: int) -> int:
    """Reject an out-of-range `?limit=` rather than clamp it: a silent clamp drops every
    row past the clamp out of the response."""
    if not 1 <= value <= MAX_PAGE_SIZE:
        raise AppApiError(422, f"limit must be between 1 and {MAX_PAGE_SIZE}.")
    return value


def clean_search(q: str | None) -> str | None:
    """Normalize a `?q=` token: strip, empty → None (no filter), over-long or
    unrepresentable → 422."""
    if q is None:
        return None
    q = q.strip()
    if not q:
        return None
    if len(q) > MAX_SEARCH_Q:
        raise AppApiError(422, f"q must be at most {MAX_SEARCH_Q} characters.")
    # A NUL byte is not representable in a Postgres text value, so it reaches asyncpg and
    # raises `CharacterNotInRepertoireError` — an unhandled 500 on an authenticated endpoint.
    # Rejected here rather than at a call site because this function is the platform's `?q=`
    # boundary: the same input would 500 every list endpoint that accepts one.
    if "\x00" in q:
        raise AppApiError(422, "q contains an unsupported character.")
    return q


def split_keyset[T](
    rows: Sequence[T], limit: int, *, key: Callable[[T], uuid.UUID]
) -> tuple[list[T], str | None, bool]:
    """Split rows fetched with `LIMIT limit + 1` (ordered by id DESC) into the page, the next
    cursor, and `hasMore`. The cursor is the id of the LAST returned row, so the next page
    continues strictly below it."""
    has_more = len(rows) > limit
    page = list(rows[:limit])
    next_cursor = str(key(page[-1])) if has_more and page else None
    return page, next_cursor, has_more
