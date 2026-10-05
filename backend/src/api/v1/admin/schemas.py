"""Super-admin governance + user-limits/feedback schemas.

All request/response models for the two admin routers (`/admin/apps` governance and `/admin`
users/limits/feedback), on the shared `CamelModel` base — camelCase over the wire, matching the
admin SPA panels (`AppRegistryPanel`, `AppSheet`, …).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, Field, field_validator

from src.api.v1.deploy.schemas import RegistryStatus
from src.db.models.app_registry import AppStatus
from src.db.models.deployment import DeploymentStatus
from src.db.models.sandbox_start import SandboxStartKind, SandboxStartMiss
from src.schemas import CamelModel, clean_stated_reason

# --- governance (`/admin/apps`) ------------------------------------------------


class LiveVersion(CamelModel):
    """The version serving now: the number of the send that put it there, its commit, and when
    that send first went live. `number` is null when no recorded send owns the serving attempt,
    and `since` is then when that attempt finished. Any field is null when nothing recorded it."""

    number: int | None
    commit_sha: str | None
    since: datetime | None


class AdminAppOut(CamelModel):
    """The admin projection — NEVER the code blobs, the app key, or a signed URL
    (a bearer credential is minted only by the dedicated download endpoint)."""

    app_id: uuid.UUID
    name: str
    owner_id: uuid.UUID
    # The owner's human handle (email/display name) so the admin UI can render the Owner cell
    # (`AppRegistryPanel` reads `ownerUsername`); the raw `ownerId` uuid is not user-facing.
    owner_username: str | None
    status: AppStatus
    registry_status: RegistryStatus
    # Null when nothing is serving. An app waiting for review can still have an older version live.
    live_version: LiveVersion | None
    login_required: bool
    # Derived from the approved pin (`approved_submission_id is not None`) — the old
    # JSX-snapshot derivation is gone with the column it read.
    has_approved_snapshot: bool
    # The submission under review: what the reviewer inspects, and the id
    # approve must echo back.
    submission_id: uuid.UUID | None
    commit_sha: str | None
    submitted_at: datetime | None
    # The approved pin: the submission the administrator approved.
    approved_submission_id: uuid.UUID | None
    approved_commit_sha: str | None
    approved_by: uuid.UUID | None
    approved_at: datetime | None
    # What the publish flow attached at submit: both answer sets, the
    # per-question differences, and the citizen's REDACTED explanation — so the review
    # screen can lead with the disagreement without a second call. Shape is
    # deliberately untyped here (the questionnaire is expected to be reworded); null
    # on a row queued without one, and the screen says so rather than rendering blanks.
    # Never contains evidence locations.
    declaration: dict[str, Any] | None
    # On-disk size of the project's own database, or null when it has none —
    # never provisioned, not yet ready, or the cluster was unreachable when the page
    # rendered. STRICTLY ADVISORY: nothing anywhere reads it as a quota or a gate. Null
    # means "no number to show", never "zero" and never "over limit".
    database_bytes: int | None
    rejection_note: str | None
    created_at: datetime
    updated_at: datetime


class AppListResponse(CamelModel):
    """The registry listing, most recently active first, plus whether it is the whole set.

    `truncated` exists because the badge and list come from different queries: the count is
    an uncapped `GROUP BY`, the listing stops at `LISTING_CAP`. Making the cap visible stops the
    two surfaces disagreeing with nothing on screen admitting it."""

    apps: list[AdminAppOut]
    truncated: bool = False


class DecisionKind(StrEnum):
    """What was decided about one sent version."""

    WAITING = "waiting"
    PUBLISHED = "published"
    APPROVED = "approved"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"
    NOT_RECORDED = "not_recorded"


class VersionState(StrEnum):
    """Where one sent version stands now."""

    WAITING = "waiting"
    LIVE = "live"
    REPLACED = "replaced"
    TAKEN_OFFLINE = "taken_offline"
    PUBLISHING = "publishing"
    PUBLISH_FAILED = "publish_failed"
    NOT_PUBLISHED = "not_published"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"
    NOT_RECORDED = "not_recorded"


class HistoryDecision(CamelModel):
    """Who decided and when; `note` is a rejection's note. A version published by itself has
    no `by` or `at`, and a field the records never held is null."""

    kind: DecisionKind
    by: str | None
    at: datetime | None
    note: str | None


class HistoryAttempt(CamelModel):
    """One publish attempt of a version, oldest first."""

    status: DeploymentStatus
    started_at: datetime
    finished_at: datetime | None
    failure_code: str | None


class HistoryVersion(CamelModel):
    """One send for publishing. `declaration` is the one stored with that send's decision, null
    when none was. `replacedBy` is the number of the version that went live after it."""

    kind: Literal["version"] = "version"
    number: int
    commit_sha: str | None
    submission_id: uuid.UUID | None
    sent_at: datetime
    sent_by: str | None
    declaration: dict[str, Any] | None
    decision: HistoryDecision
    attempts: list[HistoryAttempt]
    state: VersionState
    published_at: datetime | None
    replaced_by: int | None
    replaced_at: datetime | None


class HistoryEvent(CamelModel):
    """Something done to the app outside any send. A disable carries the enable that ended it."""

    kind: Literal["event"] = "event"
    action: str
    at: datetime
    by: str | None
    reenabled_at: datetime | None


HistoryEntry = Annotated[HistoryVersion | HistoryEvent, Field(discriminator="kind")]


class AppHistoryResponse(CamelModel):
    """An app's versions and events, newest first. `live` reads the same numbering as the App
    Registry's live version. `truncated` says the oldest records were not read."""

    entries: list[HistoryEntry]
    live: LiveVersion | None
    live_url: str | None
    truncated: bool


