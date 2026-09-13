"""Super-admin governance + user-limits/feedback + connector-access schemas.

All request/response models for the THREE admin routers (`/admin/apps` governance,
`/admin` users/limits/feedback, and `/admin/connector-requests`), on the shared
`CamelModel` base — camelCase over the wire, matching the admin SPA panels
(`AppRegistryPanel`, `AuditDrawer`, `IntegrationsPanel`, …).

The third router lives in its own module (`admin/connectors.py`) because `admin/router.py`
is already ~2,500 lines; its schemas nevertheless stay here, with the other two surfaces'.
See the section comment above them for why.

THE ONE IMPORT THIS MODULE TAKES FROM ANOTHER v1 SURFACE is `ConsentLine`, off the citizen's
`api/v1/connectors/schemas.py`. It is not a citizen-specific shape: it is the wire mirror of
`core.connectors.ConsentLine`, a `lead` and a `body`, and BOTH consent panels — the citizen's
`WHAT AN APPROVAL GIVES YOU` and the administrator's `WHAT APPROVING GIVES THEM` — cross the
wire as lists of it. A second, identical Pydantic model here would be two names for one wire
shape, free to drift the day either side gains a field, which is the exact failure both
docblocks already exist to prevent. The dependency runs one way and closes no loop: that module
imports from `db.models`, `schemas` and `services.connectors`, and nothing from `admin`.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import AfterValidator, AnyUrl, Field, UrlConstraints, field_validator

from src.api.v1.connectors.schemas import ConsentLine
from src.db.models.app_registry import MAX_DEPLOYED_URL, ApprovalRoute, AppStatus
from src.db.models.connector_access import ConnectorRequestStatus
from src.db.models.worker_pass import PassOutcome
from src.schemas import CamelModel, clean_stated_reason


def _fits_the_column(url: AnyUrl) -> AnyUrl:
    """Bound the SERIALIZED url — the value that reaches `varchar(MAX_DEPLOYED_URL)`.

    `UrlConstraints(max_length=…)` measures the INPUT string, but pydantic normalizes a
    path-less `https://…` with a trailing `/` on parse — so a 2083-char input could clear
    the constraint and still hand 2084 chars to the column (an uncaught asyncpg 500 where
    the admin deserves a 422). Re-measuring the parse OUTPUT is what makes 0019's "a URL
    that parses at the boundary always fits" true by validation, not luck.
    """
    if len(str(url)) > MAX_DEPLOYED_URL:
        raise ValueError(f"URL must be at most {MAX_DEPLOYED_URL} characters")
    return url


# The deployed-app address, parsed at the boundary ("parse, don't validate"): a
# real URL, `https` ONLY. Rejecting `http` is not pedantry — the recorded URL becomes
# a link the owner clicks, and this is the one place a typo'd or plaintext address can
# be caught before it is handed to a user. `javascript:`/`data:` and free-text junk
# fall out of the same parse (422), so no handler ever re-checks the string. The length
# is bounded TWICE by necessity: `UrlConstraints` on the way in, `_fits_the_column` on
# what the parse actually produced (the only value the column ever sees).
HttpsUrl = Annotated[
    AnyUrl,
    UrlConstraints(max_length=MAX_DEPLOYED_URL, allowed_schemes=["https"]),
    AfterValidator(_fits_the_column),
]

# --- governance (`/admin/apps`) ------------------------------------------------


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
    login_required: bool
    # Derived from the approved pin (`approved_submission_id is not None`) — the old
    # JSX-snapshot derivation is gone with the column it read.
    has_approved_snapshot: bool
    # The submission under review: what the reviewer inspects, and the id
    # approve must echo back.
    submission_id: uuid.UUID | None
    commit_sha: str | None
    submitted_at: datetime | None
    # The approved pin: the artifact the runbook operator deploys — the SHA is
    # their identity check after cloning the downloaded bundle.
    approved_submission_id: uuid.UUID | None
    approved_commit_sha: str | None
    approved_by: uuid.UUID | None
    approved_at: datetime | None
    # The manual-runbook marker: `redeploy_needed` is exact —
    # `approved_submission_id != deployed_submission_id` — so an approved-but-
    # undeployed app and a re-approved-since-deploy app both surface it.
    deployed_at: datetime | None
    # The recorded live address — read back as a plain string, never re-parsed:
    # a value already in the column was parsed when it was written, and re-validating
    # it here would turn one bad legacy row into a 500 on the whole admin queue.
    deployed_url: str | None
    # ALWAYS false for the self-publish lineage, whatever the pins say: the flag
    # is a runbook prompt, and a self-published app has no runbook step for anyone to
    # perform. For every other lineage it stays the exact id comparison above.
    redeploy_needed: bool
    # Which lineage the current submission entered through: `runbook`,
    # `self_publish`, or null (never submitted, or an interim pre-publish-flow row —
    # null keeps today's behaviour everywhere). The admin SPA keys the runbook
    # affordances off this: a `self_publish` row renders neither "Deploy needed" nor
    # "Mark deployed" — and the server refuses the latter regardless.
    approval_route: ApprovalRoute | None
    # What the publish flow attached at submit: both answer sets, the
    # per-question differences, and the citizen's REDACTED explanation — so the review
    # screen can lead with the disagreement without a second call. Shape is
    # deliberately untyped here (the questionnaire is expected to be reworded); null
    # for runbook-lineage and pre-feature rows, and the screen says so rather than
    # rendering blanks. Never contains evidence locations.
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
    """One page of the registry listing, plus whether it IS the whole set.

    `truncated` exists because the badge and list come from different queries: the count is
    an uncapped `GROUP BY`, the listing stops at `LISTING_CAP`. Past the cap the badge advertised
    a number the list refused to show — and since the pending tab sorts OLDEST FIRST, the rows
    that vanished were the NEWEST submissions, invisible to every administrator who looked.
    Pagination stays deferred; making the cap VISIBLE stops the two surfaces disagreeing."""

    apps: list[AdminAppOut]
    truncated: bool = False


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


class MarkDeployedRequest(CamelModel):
    """The optional deployed-URL payload. The whole BODY is optional (the admin SPA already
    posts `{}`), and so is the field: an admin who ran the runbook but has no URL to hand
    still records the marker.

    OMITTING `deployedUrl` means "leave the recorded URL as it is" (fail-first's optional-knob
    exception): a re-deploy of the same app keeps the same address, so a bare re-mark must not
    blank out the Live link the owner is already using. Recording a *different* URL just passes
    the new one."""

    deployed_url: HttpsUrl | None = None


class MarkDeployedResponse(CamelModel):
    app_id: uuid.UUID
    deployed_submission_id: uuid.UUID
    deployed_at: datetime
    # Echoed back so the admin SPA can show what is now recorded — including the
    # carried-forward URL when this mark did not supply one.
    deployed_url: str | None


class DeployCredentialResponse(CamelModel):
    """The long-lived per-app Blob credential the go-live runbook injects into the deployed
    container as `BIAL_BLOB_CONTAINER_URL` + `BIAL_BLOB_SAS`. `sas` is a 365-day bearer
    credential: the admin pastes it straight into an ACA secret and it is NEVER logged, NEVER
    written to the audit trail (the audit row carries the expiry, not the token), and never part
    of any list projection. `expiresAt` comes from the app's stored access policy — deleting that
    policy revokes this credential (the runbook's incident-response lever)."""

    container_url: str
    sas: str
    expires_at: datetime


class DatabaseCredentialResponse(CamelModel):
    """The project database's connection string, for the go-live runbook's
    `BIAL_DATABASE_URL`. `dsn` embeds the app role's password, so it is the same
    kind of object as `DeployCredentialResponse.sas`: returned in this body and nowhere else
    — never logged, never in the audit `detail` (which records `roleName` + `host` instead),
    never in a list projection.

    `dbName` / `roleName` / `host` are the non-secret half, repeated so an operator can
    identify and later reconcile the database without re-reading the credential."""

    dsn: str
    db_name: str
    role_name: str
    host: str


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
    """The admin app-delete's binding of the shared 5-50 word stated-reason rule. The sentence
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
    which has no foreign key to the app and so outlives it. Same 5-50 word bounds and the same
    validator as the project delete, so the two dialogs cannot disagree about what a word is.

    IT TAKES A BODY ON A DELETE, like `DELETE /v1/projects/{id}` and for the same reason: a
    50-word reason does not belong in a query string. RFC 9110 leaves content on a DELETE
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
    # --- Is the scheduled worker alive? ------------------------------------------
    # THE FLEET COUNT ABOVE CANNOT ANSWER THIS. Every alarm the reclamation pass raises is
    # emitted BY the pass, so a crashlooping scheduler emits nothing and reads exactly like a
    # healthy quiet fleet. The only detector of a dead worker is the ABSENCE of a pass record,
    # which is why these two fields hang off the operator's existing fleet endpoint rather than
    # waiting for a metrics system this deployment does not have.
    #
    # `lastReclamationPassAt` is null when no pass has EVER run — a fresh deployment, or a
    # worker that has never started. `reclamationStale` is the derived answer an operator
    # actually wants, and it is true in that null case too: never-ran and stopped-running are
    # different causes with the same consequence.
    last_reclamation_pass_at: datetime | None = None
    reclamation_stale: bool = True


class ReclamationCandidate(CamelModel):
    """One container the pass would act on, with the evidence behind the decision.

    THE TIER AND THE REASON TRAVEL WITH THE VERDICT, deliberately. An operator reading this at 2am
    has to be able to DISAGREE with it — "high_confidence / staged on an earlier pass and idle
    since" is a claim they can check, and a bare `destroy` is one they can only accept."""

    name: str
    tier: str
    verdict: str
    reason: str


class ReclamationReportResponse(CamelModel):
    """What the reclamation pass would do RIGHT NOW, without doing any of it.

    THE QUESTION AN OPERATOR COULD NOT ASK before: the only ways to learn what a pass would
    delete were the worker's logs after the fact, or arming it to find out. This answers before
    the decision, running the same classifier over the same three sources.
    THE FLAGS TRAVEL WITH THE VERDICTS because they change what the verdicts MEAN: the same
    `destroy` list is a preview off-armed, a description of what is about to happen once armed.
    COUNTS TO THE AUDIT ROW, NAMES ONLY TO THE RESPONSE — the split every sibling report makes."""

    scanned: int
    spared: int
    staged: int
    destroy: int
    escalate: int
    not_ours: int
    #: Enumerated containers carrying no identity tag at all. `SANDBOX_RECLAIM_DESTROY`'s
    #: precondition is this reading zero across the fleet — before this field, checking it meant
    #: running the tag-backfill endpoint, a write, to find out. Not the same number as `escalate`:
    #: an untagged container always escalates, but so does a tagged-and-ownerless one and a
    #: foreign control plane's container, so that count cannot answer this question alone.
    untagged: int
    #: The pass REFUSED to judge: too little of the live fleet is claimed by the coordination
    #: store, so the spare-list is not trustworthy enough to sentence anything by. Every verdict
    #: below is meaningless when this is true, which is why it is reported and not hidden.
    store_fault: bool
    #: Destroy candidates AND escalations — everything a human has a decision to make about.
    #: Spared containers are the boring majority and are a count only.
    candidates: list[ReclamationCandidate]
    #: What THIS PROCESS'S flags say right now — and that qualifier is the whole of the field's
    #: meaning, because two processes read two different files. `reclaimEnabled` is read from the
    #: API's own `settings.sandbox`, while the scheduled pass is gated on the WORKER's, loaded from
    #: a different env file in a different container.
    #: An operator who set `SANDBOX__RECLAIM_ENABLED` in `.env` and not `.env.worker` — the
    #: ordinary mistake, since `.env` is the file everyone edits — got `reclaimEnabled: true`
    #: beside a worker declining every pass with `flag_off`. This field therefore answers "did my
    #: config change reach the API", never "is the pass running"; `lastPassOutcome` below is the
    #: only field that can answer the second. Its meaning is deliberately UNCHANGED: deriving
    #: it from the worker would silently redefine a shipped field.
    #:
    #: `reclaimDestroy` false means a running pass reports rather than acts. This endpoint answers
    #: regardless of both — refusing to preview because the feature is off would make the preview
    #: useless exactly when it is most wanted.
    reclaim_enabled: bool
    reclaim_destroy: bool
    #: The same dead-worker signal `reconcile-sandboxes` carries, for the same reason: this
    #: endpoint runs the pass IN THE REQUEST, so a green report here says nothing at all about
    #: whether the scheduled worker is alive.
    last_reclamation_pass_at: datetime | None = None
    reclamation_stale: bool = True
    #: WHAT THE WORKER ACTUALLY DID, straight off the `worker_passes` row it wrote.
    #:
    #: `lastReclamationPassAt` proves a pass HAPPENED; on its own it cannot say whether the pass
    #: looked at anything. `_record_pass` writes `declined`/`flag_off` deliberately — "reclamation
    #: is switched off" is a thing an operator should SEE rather than infer from silence — so the
    #: row already knew, and the report was the only thing that did not. Surfacing both fields is
    #: what makes `scanned: 0` falsifiable: it is "the fleet is clean" only when the outcome says
    #: a pass ran, and `lastPassDetail` names the resource group and managed environment it ran
    #: against, so a worker sweeping somebody else's subscription cannot read as ours.
    #:
    #: NO SUBSCRIPTION ID travels here, by construction — `workers/reclamation._enumerated_fleet`
    #: keeps it in the server-side log only.
    #:
    #: Both null when no pass has ever been recorded, which pairs with `reclamationStale: true`.
    #: THE WORKER'S OWN ENUM rather than a re-spelled `str`, the same call
    #: `BuildCompilePart.state` makes: a `StrEnum` has an identical wire shape, and a second
    #: copy of the member list is a copy that can drift from the value the producer holds.
    last_pass_outcome: PassOutcome | None = None
    last_pass_detail: str | None = None


class SandboxTagBackfillResponse(CamelModel):
    """What one identity backfill pass did to the pre-existing fleet.

    THE BUCKETS SUM: `scanned == alreadyTagged + stamped + skippedNoRow + failed`. `skippedNoRow`
    containers WERE stamped (`kind` + `backfilled_at`) but match no app row — a sandbox name keeps
    only 28 of 32 hex chars, so it is not invertible — and are therefore escalate-forever.
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


class AuditEventOut(CamelModel):
    id: uuid.UUID
    actor_id: uuid.UUID | None
    # The actor's human handle (email), resolved from `actor_id`, so the admin AuditDrawer can
    # name the actor instead of showing a raw uuid or "anonymous". None if the actor was deleted.
    username: str | None
    action: str
    resource_type: str
    resource_id: str | None
    detail: dict[str, Any] | None
    # The count-bearing detail (flag flips, reconcile tallies) surfaced top-level for the UI.
    count: int | None
    created_at: datetime


class AuditListResponse(CamelModel):
    events: list[AuditEventOut]


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


# --- connector access requests (`/admin/connector-requests`) --------------------
#
# A THIRD ADMIN SURFACE WHOSE ROUTER IS A SEPARATE MODULE (`admin/connectors.py`, because
# `admin/router.py` is already ~2,500 lines) BUT WHOSE SCHEMAS STAY HERE, with the other two
# surfaces'. The admin SPA reads one wire vocabulary across its five tabs, and a reviewer
# comparing this queue's row against the app registry's has both shapes in one file. Splitting
# the schemas would buy a shorter module and cost that comparison.


def _clean_decline_remarks(value: str) -> str:
    """The administrator's decline remark, on the shared 5-50 word stated-reason rule.

    THE SAME RULE THE CITIZEN'S REQUEST USES (owner decision D1), and deliberately NOT the
    20-character `RejectionNote` the app registry rejects with. Both connector dialogs count
    words with `portal/src/utils/words.ts`, so one validator has to answer both sides of this
    conversation or the browser's counter would let through something the API refuses — and
    `RejectionNote`'s character floor is a rule that counter cannot express.

    `say_why` is this surface's own sentence. It is what the administrator is asked for when the
    box is empty, and the words they write are the WHOLE of what a refused person is told:
    `Ask again` is not built, so a decline has no path back and no second explanation."""
    return clean_stated_reason(value, say_why="Say why you are declining this request.")


class ConnectorDeclineRequest(CamelModel):
    """The body `POST /v1/admin/connector-requests/{request_id}/decline` requires.

    THERE IS NO APPROVE BODY AT ALL, and that asymmetry is the `AdminReview` board's largest
    departure (R10). The board draws a permanent `REQUIRED` pill over `YOUR REMARKS` and the
    sentence `Approving needs a remark as well as declining`; both come off. Approving is a
    click that stores nothing, because an approval remark would be readable nowhere — the audit
    row carries ids only, the citizen is never shown it, and `ALREADY DECIDED` has no remarks
    column and no way to reopen a decided row. A write-only column is worse than no column.

    WHO decided is stamped from the authenticated session and never carried in the body."""

    remarks: str

    _v_remarks = field_validator("remarks")(_clean_decline_remarks)


class ConnectorRequestRow(CamelModel):
    """One `connector_access_requests` row as the administrator's queue sees it: who asked, for
    what, in whose words, and what was decided.

    ONE SHAPE FOR BOTH TABLES. `AdminQueue` draws `WAITING ON YOU` and `ALREADY DECIDED` with
    different columns, and the fields outside a row's own status read `null` — the same rule
    `ConnectorEntry` follows for the citizen. Two schemas would put the person, their email and
    the connector in two places to save four nulls in each.

    `displayName` IS NEVER NULL, AND IS NOT ALWAYS `users.display_name`. That column is
    nullable, and this server substitutes the work email exactly as
    `services/connectors/access.PersonAccess` already does for a decider's name — one fallback,
    written once, on the server, so no panel writes a second one and no cell can render an empty
    string beside an authorization decision. The consequence is intended and worth stating: a
    person with no display name renders their email on both lines of the queue's two-line cell.

    `email` IS THE WORK EMAIL, IN PLACE OF THE BOARD'S `department`. `AdminQueue` draws
    `Priya Nair` / `Ground operations`; `department` exists nowhere in this product and there is
    no directory client behind one, so the second line carries the value the platform already
    verifies about a person.

    `decidedById` RIDES SO THE PORTAL CAN RENDER `you` FOR THE RIGHT ADMINISTRATOR. The board
    writes `2 Sep · you` on every decided row, which is true only for the administrator it was
    drawn for; BIAL runs two super-admins, so the console compares this id against
    `GET /v1/auth/me`'s and falls back to `decidedByName`. Hard-coding `you` server-side would
    put one administrator's identity on the other's screen."""

    id: uuid.UUID
    #: The person who asked. The queue is the ONE surface that reads across users, so the
    #: subject's id is on the row rather than inferred from anything.
    user_id: uuid.UUID
    #: `users.display_name`, or their email when that column is null. Never empty.
    display_name: str
    email: str
    #: The stored `connector_key` — stable, lowercase, never rendered.
    connector_key: str
    #: The catalogue's name for it, so the `CONNECTOR` column needs no second lookup and no
    #: component has to know what any connector is called (R18).
    connector_display_name: str
    #: `AdminReview`'s `WHAT APPROVING GIVES THEM` panel — the registry's THIRD-PERSON consent
    #: set, which is a different tuple from the citizen's `consentLinesRequester` and not
    #: derivable from it (`core.connectors` explains why they are two fields).
    #:
    #: ON EVERY ROW, INCLUDING THE DECIDED ONES, AND THAT REDUNDANCY IS THE POINT. The decide
    #: dialog is handed one row and nothing else, so the row is the only object the copy can
    #: ride; a component that reconstructed these three sentences would make "add a second
    #: connector" a component change, which is exactly the claim R18 makes and which
    #: `1935588e` had to come back and repair on the citizen's side. Narrowing it to `waiting`
    #: rows would save a few hundred bytes and reintroduce the state-conditional copy field
    #: `ConnectorEntry`'s docblock argues against.
    consent_lines_approver: list[ConsentLine]
    #: The citizen's own words, in full. The queue renders them untruncated (board) and as plain
    #: text on every surface, never through a markdown component: one user writes this and
    #: another reads it.
    requester_remarks: str
    #: When they asked — the `ASKED` column, which names a time of day, so this is not a date.
    asked_at: datetime
    #: `pending`, `approved` or `declined`. A `cancelled` row is not a decision and is not in
    #: either listing, so that value never crosses this wire.
    status: ConnectorRequestStatus
    #: The four decided-only fields. `null` on a waiting row.
    decided_at: datetime | None = None
    decided_by_id: uuid.UUID | None = None
    #: The decider's display name, or their email. `None` when the administrator who decided has
    #: since been deleted — `decided_by_id` is `ON DELETE SET NULL`, so a decision outlives its
    #: decider and the row keeps its date with the decider unnamed.
    decided_by_name: str | None = None
    #: Written only on a decline (R10). `null` on an approval is correct, not a missing write.
    decision_remarks: str | None = None
    #: `USING IT IN` — how many of THIS person's projects have THIS connector switched on.
    #: `null` on a declined row (the board draws an em dash) and on a waiting one; `0` is a real
    #: answer meaning an approved person who has not switched it on anywhere yet.
    using_it_in: int | None = None


class ConnectorRequestListResponse(CamelModel):
    """One page of the administrator's queue, in the order that table is read in.

    `truncated` EXISTS FOR THE SAME REASON `AppListResponse`'S DOES: the read stops at a cap and
    SAYS so rather than returning a silent prefix. Nothing bounds how many people may ask for a
    connector, and a queue that quietly hid its tail would let a request wait forever with the
    console showing a caught-up screen."""

    requests: list[ConnectorRequestRow]
    truncated: bool = False


class ConnectorWaitingCountResponse(CamelModel):
    """`{ waiting: N }` — the Integrations tab badge's only source.

    ONE NUMBER, AND ONLY THE WAITING ONE. The badge answers "is anybody waiting on me"; a decided
    count has no badge to render it and would be a field with no reader. A DEDICATED route rather
    than `len(requests)` off the listing, for the reason `AppCountsResponse` gives: the listing
    projects up to 200 rows, joins `users` and counts each person's enabled projects, and a badge
    polling it would pay all of that on a cadence — and pay MORE of it as the queue it reports on
    grows, which is exactly backwards."""

    waiting: int


class ConnectorDecisionResponse(CamelModel):
    """What approve and decline answer with: the row's new state, in the shape
    `AdminAppStatusResponse` answers a governance transition with.

    NO DECIDER NAME ON THIS BODY, deliberately. The caller IS the decider, so the console
    renders `you` for the row it just wrote without being told; the name matters only on the
    LISTING, where the other administrator's decisions are read, and it rides `ConnectorRequestRow`
    there. `decidedAt` is here because it is the server's clock, not the browser's."""

    request_id: uuid.UUID
    #: The person the decision is about — the console reloads its queue by it, and a client that
    #: kept the row in place needs to know whose row moved.
    user_id: uuid.UUID
    connector_key: str
    status: ConnectorRequestStatus
    decided_at: datetime
