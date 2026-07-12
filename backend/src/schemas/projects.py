"""Request/response schemas for the projects domain (KD-7, post schema-separation refactor).

All models subclass the shared `CamelModel` (snake_case in Python, camelCase on the wire),
live here in `src/schemas/`, and are re-exported from `src/schemas/__init__.py`. The
name/description write rules (strip, empty→NULL, length cap — KD-8) are enforced HERE at the
Pydantic boundary, not the DB column: a `ValueError` in a validator becomes the API's 422.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import field_validator

from src.db.models.project import MAX_PROJECT_DESCRIPTION, MAX_PROJECT_NAME
from src.schemas.base import CamelModel


def _clean_name(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("name must not be empty")
    if len(value) > MAX_PROJECT_NAME:
        raise ValueError(f"name must be at most {MAX_PROJECT_NAME} characters")
    return value


def _clean_description(value: str | None) -> str | None:
    # Normalize empty/whitespace to NULL so "present" (U7) and "non-null" (U8) have no
    # undefined empty-string third state (KD-8); cap the stored value's length.
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if len(value) > MAX_PROJECT_DESCRIPTION:
        raise ValueError(f"description must be at most {MAX_PROJECT_DESCRIPTION} characters")
    return value


class ProjectCreate(CamelModel):
    name: str
    description: str | None = None

    _v_name = field_validator("name")(_clean_name)
    _v_description = field_validator("description")(_clean_description)


class ProjectPatch(CamelModel):
    """Partial update — apply only fields present in `model_fields_set` (absent ≠ null).
    `description` may be cleared to NULL; `name` (NOT NULL) may not (enforced in the route)."""

    name: str | None = None
    description: str | None = None

    @field_validator("name")
    @classmethod
    def _v_name(cls, value: str | None) -> str | None:
        # A provided name is cleaned; an explicit null is left for the route to reject.
        return None if value is None else _clean_name(value)

    _v_description = field_validator("description")(_clean_description)


class ProjectResponse(CamelModel):
    id: uuid.UUID
    name: str
    description: str | None
    # Read-only discovery of the project's ONE app (KD-4) — additive and nullable: a
    # fresh project has no app yet, so the SPA needs no mutating provision just to
    # learn whether (and in what lifecycle state) an app exists.
    app_id: str | None = None
    app_status: str | None = None
    created_at: datetime
    updated_at: datetime


class ProjectListResponse(CamelModel):
    """The keyset page envelope (KD-1): items + the next cursor + whether more remain. No
    `total`/`totalPages` — keyset does not cheaply provide them and they are not required."""

    items: list[ProjectResponse]
    next_cursor: str | None
    has_more: bool