class AppStatusCounts(CamelModel):
    """How many apps sit in each registry status, zero-filled.

    Every status is REQUIRED rather than a free `dict[str, int]`: the badge that reads this
    is the only thing telling an administrator a queue has items, and a silently-absent key
    would render as "no number" — indistinguishable from zero on a screen whose whole job is
    that distinction. The vocabulary is closed (`AppStatus`), so naming all five costs nothing.
    Field names are single words, so the camel base is a no-op and wire keys match verbatim.
    """

    draft: int
    pending: int
    approved: int
    rejected: int
    disabled: int


class AppCountsResponse(CamelModel):
    """The badge's whole payload — counts, and nothing else.

    Deliberately NOT the listing: `list_apps` returns up to 200 fully-projected rows AND
    runs a cluster size probe per page, so polling it for a number would pay for both and
    grow more expensive as the queue does. This route is one `GROUP BY` and never touches
    the maintenance engine.
    """

    counts: AppStatusCounts


class AdminAppStatusResponse(CamelModel):
    app_id: uuid.UUID
    status: AppStatus


class ApproveRequest(CamelModel):
    # The submission id the admin ACTUALLY reviewed: the guarded UPDATE adds
    # `AND source_submission_id = :submission_id`, so a re-submit between the
    # admin's review and their click updates zero rows → 409, never a silent
    # promotion of an unreviewed bundle.
    submission_id: uuid.UUID


class BundleUrlResponse(CamelModel):
    """The audited out-of-band review download. `url` is a short-TTL bearer
    credential — the SPA uses it immediately and never stores it; it is likewise
    never written to the audit trail."""

    url: str
    submission_id: uuid.UUID
    commit_sha: str | None
    expires_in_seconds: int


# The rejection note's floor. A rejection is the only thing the citizen gets
# back, and an EMPTY note rendered as nothing at all — a bare red badge and no idea what
# to change. The floor is a product decision in disguise (it decides how much an
# administrator must write), so it is a named constant rather than a magic number
# scattered across a schema and a React component: 20 characters, against the 1000-char
# ceiling that has always been here.
MIN_REJECTION_NOTE = 20
MAX_REJECTION_NOTE = 1000

_TOO_SHORT_NOTE = f"Please tell the developer why — at least {MIN_REJECTION_NOTE} characters."


def _says_something(note: str) -> str:
    """Trim, then re-measure: `min_length` alone counts whitespace, so twenty spaces
    would clear the floor and reach the citizen as the blank note the floor exists to
    prevent. The trimmed value is what gets stored, so the column never carries the
    padding either."""
    trimmed = note.strip()
    if len(trimmed) < MIN_REJECTION_NOTE:
        raise ValueError(_TOO_SHORT_NOTE)
    return trimmed


# Bounded at BOTH ends at the boundary (422), never in the handler: an over-long note
# used to be sliced to 1000 chars there, so the admin's reasoning was silently truncated
# and they never learned it happened; an absent one used to be stored as `""`.
RejectionNote = Annotated[
    str,
    Field(min_length=MIN_REJECTION_NOTE, max_length=MAX_REJECTION_NOTE),
    AfterValidator(_says_something),
]


