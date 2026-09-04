"""Super-admin governance + user-limits/feedback schemas.

All request/response models for the two admin routers (`/admin/apps` governance and
`/admin` users/limits/feedback), on the shared `CamelModel` base — camelCase over
the wire, matching the admin SPA panels (`AppRegistryPanel`, `AuditDrawer`, …).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import AfterValidator, AnyUrl, Field, UrlConstraints

from src.api.v1.pagination import DEFAULT_PAGE_SIZE
from src.db.models.app_registry import MAX_DEPLOYED_URL, ApprovalRoute, AppStatus
from src.schemas import CamelModel


def _fits_the_column(url: AnyUrl) -> AnyUrl:
    """Bound the SERIALIZED url, which is the value that reaches `varchar(MAX_DEPLOYED_URL)`.

    `UrlConstraints(max_length=…)` measures the INPUT string, but pydantic normalizes on parse —
    a path-less `https://…` comes back with a trailing `/`. So a 2083-char path-less URL passed
    the constraint and then `str(recorded_url)` handed 2084 chars to the column: an uncaught
    asyncpg error, i.e. a 500 on mark-deployed where the admin deserves a 422. Re-measuring the
    parse OUTPUT is what makes 0019's "a URL that parses at the schema boundary always fits"
    true by validation rather than by luck.
    """
    if len(str(url)) > MAX_DEPLOYED_URL:
        raise ValueError(f"URL must be at most {MAX_DEPLOYED_URL} characters")
    return url


# The deployed-app address, parsed at the boundary (R5, "parse, don't validate"): a
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
    (a bearer credential is minted only by the dedicated download endpoint, R15)."""

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
    # The submission under review (R16): what the reviewer inspects, and the id
    # approve must echo back (D5).
    submission_id: uuid.UUID | None
    commit_sha: str | None
    submitted_at: datetime | None
    # The approved pin (R4): the artifact the runbook operator deploys — the SHA is
    # their identity check after cloning the downloaded bundle.
    approved_submission_id: uuid.UUID | None
    approved_commit_sha: str | None
    approved_by: uuid.UUID | None
    approved_at: datetime | None
    # The manual-runbook marker (R17, D7): `redeploy_needed` is exact —
    # `approved_submission_id != deployed_submission_id` — so an approved-but-
    # undeployed app and a re-approved-since-deploy app both surface it.
    deployed_at: datetime | None
    # The recorded live address (R5) — read back as a plain string, never re-parsed:
    # a value already in the column was parsed when it was written, and re-validating
    # it here would turn one bad legacy row into a 500 on the whole admin queue.
    deployed_url: str | None
    # ALWAYS false for the self-publish lineage, whatever the pins say (R17a): the flag
    # is a runbook prompt, and a self-published app has no runbook step for anyone to
    # perform. For every other lineage it stays the exact id comparison above.
    redeploy_needed: bool
    # Which lineage the current submission entered through (R17a/P5): `runbook`,
    # `self_publish`, or null (never submitted, or an interim pre-publish-flow row —
    # null keeps today's behaviour everywhere). The admin SPA keys the runbook
    # affordances off this: a `self_publish` row renders neither "Deploy needed" nor
    # "Mark deployed" — and the server refuses the latter regardless.
    approval_route: ApprovalRoute | None
    # What the publish flow attached at submit (R15): both answer sets, the
    # per-question differences, and the citizen's REDACTED explanation — so the review
    # screen can lead with the disagreement without a second call. Shape is
    # deliberately untyped here (the questionnaire is expected to be reworded); null
    # for runbook-lineage and pre-feature rows, and the screen says so rather than
    # rendering blanks. Never contains evidence locations (OD-B).
    declaration: dict[str, Any] | None
    # On-disk size of the project's own database (ADR-0028), or null when it has none —
    # never provisioned, not yet ready, or the cluster was unreachable when the page
    # rendered. STRICTLY ADVISORY: it replaced the retired `dataBytes` counter as an
    # observation, and nothing anywhere reads it as a quota or a gate. Null means "no
    # number to show", never "zero" and never "over limit".
    database_bytes: int | None
    rejection_note: str | None
    created_at: datetime
    updated_at: datetime


