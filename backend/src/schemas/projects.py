"""Request/response schemas for the projects domain (post schema-separation refactor).

All models subclass the shared `CamelModel` (snake_case in Python, camelCase on the wire),
live here in `src/schemas/`, and are re-exported from `src/schemas/__init__.py`. The name and
description write rules (strip, required, length/word cap — KD-8, #191) are enforced HERE at
the Pydantic boundary, not the DB column: a `ValueError` in a validator becomes the API's 422.
Neither field may be blanked to empty/whitespace any more — that was description's old
behaviour (normalize to NULL) before #191 made it required; the column itself stays nullable
regardless, so a project written before #191 with no description is untouched.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import field_validator

from src.core.words import count_words
from src.db.models.deleted_project import (
    MAX_DELETE_REMARK_CHARS,
    MAX_DELETE_REMARK_WORDS,
    MIN_DELETE_REMARK_WORDS,
)
from src.db.models.project import (
    MAX_PROJECT_DESCRIPTION,
    MAX_PROJECT_DESCRIPTION_WORDS,
    MAX_PROJECT_NAME,
    MAX_PROJECT_NAME_WORDS,
    MIN_PROJECT_DESCRIPTION_WORDS,
)
from src.schemas.base import CamelModel
from src.schemas.marketplace import MarketplaceEntry


def _clean_name(value: str) -> str:
    """The ONE name rule, shared by `ProjectCreate` and `ProjectPatch` — so create and RENAME
    are both covered. Fixing only create leaves the limit half real, and rename is the half
    with no client-side guard at all.

    Messages are written for a person: they reach the screen verbatim (the portal flattens
    Pydantic's `detail[].msg` straight through), so a validator string IS product copy here,
    intended or not."""
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


def _clean_description(value: str) -> str:
    """The ONE description rule (#191), shared by `ProjectCreate` and `ProjectPatch` the same
    way `_clean_name` is — one change covers create and edit both.

    A description is required and WORD-bounded — 15 to 120 (#191 R12) — following
    `clean_deletion_reason`'s shape (char-cap backstop first, then the word-count checks
    each with their own message), NOT `_clean_name`'s (which only ever checks a maximum).
    The minimum exists because a one-line description embeds into a single vector for
    semantic search (slice 3) and a description too short to say anything embeds to nothing
    worth matching; the maximum exists because a long multi-topic description embeds to a
    vector that matches everything weakly, and `ts_rank_cd` has no document-length
    normalisation to protect a precise description from being out-ranked by a rambling one.

    Blank-to-NULL normalization is GONE (KD-8's old behaviour): a description can no longer
    be written as empty, because it is no longer optional. The column itself stays nullable
    (R13) — a project created before #191 with no description is untouched and keeps
    working (R14); this validator only governs what a NEW write may contain.
    """
    value = value.strip()
    if not value:
        raise ValueError("What should this app do?")
    # The character bound stays: the column has no width limit of its own, but the field is
    # injected into every project chat turn (KD-8), so this remains the paste backstop. The
    # WORD rule is the one a person is told about; this one they should never meet.
    if len(value) > MAX_PROJECT_DESCRIPTION:
        raise ValueError(
            f"That description is too long. Keep it under {MAX_PROJECT_DESCRIPTION} characters."
        )
    words = count_words(value)
    if words < MIN_PROJECT_DESCRIPTION_WORDS:
        raise ValueError(f"Say a bit more — at least {MIN_PROJECT_DESCRIPTION_WORDS} words.")
    if words > MAX_PROJECT_DESCRIPTION_WORDS:
        raise ValueError(f"Keep it under {MAX_PROJECT_DESCRIPTION_WORDS} words.")
    return value


class ProjectCreate(CamelModel):
    name: str
    description: str

    _v_name = field_validator("name")(_clean_name)
    _v_description = field_validator("description")(_clean_description)


class ProjectDuplicateCheckRequest(CamelModel):
    """The body `POST /v1/projects:check-duplicates` takes (#191 slice 4, R31) — searched
    against the live marketplace BEFORE a project exists, so there is no project id to hang
    this off yet. Validated by the SAME rule `ProjectCreate.description` uses: the create
    form only reaches this check after its own description already clears the word bound,
    so a request that fails validation here is not a citizen typing, it is a caller
    bypassing the form."""

    description: str

    _v_description = field_validator("description")(_clean_description)


class ProjectDuplicateCheckResponse(CamelModel):
    """At most `services.projects.duplicates.MAX_MATCHES` entries (R34's confidence bar
    already applied), each the SAME four-field shape the marketplace itself shows (R33) —
    reusing `MarketplaceEntry` rather than a parallel type keeps the exposure boundary that
    schema documents in one place."""

    matches: list[MarketplaceEntry]


class ProjectDuplicateResolution(CamelModel):
    """The body `POST /v1/projects:duplicate-check-resolved` takes (#191 R39) — what the
    citizen did once shown possible duplicates. A closed set: FastAPI 422s a typo instead of
    silently logging an event nothing downstream recognises."""

    resolution: Literal["opened_existing", "created_anyway"]


class ProjectPatch(CamelModel):
    """Partial update — apply only fields present in `model_fields_set` (absent ≠ null).
    Neither `name` nor `description` may be cleared to NULL (enforced in the route) —
    #191 widened the rename path's existing rule to cover description too."""

    name: str | None = None
    description: str | None = None

    @field_validator("name")
    @classmethod
    def _v_name(cls, value: str | None) -> str | None:
        # A provided name is cleaned; an explicit null is left for the route to reject.
        return None if value is None else _clean_name(value)

    @field_validator("description")
    @classmethod
    def _v_description(cls, value: str | None) -> str | None:
        # Same shape as `_v_name` above: a provided description is cleaned; an explicit
        # null is left for the route to reject (R11 — description cannot be cleared either).
        return None if value is None else _clean_description(value)


def clean_deletion_reason(value: str, *, subject: str) -> str:
    """Why this `subject` is being deleted — 5 to 50 WORDS.

    The same shared rule as the title cap: `count_words` here,
    `portal/src/utils/words.ts` in the browser, both pinned against the same inputs. The
    client keeps the person inside the limit and the server enforces it independently.

    A lower bound is unusual and deliberate. The reason exists so an administrator reading
    a deletion months later learns something; "no" and "done" satisfy a required field
    without satisfying that, and a field that can be dismissed in one word is a field that
    will be.

    ONE RULE, TWO DELETES. The citizen deleting their own project and the administrator
    destroying somebody else's app answer the same question under the same bounds, and both
    dialogs share `words.ts`'s counter — so they share the validator too, with `subject`
    supplying the only word that differs. A second copy of these four checks is how the two
    surfaces end up disagreeing about what a word is.
    """
    value = value.strip()
    if not value:
        raise ValueError(f"Say why you are deleting this {subject}.")
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


def _clean_delete_remark(value: str) -> str:
    """The project delete's own binding of the shared rule."""
    return clean_deletion_reason(value, subject="project")


class ProjectDeleteRequest(CamelModel):
    """The body `DELETE /v1/projects/{id}` requires: deletion destroys the app, database,
    files and all chats, permanently — and a stated reason replaced an earlier type-the-name
    gate, because retyping a name proves you can read, not that you meant it, while the reason
    is the part still useful a month later.

    THE REASON IS THE ONLY THING THE CLIENT GETS TO SAY. WHO deleted it is stamped by the route
    from the authenticated session, never carried in the body — it was briefly a body field, and
    that was wrong: a client-supplied name can name somebody who did not act. An extra
    `deletedByName` is silently ignored, as Pydantic ignores any unknown key."""

    remark: str

    _v_remark = field_validator("remark")(_clean_delete_remark)


class ProjectResponse(CamelModel):
    id: uuid.UUID
    name: str
    description: str | None
    # Read-only discovery of the project's ONE app — additive and nullable: a fresh project
    # has no app yet, so the SPA needs no mutating provision just to learn whether (and in
    # what lifecycle state) an app exists.
    app_id: str | None = None
    app_status: str | None = None
    # IS IT SERVING RIGHT NOW? Defined as: "live = deployed / published — if the application
    # is published and has url". That is a DEPLOYMENT fact and cannot be
    # read off `app_status`: APPROVED means an administrator said yes, not that anything is
    # running, and `PublishStatusChip` keeps `Approved` and `Live` apart for the same
    # reason. Derived by `services/deploy/liveness.live_app_ids`, the one definition the
    # marketplace and the dashboard count also read, so a row and the number above it can
    # never disagree.
    #
    # `False` for a project with no app at all — there is nothing that could be live.
    # NO DEFAULT. `_to_response` was made keyword-only and required specifically to stop a
    # call site silently omitting this: with `= False` here, three of five endpoints answered
    # a live app as not serving. A default one layer down would let a future direct
    # `ProjectResponse(...)` construction reintroduce exactly that bug; every call site
    # already passes it via `_to_response`.
    is_serving: bool
    # Whether this project has a bundle a Relaunch could actually restore.
    # THREE-STATE ON PURPOSE: `true` = there is one, `false` = confirmed there is not,
    # `null` = the object store could not be reached, so the platform declines to claim
    # anything in either direction and the client renders the plain empty state.
    #
    # Computed by `restorable_presence`, which is the platform's turn-boundary recovery copy
    # OR the user's explicit Save — the same pair a restore
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
    # WHETHER THE OWNER HAS EVER SAVED — narrower than `has_relaunchable_snapshot` on purpose
    # (#198 R10's second sentence). The shared runtime restores ONLY from the saved bundle,
    # never the autosave/recovery copy `has_relaunchable_snapshot` also counts (R21) — a
    # recipient told "restorable" on the strength of an autosave alone would press Launch into
    # a guaranteed 404. Computed by the same `snapshot_presence` check `create_share` (R10's
    # first sentence) already refuses a share creation on, so the two surfaces agree.
    #
    # `null` for an owner's own view (irrelevant there — an owner uses Relaunch, which reads
    # `has_relaunchable_snapshot` instead) and for a shared view with no app at all. `false`
    # is what lets the restricted workspace disable Launch and explain why, rather than
    # letting a recipient press it into a failure it could have shown without the round trip.
    has_saved_snapshot: bool | None = None
    created_at: datetime
    updated_at: datetime
    # WHO THE CALLER IS TO THIS PROJECT (#198 R11/R14) — "owner" everywhere except the one
    # place a share can widen access, `get_project`. This is what the restricted workspace
    # view keys off client-side; the API's OWN refusal of every mutating action for a
    # recipient does not depend on this field at all (those routes call the strict
    # `owned_project_or_404`, which a share never satisfies) — it exists so the UI can decide
    # what to render before it tries something the API would refuse anyway.
    access: Literal["owner", "shared"] = "owner"


class ProjectCountsResponse(CamelModel):
    """The three numbers above the project list. A DEDICATED route, not a count derived from
    the listing: the list joins rows and is PAGINATED, so a client holding 8 of 12 rows cannot
    compute any of these, and polling the full list just for three integers pays that cost.

    `in_production` reads the SAME `live_app_ids` collapse the status column does, so the
    headline number and the rows beneath it can never disagree."""

    # "Live = deployed / published — if the application is published and has url". NOT
    # `AppStatus.APPROVED`, which means an administrator said yes and nothing about whether
    # anything is serving.
    in_production: int
    # Every application the citizen has ever created, whatever its state.
    total_applications: int
    # Moving through the pipeline: submitted, approved-but-not-yet-live, or changes
    # requested. Deliberately NOT "everything that is not live" — a project with nothing
    # built yet has not entered the pipeline, and counting it would make this number read
    # as a backlog that nobody can act on.
    in_pipeline: int


class ProjectListResponse(CamelModel):
    """An OFFSET page envelope, deliberately NOT the keyset one admin rosters use: numbered
    pages ("Page 1 of 2") need a `total`, which keyset declines to compute.

    THE COST IS REAL, NOT ASSUMED AWAY. `pagination.py` refuses offset because a row inserted
    underneath a page walk can duplicate or skip an entry at a boundary, and because `total` is
    a second read under READ COMMITTED rather than one snapshot with the page. Both stay true
    here; what makes them acceptable is written at `list_projects`, not here.

    `total` counts AFTER `q` is applied — it describes the search, never the whole collection."""

    items: list[ProjectResponse]
    page: int
    page_size: int
    total: int
    total_pages: int