def _clean_app_delete_reason(value: str) -> str:
    """The admin app-delete's binding of the shared word-bounded stated-reason rule. The sentence
    is byte-identical to the one that rule used to build from `subject="app"`."""
    return clean_stated_reason(value, say_why="Say why you are deleting this app.")


class RejectRequest(CamelModel):
    # REQUIRED: "a rejection carries a note back" is the requirement, and an
    # optional field made that a suggestion. Omitting it is a 422 on the missing field, and
    # a too-short or whitespace-only one is a 422 on its content.
    note: RejectionNote


class AppDeleteRequest(CamelModel):
    """The body that `DELETE /v1/admin/apps/{app_id}` requires.

    AN ADMINISTRATOR DESTROYING SOMEBODY ELSE'S APP MUST SAY WHY. The citizen deleting their own
    project already has to; the harsher act — an administrator destroying work that is not
    theirs, with no undo and no export — asked for nothing at all, and the browser
    `window.confirm` it went through could not have collected it.

    The reason rides the `app:delete` audit row this path already writes BEFORE destruction,
    which has no foreign key to the app and so outlives it. Same word bounds and the same
    validator as the project delete, so the two dialogs cannot disagree about what a word is.

    IT TAKES A BODY ON A DELETE, like `DELETE /v1/projects/{id}` and for the same reason: a
    paragraph-long reason does not belong in a query string. RFC 9110 leaves content on a DELETE
    undefined and httpx declines to offer `json=` on `.delete()` for that reason — tests use
    `.request("DELETE", ...)` — but nginx and the container ingress both forward it and the
    admin SPA is the only client.
    """

    reason: str

    _v_reason = field_validator("reason")(_clean_app_delete_reason)


class PatchAppRequest(CamelModel):
    # The app display name is now sourced from the owning project — not settable here.
    # Only the login-required gate remains admin-patchable.
    login_required: bool | None = None


class PrefixReconcileCounts(CamelModel):
    """One object-store prefix's reconciliation tally. Counts only, never a key list.
    `scanned == owned + withinGrace + eligible`; `deleted` is 0 on a report-only prefix
    (`submissions`, `apps`)."""

    scanned: int
    owned: int
    within_grace: int
    eligible: int
    deleted: int


class AttachmentReclaimSummary(CamelModel):
    """The aggregate never-sent-attachment reclaim tally folded into the operator sweep: rows
    reclaimed, quota bytes freed, and object keys swept, summed across every owning user the pass
    touched. Counts only, like `PrefixReconcileCounts` — never a key, a user id or any list."""

    reclaimed: int
    freed_bytes: int
    swept_keys: int


class StorageReconcileResponse(CamelModel):
    """The operator-invoked reconciling sweep's report. For the report-only prefixes the
    report IS the whole product of the endpoint, so it reaches the caller as a typed body rather
    than a log line. `ownerlessSubmissions` names the `submissions/{app_id}/` bundles whose app row
    is gone (past grace) — the set the retention call must rule on. `attachmentReclaim` is the
    never-sent-upload reclaim the sweep now folds in (the quota leak it fixes finally runs in
    prod, not just in its unit test)."""

    attachments: PrefixReconcileCounts
    snapshots: PrefixReconcileCounts
    # `recovery/` — the crash-recovery twin of `snapshots/`. Reported separately rather than
    # folded in, because the two answer different operator questions: a rising orphan count under
    # `snapshots/` means saved versions are outliving their app rows, while one under `recovery/`
    # means the same for bundles no user ever asked for.
    recovery: PrefixReconcileCounts
    submissions: PrefixReconcileCounts
    apps: PrefixReconcileCounts
    ownerless_submissions: int
    attachment_reclaim: AttachmentReclaimSummary


class DatabaseReconcileCounts(CamelModel):
    """The per-project-database half of the orphan sweep. Counts only, never a database name.

    `scanned == notOurs + owned + orphaned + unknownAge`. `unknownAge` is its own bucket
    rather than a share of `orphaned` because `pg_database` has no creation timestamp: the
    provision-time COMMENT is the only age source, and a database whose age cannot be proven
    is deliberately NOT reported as actionable. Nothing in this sweep deletes anything —
    delete-eligibility is a human ruling made with these numbers in hand.
    """

    scanned: int
    not_ours: int
    owned: int
    orphaned: int
    unknown_age: int
    # Whole hours since the oldest orphan's provision stamp; null when there are no orphans.
    # An age, never an identity — it separates "stale for a week" from "a provision that
    # failed five minutes ago and may still be retried".
    oldest_orphan_age_hours: int | None