class AppListResponse(CamelModel):
    """One page of the registry listing, plus whether it IS the whole set.

    `truncated` exists because the badge and this list are fed by two different queries:
    the count is an uncapped `GROUP BY`, the listing stops at `LISTING_CAP`. Past the cap
    the badge advertised a number the list refused to show, with nothing on the wire
    saying so — and because the pending tab sorts OLDEST FIRST, the rows that vanished
    were the NEWEST submissions. A citizen's app could sit in the queue, be counted, and
    be invisible to every administrator who looked. Pagination stays deferred; making the
    cap VISIBLE is what stops the two surfaces from silently disagreeing."""

    apps: list[AdminAppOut]
    truncated: bool = False


class AppStatusCounts(CamelModel):
    """How many apps sit in each registry status, zero-filled.

    Every status is a REQUIRED field rather than a free `dict[str, int]`: the badge that
    reads this is the only thing telling an administrator a queue has items in it, and a
    silently-absent key would render as "no number" — indistinguishable from zero on a
    screen whose whole job is that distinction. The status vocabulary is closed
    (`AppStatus`), so naming the five costs nothing and buys the client real narrowing.

    Field names are single words, so the camel base is a no-op and the wire keys are the
    `AppStatus` values verbatim.
    """

    draft: int
    pending: int
    approved: int
    rejected: int
    disabled: int


