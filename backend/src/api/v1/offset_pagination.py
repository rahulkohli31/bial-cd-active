"""Offset paging — the deliberate exception to `pagination.py`, and where its argument goes.

`pagination.py` is the platform's KEYSET contract and states the position plainly: keyset,
not offset, and no `total`/`totalPages` (KD-1), because offset cannot guarantee a page with
no duplicates or skips while rows are inserted underneath it. That module governs every
admin roster and stays untouched.

Two surfaces need page NUMBERS, which keyset structurally cannot express — "Page 3 of 12"
requires a total, and a total is exactly what the keyset envelope declines to compute:

  * the marketplace catalog (#145)
  * the projects list (#158 §2, where numbered pages and a rows-per-page selector are the
    specified design)

EACH DEVIATION NEEDS ITS OWN ARGUMENT, and they are not the same argument. The marketplace's
is that a read-only catalog is not the list KD-1 protects. The projects list is a list you
ARE writing to — create and delete both act on it — so that reasoning does not transfer, and
inheriting it by analogy would be the quiet kind of wrong. Its argument is written at
`list_projects`, where a reader meets it.

WHAT THIS MODULE IS NOT: a general blessing of offset. It holds the two helpers the shape
needs — a bounded `page` and its 422 — so that a second surface does not re-derive the
overflow bound by hand. Adding a third caller means writing a third argument, not importing
this and considering the matter settled.

`marketplace/router.py` still carries its own copy of `clean_page`, written before there was
a second caller. It should adopt this module, but that file is under active review in #147
and moving code out from under an open review buys a merge conflict for no functional gain;
the duplication is recorded here rather than left to be discovered.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Query

from src.core.errors import AppApiError

# Bounds `(page - 1) * page_size` comfortably inside int64, so an absurd page number 422s
# instead of overflowing asyncpg's OFFSET parameter — which surfaces as a raw `DataError`
# and a 500, not a refusal a client can read.
MAX_PAGE = 100_000

PageQuery = Annotated[int, Query(description="1-based page number.")]


def clean_page(value: int) -> int:
    """Reject an out-of-range `?page=` in this platform's `{error:{message}}` 422 shape.

    Validated here rather than with FastAPI `ge`/`le` bounds for the reason `clean_limit`
    documents: a native bound emits `{detail:[...]}`, which would put two different 422
    bodies on one endpoint and `error_responses(...)` structurally cannot document both.
    """
    if not 1 <= value <= MAX_PAGE:
        raise AppApiError(422, f"page must be between 1 and {MAX_PAGE}.")
    return value