class RoleReconcileCounts(CamelModel):
    """The login-role half of the same sweep. Counts ONLY.

    `scanned == notOurs + owned + stranded + paired`. `stranded` is the finding that a
    database-only diff cannot see: teardown drops the database and THEN the role, so a
    failure between the two leaves a LOGIN role whose database is gone and whose registry
    row is gone — a re-entry handle nothing else in the system would ever surface. `paired`
    roles still have their database, so the database is already the reported orphan.
    """

    scanned: int
    not_ours: int
    owned: int
    stranded: int
    paired: int


class DatabaseReconcileResponse(CamelModel):
    """The operator-invoked per-project-database sweep's report.

    A SIBLING of `StorageReconcileResponse`, deliberately not an extension of it: that shape
    is frozen around `scanned == owned + withinGrace + eligible`, and a 24h age grace keyed
    off a blob's `last_modified` has no analogue on `pg_database`. The report IS the whole
    product of the endpoint — it deletes nothing.
    """

    databases: DatabaseReconcileCounts
    roles: RoleReconcileCounts


class SandboxReconcileResponse(CamelModel):
    """The operator-invoked sandbox-fleet sweep's report.

    A SIBLING of the storage and database reports, and report-only for the same reason: the
    ambiguity between "orphaned" and "provisioned seconds ago, registry not written yet" is not
    something to hand an irreversible ARM delete.

    Counts for the fleet, NAMES only for the gaps — the operator needs those to act on. The
    names travel in the RESPONSE and never in the audit row."""

    live: int
    registered: int
    unregistered: list[str]
    registered_missing: list[str]


class SandboxTagBackfillResponse(CamelModel):
    """What one identity backfill pass did to the pre-existing fleet.

    THE BUCKETS SUM: `scanned == alreadyTagged + stamped + skippedNoRow + failed`. `skippedNoRow`
    containers WERE stamped (`kind` + `backfilled_at`) but match no app row — a sandbox name keeps
    only 28 of 32 hex chars, so it is not invertible — and stay unowned rather than guessed at.
    `unowned` is that same population, RECOUNTED EVERY PASS, deliberately outside the sum: a
    container `alreadyTagged` on pass 2 makes `skippedNoRow` read zero and `scanned` look clean —
    the trap `unowned` alone still catches. Counts only; failures travel to the logs by name."""

    scanned: int
    already_tagged: int
    stamped: int
    skipped_no_row: int
    failed: int
    unowned: int


class DeployReconcileResponse(CamelModel):
    """The operator-invoked deploy-reconciliation report.

    ONE number — the honest shape, not a thin one. `resolved` counts abandoned rows this pass
    SETTLED; anything richer needs a second read of a table the pass just changed, which
    contradicts itself the moment the scheduled pass and the boot one-shot overlap.

    A row ARM could not answer for is DEFERRED, not resolved — left for the next pass, since a
    throttled request read as "gone" would eventually mark a live app failed. Counts only."""

    resolved: int


# --- users / limits / feedback (`/admin`) --------------------------------------


class LimitFields(CamelModel):
    daily_token_limit: int | None = None
    context_soft_limit: int | None = None
    context_hard_limit: int | None = None


class UserLimitsOut(CamelModel):
    user_id: uuid.UUID
    email: str
    display_name: str | None
    role: str
    # Local suspension marker: null = active. Surfaced so the roster shows
    # who is blocked without a per-user read.
    suspended_at: datetime | None
    # Ever signed in, not signed in now.
    signed_in: bool
    # Today's folded BUILD token spend (all four classes, IST day) — the figure the
    # daily cap actually measures, via the same shared expression the gate reads.
    # One page-wide aggregate feeds this, never a per-row query.
    usage_today: int
    # Today's pre-publish-review spend, as its OWN figure: metered against the
    # citizen for attribution, never part of what the cap measures, and never folded
    # into `usage_today` — one number that means two things is how the ledger went
    # wrong before.
    review_usage_today: int
    limits: LimitFields
    effective_limits: LimitFields


