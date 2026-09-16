"""Writing a project's description embedding."""

from __future__ import annotations

import structlog
from pydantic_ai import Embedder

from src.core.alarms import EMBEDDING_WRITE_FAILED_EVENT
from src.db.models.project import DESCRIPTION_EMBEDDING_DIMENSIONS, Project

logger = structlog.get_logger()


async def write_description_embedding(project: Project, embedder: Embedder | None) -> None:
    """Embed `project.description` and set `project.description_embedding` on the ORM
    object (the caller commits) — or leave the column untouched on any failure.

    NEVER RAISES: an embedding failure must never fail the project write itself. A row left
    with no embedding (or a STALE one, if this was meant to refresh an existing value) simply
    falls back to keyword-only search — the hybrid query already treats an absent embedding as
    a normal case for every project that predates this feature, not a special one. Leaving a
    stale embedding in place on a failed refresh is deliberate, not an oversight: it still
    degrades to "close enough" rather than dropping the row out of semantic search entirely on
    what is usually a transient failure (see `test_a_failed_embed_call_never_clobbers_an_
    existing_embedding`).

    Also degrades (never raises) on a WRONG-WIDTH result: pgvector enforces the declared
    `DESCRIPTION_EMBEDDING_DIMENSIONS` width only at flush time, well outside this function's
    own try/except, so a misconfigured or drifted embedding deployment would otherwise raise
    `DataError` at `db.commit()` and 500 the caller's entire project write. Checking the width
    here, before assignment, keeps that failure on the same degrade path as any other, and
    keeps the "never clobber" rule above true for it too — the bad-width result is simply
    never assigned.

    A no-op when embeddings aren't configured (`embedder is None`) or the project has no
    description at all (nothing to embed). The CALLER decides WHEN to call this: on every
    create (description is always set) and on a patch only when `description` actually
    changed (written when first set and refreshed whenever it changes, not on every unrelated
    edit).
    """
    if embedder is None or project.description is None:
        return
    try:
        result = await embedder.embed_documents(project.description)
        embedding = list(result.embeddings[0])
        if len(embedding) != DESCRIPTION_EMBEDDING_DIMENSIONS:
            raise ValueError(
                f"embedder returned {len(embedding)} dimensions, "
                f"expected {DESCRIPTION_EMBEDDING_DIMENSIONS}"
            )
        project.description_embedding = embedding
    except Exception as exc:  # noqa: BLE001 — degraded state, never a failed write
        logger.warning(
            EMBEDDING_WRITE_FAILED_EVENT,
            project_id=str(project.id),
            reason=type(exc).__name__,
        )