class AppCountsResponse(CamelModel):
    """The badge's whole payload (P1) — counts, and nothing else.

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
    # The submission id the admin ACTUALLY reviewed (D5): the guarded UPDATE adds
    # `AND source_submission_id = :submission_id`, so a re-submit between the
    # admin's review and their click updates zero rows → 409, never a silent
    # promotion of an unreviewed bundle.
    submission_id: uuid.UUID


class BundleUrlResponse(CamelModel):
    """The audited out-of-band review download (R15). `url` is a short-TTL bearer
    credential — the SPA uses it immediately and never stores it; it is likewise
    never written to the audit trail."""

    url: str
    submission_id: uuid.UUID
    commit_sha: str | None
    expires_in_seconds: int


class MarkDeployedRequest(CamelModel):
    """The optional deployed-URL payload. The whole BODY is optional (the endpoint
    shipped without one and the admin SPA already posts `{}`), and so is the field:
    an admin who ran the runbook but has no URL to hand still records the marker.

    OMITTING `deployedUrl` means "leave the recorded URL as it is" — a defined,
    documented meaning (fail-first's optional-knob exception), and the right one: a
    re-deploy of the same app keeps the same address, so a bare re-mark must not
    silently blank out the Live link the owner is already using. Recording a
    *different* URL is just passing the new one."""

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
    container as `BIAL_BLOB_CONTAINER_URL` + `BIAL_BLOB_SAS` (U2/R2). `sas` is a 365-day bearer
    credential: the admin pastes it straight into an ACA secret and it is NEVER logged, NEVER
    written to the audit trail (the audit row carries the expiry, not the token), and never part
    of any list projection. `expiresAt` comes from the app's stored access policy — deleting that
    policy revokes this credential (the runbook's incident-response lever)."""

    container_url: str
    sas: str
    expires_at: datetime


class DatabaseCredentialResponse(CamelModel):
    """The project database's connection string, for the go-live runbook's
    `BIAL_DATABASE_URL` (ADR-0028). `dsn` embeds the app role's password, so it is the same
    kind of object as `DeployCredentialResponse.sas`: returned in this body and nowhere else
    — never logged, never in the audit `detail` (which records `roleName` + `host` instead),
    never in a list projection.

    `dbName` / `roleName` / `host` are the non-secret half, repeated so an operator can
    identify and later reconcile the database without re-reading the credential."""

    dsn: str
    db_name: str
    role_name: str
    host: str


# The rejection note's floor (U13/P3). A rejection is the only thing the citizen gets
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


class RejectRequest(CamelModel):
    # REQUIRED since U13 (P3): "a rejection carries a note back" is the requirement, and an
    # optional field made that a suggestion. Omitting it is a 422 on the missing field, and
    # a too-short or whitespace-only one is a 422 on its content.
    note: RejectionNote


class PatchAppRequest(CamelModel):
    # The app display name is now sourced from the owning project (#48) — not settable here.
    # Only the login-required gate remains admin-patchable.
    login_required: bool | None = None


class PrefixReconcileCounts(CamelModel):
    """One object-store prefix's reconciliation tally (U10, R11/R13). Counts ONLY — never a key
    list, which would leak the internal object layout. `scanned == owned + withinGrace +
    eligible`; `deleted` is 0 on a report-only prefix (`submissions`, `apps`)."""

    scanned: int
    owned: int
    within_grace: int
    eligible: int
    deleted: int


class AttachmentReclaimSummary(CamelModel):
    """The aggregate never-sent-attachment reclaim tally folded into the operator sweep (U9/U10).
    Counts ONLY (R13 posture, like `PrefixReconcileCounts`): rows reclaimed, quota bytes freed, and
    object keys swept — summed across every owning user the pass touched. Never a key, a user id,
    or any list, which would leak the internal layout / the roster."""

    reclaimed: int
    freed_bytes: int
    swept_keys: int


class StorageReconcileResponse(CamelModel):
    """The operator-invoked reconciling sweep's report (U10). For the report-only prefixes the
    report IS the whole product of the endpoint, so it reaches the caller as a typed body rather
    than a log line. `ownerlessSubmissions` names the `submissions/{app_id}/` bundles whose app row
    is gone (past grace) — the set the D7 retention call must rule on. `attachmentReclaim` is the
    U9 never-sent-upload reclaim the sweep now folds in (the quota leak it fixes finally runs in
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
    """The per-project-database half of the orphan sweep (U7, R10). Counts ONLY — never a
    database name, which embeds the owning project's uuid and would turn this report into an
    inventory of who has what (the exact posture `PrefixReconcileCounts` takes on keys).

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
    """The operator-invoked per-project-database sweep's report (U7).

    A SIBLING of `StorageReconcileResponse`, deliberately not an extension of it: that shape
    is frozen around `scanned == owned + withinGrace + eligible`, and a 24h age grace keyed
    off a blob's `last_modified` has no analogue on `pg_database`. The report IS the whole
    product of the endpoint — it deletes nothing.
    """

    databases: DatabaseReconcileCounts
    roles: RoleReconcileCounts


class SandboxReconcileResponse(CamelModel):
    """The operator-invoked sandbox-fleet sweep's report (#83 follow-up).

    A SIBLING of the storage and database reports, and report-only for the same reason: the
    ambiguity between "orphaned" and "provisioned seconds ago, registry not written yet" is not
    something to hand an irreversible ARM delete.

    Counts for the fleet, NAMES only for the gaps. The operator needs the names to act on;
    everything else is a number, because a sandbox name embeds its app's uuid and a full list
    would be an inventory of who is running what. The names travel in the RESPONSE and never in
    the audit row — the same split the storage report makes for blob keys."""

    live: int
    registered: int
    unregistered: list[str]
    registered_missing: list[str]
    # --- R20: is the scheduled worker alive? ------------------------------------------
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
    """What the reclamation pass would do RIGHT NOW, without doing any of it (R20).

    THE QUESTION AN OPERATOR COULD NOT ASK. Before flipping `SANDBOX__RECLAIM_DESTROY` the only
    ways to learn what a pass would delete were to read the worker's logs after the fact, or to
    turn it on and find out. Both answer after the decision. This answers before it, on demand,
    and touches nothing: it runs the same classifier over the same three sources and returns the
    verdicts.

    THE FLAGS COME BACK WITH THE VERDICTS because they change what those verdicts MEAN. The same
    `destroy` list is a preview on a report-only deployment and a description of what is about to
    happen on an armed one, and an operator must not have to go and look up which they are in.

    COUNTS TO THE AUDIT ROW, NAMES ONLY TO THE RESPONSE — the split every sibling admin report
    makes. A sandbox name embeds 28 hex characters of its app's uuid, so a name list in an audit
    log is a durable inventory of who was running what."""

    scanned: int
    spared: int
    staged: int
    destroy: int
    escalate: int
    not_ours: int
    #: The pass REFUSED to judge: too little of the live fleet is claimed by the coordination
    #: store, so the spare-list is not trustworthy enough to sentence anything by. Every verdict
    #: below is meaningless when this is true, which is why it is reported and not hidden.
    store_fault: bool
    #: Destroy candidates AND escalations — everything a human has a decision to make about.
    #: Spared containers are the boring majority and are a count only.
    candidates: list[ReclamationCandidate]
    #: What the flags say right now. `reclaimEnabled` false means the scheduled pass is not even
    #: running; `reclaimDestroy` false means it runs and reports. This endpoint answers regardless
    #: of both — refusing to preview because the feature is off would make the preview useless
    #: exactly when it is most wanted.
    reclaim_enabled: bool
    reclaim_destroy: bool
    #: The same dead-worker signal `reconcile-sandboxes` carries, for the same reason: this
    #: endpoint runs the pass IN THE REQUEST, so a green report here says nothing at all about
    #: whether the scheduled worker is alive.
    last_reclamation_pass_at: datetime | None = None
    reclamation_stale: bool = True


class SandboxTagBackfillResponse(CamelModel):
    """What one C10 identity backfill pass did to the pre-existing fleet (U8).

    THE BUCKETS SUM: `scanned == alreadyTagged + stamped + skippedNoRow + failed`, the
    `reconcile-databases` shape. That is not tidiness — an operator reads this to decide whether
    the fleet is ready for the destroy flag to be flipped, and a report whose numbers do not add up
    cannot support that decision.

    `skippedNoRow` is the one to read carefully, and its name understates it: those containers WERE
    stamped, with `kind` and `backfilled_at` and nothing else, because no app row matches their
    name (a sandbox name keeps only 28 of its app_id's 32 hex characters, so it is not invertible).
    They carry no owner, and they are therefore escalate-forever — reported on every pass,
    destroyed by nothing. A non-zero value here is not an error; it is the count of containers a
    human has to decide about.

    `unowned` IS THAT SAME POPULATION, COUNTED ON EVERY PASS, and it deliberately stands outside
    the sum. The four summing buckets say what this pass DID; `unowned` says what the fleet IS.
    They diverge immediately: a container stamped `kind`-only by pass 1 is `alreadyTagged` in
    pass 2, so `skippedNoRow` drops to zero and `alreadyTagged == scanned` — which is exactly the
    reading an operator takes as "the fleet is clean, flip the destroy flag". `unowned` is the
    number that keeps saying otherwise.

    COUNTS ONLY, unlike its sibling reports, and the asymmetry is deliberate: `reconcile-sandboxes`
    returns names because an operator has to know WHICH container to go and delete, whereas this
    endpoint has already acted on every container it found, so a name list would be an inventory of
    who is running what with nothing to do about it. Failures travel to the logs by name."""

    scanned: int
    already_tagged: int
    stamped: int
    skipped_no_row: int
    failed: int
    unowned: int


class DeployReconcileResponse(CamelModel):
    """The operator-invoked deploy-reconciliation report (U6).

    ONE number, and that is the honest shape rather than a thin one.
    `reconcile_stalled_deployments` returns how many abandoned rows it SETTLED; anything richer
    would have to be assembled from a second read of a table the pass has just changed — a report
    that contradicts itself the moment two reconcilers overlap, which is exactly the window this
    endpoint runs in while the in-process loop is still alive (removed in U7).

    A row ARM could not answer for is deliberately NOT in this count. It is not resolved, it is
    DEFERRED — left exactly as it was for the next pass — because a throttled request that read
    as "gone" would eventually mark a live app failed.

    Counts only, like every sibling report: a deployment id or an app name would turn the
    operator trail into an inventory of who deployed what (`.claude/rules/security.md`).
    """

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
    # Local suspension marker (R10): null = active. Surfaced so the roster shows
    # who is blocked without a per-user read.
    suspended_at: datetime | None
    # Today's folded BUILD token spend (all four classes, IST day) — the figure the
    # daily cap actually measures, via the same shared expression the gate reads.
    # One page-wide aggregate feeds this, never a per-row query (R9).
    usage_today: int
    # Today's pre-publish-review spend (U15), as its OWN figure: metered against the
    # citizen for attribution, never part of what the cap measures, and never folded
    # into `usage_today` — one number that means two things is how the ledger went
    # wrong before.
    review_usage_today: int
    limits: LimitFields
    effective_limits: LimitFields


class UsersResponse(CamelModel):
    """The roster page. Keyset envelope fields (KD-1) are additive next to the
    original `{defaults, users}` shape — a called-out SPA contract change (U9)."""

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
    """One counter's total, and when it was last seen (U25, R32)."""

    name: str
    total: int
    occurrences: int
    last_seen_at: datetime | None


class HarnessCountersResponse(CamelModel):
    """`GET /v1/admin/harness-counters` → 200 — the build-harness outcomes, totalled.

    THE QUESTION THIS ANSWERS, in the words the plan's success criteria use: did the verdict block
    a false claim, how often did we restore, and did any turn fail to reach a durable copy. After a
    week in production those are answerable from this one response.

    NO METRICS DEPENDENCY, deliberately. There is no metrics system in this deployment (this
    module says so elsewhere at length), so the shape is a `GROUP BY` over a small append-only
    table — the same trade `worker_passes` already makes.

    Rows are whatever names have been WRITTEN, not the enum's members: the vocabulary is open by
    design, and a counter the companion plan adds at the tool boundary shows up here with no
    change to this file."""

    counters: list[HarnessCounterRow]
    since: datetime


# --- deleted projects (`/admin/deleted-projects/*`) -----------------------------


class DeletedProjectsQuery(CamelModel):
    """The search body for `POST /admin/deleted-projects/search`.

    A BODY RATHER THAN QUERY PARAMS, and the method is the point rather than the ergonomics.
    The route commits an audit row — its own comment says "A READ THAT WRITES" — but
    `main.py`'s `refuse_cross_origin_writes` only guards POST/PUT/PATCH/DELETE, precisely
    because a GET is not supposed to mutate. As a GET this route sat outside that guard with
    a `SameSite=Lax` session cookie and generated apps served same-site, so app code written
    by a model from a citizen's prompt could drive an admin's session into writing audit rows
    under their identity. POST puts it back inside the guard, picks up `RequireCsrf`'s
    double-submit token, and forces a preflight a same-site app cannot satisfy.

    It also takes the search term out of the URL, and so out of uvicorn's `access_log` and
    the gateway's `requestUri` — two audiences wider than `superadmin_emails`, holding a
    citizen's words for a retention this repo does not control.

    EVERY FIELD IS A BARE TYPE, deliberately, and this is load-bearing rather than lazy.
    `pagination.py` argues that this platform answers a bad page argument with one
    `{"error": {"message"}}` 422; a Pydantic constraint (`ge=`, `le=`) or a `datetime`
    coercion failure emits FastAPI's native `{"detail": [...]}` instead, putting two
    different 422 bodies on one endpoint that `error_responses(...)` cannot both document.
    So the validating stays in the handler, on `parse_cursor` / `clean_limit` /
    `clean_search` / `parse_deleted_at_bound`, exactly as it did when these were query
    params.
    """

    cursor: str | None = None
    limit: int = DEFAULT_PAGE_SIZE
    q: str | None = None
    # The date range #176 asked for ("filters worth having: by owner, and by date range").
    # ISO-8601 strings, parsed in the handler for the reason the class docstring gives — a
    # `datetime` annotation here would hand a malformed date to Pydantic and produce the
    # wrong 422 shape. Inclusive on both ends: an administrator asking for a day means that
    # whole day, and a half-open upper bound silently drops the last row of it.
    deleted_from: str | None = None
    deleted_to: str | None = None


class DeletedProjectOut(CamelModel):
    """One deletion, as an administrator reads it (#176).

    Every field is a VALUE copied at the moment of deletion, not a join: the project row, its
    app, its database and its chats are all gone by the time this row is written, so there is
    nothing left to join to. That is why the tombstone stores rather than references.

    `deletedBy` AND `deletedByName` ARE NOT THE SAME KIND OF FACT, and this screen is exactly
    where the difference matters. `deletedBy` is the account that acted, taken from the
    authenticated session and never from a request body. `deletedByName` is the readable label
    for it — also stamped server-side, from `display_name` or the email when Entra gave us
    none — so the two cannot disagree. It was briefly a client-supplied field, which meant a
    browser signed in as one person could file a deletion under somebody else's name; that is
    the question this row exists to answer, so it is now unspoofable by construction.

    The three counts are what went with the project. They cannot be reconstructed once the
    children are deleted, which is why they are captured at deletion time rather than derived.
    """

    id: uuid.UUID
    project_id: uuid.UUID
    project_name: str
    owner_id: uuid.UUID
    owner_email: str
    deleted_by: uuid.UUID
    deleted_by_name: str
    deleted_at: datetime
    remark: str
    chats_deleted: int
    had_app: bool
    had_database: bool


class DeletedProjectsResponse(CamelModel):
    """A keyset page of deletions, newest first.

    KEYSET, NOT THE OFFSET ENVELOPE the projects list uses — and the distinction is not
    arbitrary. `/v1/projects` deviated to offset because §2 specifies `Showing 1-8 of 12` and
    `Page 1 of 2`, neither of which is expressible without a `total`. Nothing here asks for
    one, and the two reasons `pagination.py` prefers keyset both hold: the table is
    APPEND-ONLY (no row is ever updated or deleted, so a page walk cannot skew) and the walk
    is over a UUIDv7 primary key, so `ORDER BY id DESC` already IS newest-first and the cursor
    is just the last row's id.
    """

    deletions: list[DeletedProjectOut]
    next_cursor: str | None
    has_more: bool