class UsersResponse(CamelModel):
    """The roster page. Keyset envelope fields are additive next to the
    original `{defaults, users}` shape — a called-out SPA contract change."""

    defaults: LimitFields
    users: list[UserLimitsOut]
    next_cursor: str | None
    has_more: bool


class SuspensionResponse(CamelModel):
    user_id: uuid.UUID
    suspended_at: datetime | None


class UsageResetResponse(CamelModel):
    user_id: uuid.UUID
    # Always 0 — the reset target is always today (ist_today()); nothing else can
    # reasonably remain after the row for the day is cleared.
    usage_today: int


class LimitsPatchResponse(CamelModel):
    user_id: uuid.UUID
    limits: LimitFields
    effective_limits: LimitFields


# Comfortably BIGINT-safe (max ~9.2e18) with enormous headroom above any real plan
# tier — rules out a stray extra digit silently uncapping the whole fleet. Checked in
# the router handler (alongside the existing `<= 0` check) rather than as a `Field`
# bound, so an out-of-range value stays a 400 through the app's own `AppApiError`
# path instead of falling through to FastAPI's default 422 on `RequestValidationError`.
MAX_DAILY_TOKEN_LIMIT = 1_000_000_000_000


class BulkLimitsRequest(CamelModel):
    """The admin "Global Limits" bulk apply (sets, never resets-to-default — unlike
    the single-user `LimitFields` patch, there is no "use default" concept in a bulk
    action). `user_ids=None` means every user, system-wide; a non-empty list means
    exactly those users and no others."""

    daily_token_limit: int
    # max_length=2000: the selected-scope path is still a multi-VALUES upsert at 3
    # bind params/row (the client-side `id` default counts), so past ~10,922 ids a
    # caller would otherwise get a driver-level 500 instead of a clean 400. The panel
    # itself caps loaded users well under this (MAX_LOADED_USERS = 2000), so it's
    # unreachable from the UI — this is a wire-contract bound for direct API callers.
    user_ids: list[uuid.UUID] | None = Field(default=None, max_length=2000)
    # Required (and must be true) when `user_ids` is omitted — field-ABSENCE would
    # otherwise be the most destructive input for an irreversible fleet-wide mutation,
    # since it's also the path of least resistance for a caller that forgot the field.
    # Ignored when `user_ids` is a real list, since that scope is already explicit.
    confirm_all: bool = False


class BulkLimitsResponse(CamelModel):
    updated_count: int


class FeedbackItem(CamelModel):
    user_id: uuid.UUID
    email: str
    message: str
    page: str
    created_at: datetime


class FeedbackResponse(CamelModel):
    feedback: list[FeedbackItem]
    total: int


class HarnessCounterRow(CamelModel):
    """One counter's total, and when it was last seen."""

    name: str
    total: int
    occurrences: int
    last_seen_at: datetime | None


class HarnessCountersResponse(CamelModel):
    """`GET /v1/admin/harness-counters` → 200 — the build-harness outcomes, totalled.

    THE QUESTION THIS ANSWERS: did the verdict block a false claim, how often did we restore,
    and did any turn fail to reach a durable copy. NO METRICS DEPENDENCY, deliberately — this
    deployment has none, so the shape is a `GROUP BY` over a small append-only table, the same
    trade `worker_passes` makes.
    Rows are whatever names have been WRITTEN, not the enum's members: a counter added at the
    tool boundary shows up here with no change to this file."""

    counters: list[HarnessCounterRow]
    since: datetime


class SandboxStartMedians(CamelModel):
    """Median milliseconds of each stage over the starts that reached it, and of the whole start,
    door to first page, over the starts that served. `None` where no start had it."""

    admission_ms: int | None
    settings_ms: int | None
    create_ms: int | None
    dev_start_ms: int | None
    first_page_ms: int | None
    browser_visible_ms: int | None
    total_ms: int | None


class SandboxStartKindSummary(CamelModel):
    """The starts of one kind: how many, how they ended, how many took a ready container, why
    the rest did not, and how long each stage took. A start neither served nor failed is still
    under way, or one whose first page no watcher saw."""

    kind: SandboxStartKind
    starts: int
    served: int
    failed: int
    claimed: int
    misses: dict[SandboxStartMiss, int]
    medians: SandboxStartMedians


class SandboxStartsResponse(CamelModel):
    """`GET /v1/admin/sandbox-starts` → 200 — sandbox start times, per kind of start."""

    kinds: list[SandboxStartKindSummary]
    since: datetime
