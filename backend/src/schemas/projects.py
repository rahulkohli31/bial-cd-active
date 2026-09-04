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

from src.core.words import count_words
from src.db.models.deleted_project import (
    MAX_DELETE_REMARK_CHARS,
    MAX_DELETE_REMARK_WORDS,
    MIN_DELETE_REMARK_WORDS,
)
from src.db.models.project import (
    MAX_PROJECT_DESCRIPTION,
    MAX_PROJECT_NAME,
    MAX_PROJECT_NAME_WORDS,
)
from src.schemas.base import CamelModel


def _clean_name(value: str) -> str:
    """The ONE name rule, shared by `ProjectCreate` and `ProjectPatch` — so create and
    RENAME are both covered. #158 §14 is explicit that fixing only create leaves the limit
    half real, and rename was the half with no client-side guard at all.

    The messages are written for a person. They used to reach the screen verbatim as
    "Value error, name must be at most 120 characters", because the portal flattens
    Pydantic's `detail[].msg` straight through — so a validator string IS product copy
    here, whether or not anyone intended it to be.
    """
    value = value.strip()
    if not value:
        raise ValueError("Give the project a name.")
    # The character bound stays: it is the column width, and it is what stops a paste of
    # arbitrary size reaching the database. The WORD rule is the one a person is told
    # about; this one is a backstop they should never meet.
    if len(value) > MAX_PROJECT_NAME:
        raise ValueError(f"That name is too long. Keep it under {MAX_PROJECT_NAME} characters.")
    if count_words(value) > MAX_PROJECT_NAME_WORDS:
        raise ValueError("Keep the title short — about 6 to 8 words.")
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


def _clean_delete_remark(value: str) -> str:
    """Why this project is being deleted — 5 to 50 WORDS (#158 §13.2).

    The same shared rule as the title cap: `count_words` here,
    `portal/src/utils/words.ts` in the browser, both pinned against the same inputs. The
    client keeps the person inside the limit and the server enforces it independently —
    which is the shape §13.2 asks for, and the opposite of the rename path it cites as the
    thing not to repeat.

    A lower bound is unusual and deliberate. The remark exists so an administrator reading
    a deletion months later learns something; "no" and "done" satisfy a required field
    without satisfying that, and a field that can be dismissed in one word is a field that
    will be.
    """
    value = value.strip()
    if not value:
        raise ValueError("Say why you are deleting this project.")
    # The paste backstop, which a person should never meet.
    if len(value) > MAX_DELETE_REMARK_CHARS:
        # The character cap fires on something a WORD cap cannot express: a 40-word paste of
        # long words, URLs or a non-English script can clear 2000 characters while genuinely
        # under 50 words, and telling that person to get under a bound they are already under
        # is not actionable. Matches `_clean_name`'s own character-cap message for the same
        # reason.
        raise ValueError(
            f"That reason is too long. Keep it under {MAX_DELETE_REMARK_CHARS} characters."
        )
    words = count_words(value)
    if words < MIN_DELETE_REMARK_WORDS:
        raise ValueError("Give a little more detail — at least 5 words.")
    if words > MAX_DELETE_REMARK_WORDS:
        raise ValueError("Keep the reason under 50 words.")
    return value


class ProjectDeleteRequest(CamelModel):
    """The body `DELETE /v1/projects/{id}` now requires.

    Deleting a project destroys its app, its database, its files and all of its chats, and
    none of it comes back. #158 §13.1 replaced the type-the-name gate with a plain
    confirmation plus a stated reason: retyping a name proves you can read, not that you
    meant it, and the reason is the part that is still useful a month later.

    THE REASON IS THE ONLY THING THE CLIENT GETS TO SAY. The deletion also records WHO, but
    that is stamped by the route from the authenticated session, not carried here. It was
    briefly a body field and that was wrong: a name the client supplies can name somebody
    who did not act, and the field exists precisely so an administrator can tell who did.
    An extra `deletedByName` in the body is ignored, as Pydantic ignores any unknown key.
    """

    remark: str

    _v_remark = field_validator("remark")(_clean_delete_remark)


