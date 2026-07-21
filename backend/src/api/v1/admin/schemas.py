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

from src.db.models.app_registry import MAX_DEPLOYED_URL, AppStatus
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
    redeploy_needed: bool
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
    apps: list[AdminAppOut]


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


class RejectRequest(CamelModel):
    # Bounded at the boundary: an over-long note used to be sliced to 1000 chars in the handler,
    # so the admin's reasoning was silently truncated and they never learned it happened.
    note: str | None = Field(default=None, max_length=1000)


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
    # Today's folded token spend (all four classes, IST day) — one page-wide
    # aggregate feeds this, never a per-row query (R9).
    usage_today: int
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


class LimitsPatchResponse(CamelModel):
    user_id: uuid.UUID
    limits: LimitFields
    effective_limits: LimitFields


class FeedbackItem(CamelModel):
    user_id: uuid.UUID
    email: str
    message: str
    page: str
    created_at: datetime


class FeedbackResponse(CamelModel):
    feedback: list[FeedbackItem]
    total: int
