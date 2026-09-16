"""Offset paging — the deliberate exception to `pagination.py`, and where its argument goes.

`pagination.py` is the platform's KEYSET contract: keyset, not offset, no `total`/`totalPages`,
because offset cannot guarantee a duplicate- or skip-free page while rows insert underneath it.
Three surfaces need page NUMBERS, which keyset cannot express, and each states its own reason
rather than inheriting the one before it: the marketplace catalog (read-only, so that guarantee
protects nothing there), the projects list (numbered pages and a rows-per-page selector are the
specified design, and create and delete are the reader's own writes), and the shared-with-me list
(the same specified design over a list MANY people write into — the one place the keyset rule is
overruled rather than found inapplicable, argued at `services/projects/shares.py`). This module
holds only the two helpers the shape needs — a bounded `page` and its 422 — so a fourth caller
writes its own argument rather than treating the import as a blanket blessing of offset.
`marketplace/router.py` still carries an older, duplicate `clean_page`; adopting this module is
deferred while that file is under review.
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