class ProjectResponse(CamelModel):
    id: uuid.UUID
    name: str
    description: str | None
    # Read-only discovery of the project's ONE app (KD-4) — additive and nullable: a
    # fresh project has no app yet, so the SPA needs no mutating provision just to
    # learn whether (and in what lifecycle state) an app exists.
    app_id: str | None = None
    app_status: str | None = None
    # IS IT SERVING RIGHT NOW? Settled on the #158 call: "live = deployed / published — if
    # the application is published and has url". That is a DEPLOYMENT fact and cannot be
    # read off `app_status`: APPROVED means an administrator said yes, not that anything is
    # running, and `PublishStatusChip` keeps `Approved` and `Live` apart for the same
    # reason. Derived by `services/deploy/liveness.live_app_ids`, the one definition the
    # marketplace and the dashboard count also read, so a row and the number above it can
    # never disagree.
    #
    # `False` for a project with no app at all — there is nothing that could be live.
    # NO DEFAULT. `_to_response` was made keyword-only and required specifically to stop a
    # call site silently omitting this (round-3 fix for round-1 finding 1: three of five
    # endpoints answered a live app as not serving because this WAS `= False`). A default
    # one layer down would let a future direct `ProjectResponse(...)` construction
    # reintroduce exactly that bug; every call site already passes it via `_to_response`.
    is_serving: bool
    # N7 — whether this project has a bundle a Relaunch could actually restore.
    # THREE-STATE ON PURPOSE: `true` = there is one, `false` = confirmed there is not,
    # `null` = the object store could not be reached, so the platform declines to claim
    # anything in either direction and the client renders the plain empty state.
    #
    # WIDENED (R18, 2026-08-11): computed by `restorable_presence`, which is the platform's
    # turn-boundary recovery copy OR the user's explicit Save — the same pair a restore
    # actually consults. The saved bundle alone under-reported by exactly one person: the
    # builder who worked across several turns and never pressed Save. The field name still
    # says "snapshot" because renaming a shipped wire field to fix a nuance is a worse trade
    # than this comment; read it as "restorable".
    #
    # It cannot be derived from `app_status`. `AppStatus.DRAFT` is minted by PROVISION, and a
    # successfully built app stays `draft` until someone submits it for approval — so the
    # tempting `status != 'draft'` predicate would hide Relaunch for the normal case while
    # still claiming a saved build for a project whose first build failed. The only honest
    # source is the object-store HEAD that `relaunch_preview` itself requires.
    #
    # ONLY the single-project GET computes it: the list endpoint would need one HEAD per row,
    # and nothing on that surface offers Relaunch. It stays `null` there and no caller reads it.
    has_relaunchable_snapshot: bool | None = None
    created_at: datetime
    updated_at: datetime


class ProjectCountsResponse(CamelModel):
    """The three numbers above the project list (#158 §1).

    A DEDICATED route rather than a count derived from the listing, for the reason
    `/admin/apps/counts` gives: the list projects rows and joins, and polling it for three
    integers would pay that on a cadence. It is also the only honest option — the list is
    PAGINATED, so a client holding 8 of 12 rows cannot compute any of these.

    `in_production` reads the SAME `live_app_ids` collapse the status column does, so the
    headline number and the rows beneath it cannot disagree. That is the failure this shape
    exists to prevent: a dashboard saying three are live above a list showing two.
    """

    # "Live = deployed / published — if the application is published and has url" (#158
    # call). NOT `AppStatus.APPROVED`, which means an administrator said yes and nothing
    # about whether anything is serving.
    in_production: int
    # Every application the citizen has ever created, whatever its state.
    total_applications: int
    # Moving through the pipeline: submitted, approved-but-not-yet-live, or changes
    # requested. Deliberately NOT "everything that is not live" — a project with nothing
    # built yet has not entered the pipeline, and counting it would make this number read
    # as a backlog that nobody can act on.
    in_pipeline: int


class ProjectListResponse(CamelModel):
    """An OFFSET page envelope — deliberately NOT the keyset one the admin rosters use.

    This list was keyset (`nextCursor` + `hasMore`, a forward-only "Load more") until #158
    specified numbered pages and a rows-per-page selector: `Showing 1-8 of 12`,
    `Page 1 of 2`. Neither sentence is expressible without a `total`, and a total is exactly
    what the keyset envelope declines to compute. The design is the requirement, so the
    envelope changed rather than the design.

    THE COST, STATED RATHER THAN ASSUMED AWAY. `pagination.py` refuses offset because a row
    inserted underneath a page walk duplicates or skips an entry at a boundary, and because
    `total` is a second read under READ COMMITTED rather than one snapshot with the page.
    Both remain true here. What makes it acceptable is written at `list_projects`, and it is
    NOT the marketplace's argument — see there.

    `total` is the count AFTER `q` is applied, so "Showing 1-8 of 12" describes the search
    the rows answer, never the whole collection.
    """

    items: list[ProjectResponse]
    page: int
    page_size: int
    total: int
    total_pages: int
