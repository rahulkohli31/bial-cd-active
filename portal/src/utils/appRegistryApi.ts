/**
 * App Registry data access — ADMIN-side thin wrappers over the registry endpoints
 * (/api/admin/apps/*, admin-gated server-side), all via authFetch (Bearer +
 * refresh-and-retry): list / approve / reject / patch / disable / enable /
 * delete / history.
 * Each throws an Error with a user-ready message on failure.
 *
 * The OWNER group (provision/submit/status/source) is RETIRED: the open-sandbox
 * submit flow lives in the typed `approvalApi.ts` (no client compile, no
 * client-supplied artifact), and provisioning happens inside the build session.
 */
import { authFetch } from './api'
import type { AuthFetchDeps } from './api'
import { ApiError, isRecord, optionalNumber, readApiError } from './apiError'
import type { AppStatus } from './projectApi'

/**
 * Read a successful response body as untrusted `unknown`, or throw an ApiError carrying
 * `.status` and `.code` so callers branch on 404 / 409 / `submission_withdrawn` instead
 * of string-matching a message.
 *
 * This USED to be `asJson<T>`, which parsed to `unknown` and handed the body straight
 * back typed as whatever `T` the call site named — an assertion by the caller, not
 * validation. The admin review screen now makes real decisions off these fields (whether
 * a declaration exists, what is in dispute), so a server
 * shape that drifts must fail at this boundary rather than surface as a blank dispute
 * row an administrator would read as "nothing was flagged". The narrowing below follows
 * `projectApi.ts`'s `toProject` / `toProjectsPage` pattern, which was already the
 * in-repo answer: `isRecord` plus per-field guards, throwing or defaulting per field.
 */
async function readBody(res: Response, fallback: string): Promise<unknown> {
  if (!res.ok) throw await readApiError(res, fallback)
  return res.json()
}

function asString(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

function asStringOrNull(value: unknown): string | null {
  return typeof value === 'string' ? value : null
}

/** A count is a count: anything that is not a finite number is 0, never NaN in a badge. */
function asCount(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) ? Math.max(0, value) : 0
}

function oneOf<T extends string>(allowed: readonly T[], value: unknown, fallback: T): T {
  return allowed.find((member) => member === value) ?? fallback
}

const jsonOpts = (method: string, body?: unknown) => ({
  method,
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body || {}),
})

// ── Admin ──────────────────────────────────────────────────────────────────

/** The registry status vocabulary, re-exported from the one module that declares it
 * (`projectApi`) so the admin screen keeps importing it from its own client. */
export type { AppStatus }

export const APP_STATUSES: readonly AppStatus[] = [
  'draft',
  'pending',
  'approved',
  'rejected',
  'disabled',
]

function isAppStatus(value: unknown): value is AppStatus {
  return APP_STATUSES.some((status) => status === value)
}

/** The App Registry's status column, as the backend's `RegistryStatus` sends it. */
export type RegistryStatus =
  | 'draft'
  | 'waiting_for_review'
  | 'rejected'
  | 'not_published'
  | 'publishing'
  | 'live'
  | 'publish_failed'
  | 'taken_offline'
  | 'disabled'

const REGISTRY_STATUSES: readonly RegistryStatus[] = [
  'draft',
  'waiting_for_review',
  'rejected',
  'not_published',
  'publishing',
  'live',
  'publish_failed',
  'taken_offline',
  'disabled',
]

/** The version serving now: the number of the send that put it there, its commit, and when
 *  that send first went live. Any of them is null when nothing recorded it. */
export interface LiveVersion {
  number: number | null
  commitSha: string | null
  since: string | null
}

function toLiveVersion(value: unknown): LiveVersion | null {
  return isRecord(value)
    ? {
        number: optionalNumber(value.number),
        commitSha: asStringOrNull(value.commitSha),
        since: asStringOrNull(value.since),
      }
    : null
}

/**
 * The submitted data-classification declaration, exactly as the publish gate wrote
 * it. Left as `unknown` on purpose: the questionnaire is expected to be reworded, the
 * document is stored data rather than a wire schema, and the drift block is landing in
 * it in parallel — so the SCREEN narrows the parts it renders, defensively, and an
 * unrecognised addition renders as nothing instead of failing the whole admin queue.
 * `null` means no declaration at all: a row queued without one. Never contains evidence
 * locations.
 */
export type SubmittedDeclaration = Record<string, unknown>

/** Mirrors the backend's `AdminAppOut` (`backend/src/api/v1/admin/schemas.py`,
 * `CamelModel`-based — camelCase on the wire, same base as the feedback/user
 * schemas). Every field from the real schema is included even though
 * AppRegistryPanel.jsx doesn't read all of them today (ownerId,
 * hasApprovedSnapshot, approvedSubmissionId, approvedCommitSha, approvedBy,
 * approvedAt, rejectionNote, createdAt, updatedAt) — this is the
 * real wire contract, not a guess at what's consumed. Datetimes serialize as
 * ISO strings on the wire. */
export interface RegistryApp {
  appId: string
  name: string
  ownerId: string
  ownerUsername: string | null
  status: AppStatus
  registryStatus: RegistryStatus
  /** Null when nothing is serving. */
  liveVersion: LiveVersion | null
  loginRequired: boolean
  hasApprovedSnapshot: boolean
  submissionId: string | null
  commitSha: string | null
  submittedAt: string | null
  approvedSubmissionId: string | null
  approvedCommitSha: string | null
  approvedBy: string | null
  approvedAt: string | null
  /** What the publish flow attached at submit, or null when nothing did. */
  declaration: SubmittedDeclaration | null
  databaseBytes: number | null
  rejectionNote: string | null
  createdAt: string
  updatedAt: string
}

/**
 * Narrow one untrusted admin row.
 *
 * An app with no `appId` is not an app — every action on the row targets that id, so a
 * coerced `''` would produce controls that POST to `/api/admin/apps//approve`. Fail at
 * the boundary. Every other field has a defined absent
 * meaning and takes it: a missing declaration IS null, an
 * unreadable status falls back to `draft`, which shows no approve/reject controls at all
 * — the fail-closed direction for a row we could not read.
 */
function toRegistryApp(value: unknown): RegistryApp {
  if (!isRecord(value) || typeof value.appId !== 'string' || value.appId === '') {
    throw new ApiError('The server returned an app we could not read.', 500)
  }
  return {
    appId: value.appId,
    name: asString(value.name),
    ownerId: asString(value.ownerId),
    ownerUsername: asStringOrNull(value.ownerUsername),
    status: isAppStatus(value.status) ? value.status : 'draft',
    registryStatus: oneOf(REGISTRY_STATUSES, value.registryStatus, 'draft'),
    liveVersion: toLiveVersion(value.liveVersion),
    loginRequired: value.loginRequired === true,
    hasApprovedSnapshot: value.hasApprovedSnapshot === true,
    submissionId: asStringOrNull(value.submissionId),
    commitSha: asStringOrNull(value.commitSha),
    submittedAt: asStringOrNull(value.submittedAt),
    approvedSubmissionId: asStringOrNull(value.approvedSubmissionId),
    approvedCommitSha: asStringOrNull(value.approvedCommitSha),
    approvedBy: asStringOrNull(value.approvedBy),
    approvedAt: asStringOrNull(value.approvedAt),
    declaration: isRecord(value.declaration) ? value.declaration : null,
    // Null is a REAL value here — "no number to show" — and must survive as null rather
    // than becoming 0, which would render as an empty database (see `fmtBytes`).
    databaseBytes: typeof value.databaseBytes === 'number' ? value.databaseBytes : null,
    rejectionNote: asStringOrNull(value.rejectionNote),
    createdAt: asString(value.createdAt),
    updatedAt: asString(value.updatedAt),
  }
}

export interface RegistryList {
  apps: RegistryApp[]
  /** The server stopped at its cap, so these are not every app. */
  truncated: boolean
}

/** Every registry app up to the server's cap, most recently active first. */
export async function listApps(deps: AuthFetchDeps = {}): Promise<RegistryList> {
  const body = await readBody(await authFetch('/api/admin/apps', {}, deps), 'Failed to load apps')
  const apps = isRecord(body) ? body.apps : null
  return {
    apps: Array.isArray(apps) ? apps.map(toRegistryApp) : [],
    truncated: isRecord(body) && body.truncated === true,
  }
}

/** How many apps sit in each registry status — the waiting-count badge's source. */
export type AppStatusCounts = Record<AppStatus, number>

/**
 * The per-status counts. A dedicated route, NOT a `listApps(...).length`: the
 * listing projects up to 200 rows and probes the app-database cluster for its size
 * column, so polling it for one number would pay both costs and pay more of the first as
 * the queue grows. Superadmin-only server-side — callers must not request it for anyone
 * else (a 403 in the console is not a feature).
 */
export async function fetchAppStatusCounts(deps: AuthFetchDeps = {}): Promise<AppStatusCounts> {
  const body = await readBody(await authFetch('/api/admin/apps/counts', {}, deps), 'Failed to load the review queue count')
  const counts = isRecord(body) && isRecord(body.counts) ? body.counts : {}
  return {
    draft: asCount(counts.draft),
    pending: asCount(counts.pending),
    approved: asCount(counts.approved),
    rejected: asCount(counts.rejected),
    disabled: asCount(counts.disabled),
  }
}

/** The same-tab nudge the nav's waiting count listens for. An event on `window` rather than a
 *  store: there is one fact to share ("read the count again"), and the read already exists. */
export const REVIEW_QUEUE_CHANGED = 'bial:review-queue-changed'

/** Something in this tab changed the review queue. Focus and visibility never fire for an
 *  administrator who stays on the tab they acted in, so this is what reaches the nav there. */
export function announceReviewQueueChanged(): void {
  window.dispatchEvent(new Event(REVIEW_QUEUE_CHANGED))
}

/** Approve a pending app, pinning EXACTLY the reviewed submission: the server
 * refuses (409) when the app was re-submitted since the admin reviewed it. */
export async function approveApp(appId: string, submissionId: string, deps: AuthFetchDeps = {}): Promise<unknown> {
  return readBody(
    await authFetch(`/api/admin/apps/${encodeURIComponent(appId)}/approve`, jsonOpts('POST', { submissionId }), deps),
    'Failed to approve',
  )
}

/** Reject a pending app. The note is REQUIRED — a rejection is the only
 *  thing that travels back to the developer, and an empty one reached them as a bare red
 *  badge. Length is enforced server-side (422 below 20 characters or above 1000); the UI
 *  disables the action rather than letting an admin discover the floor by hitting it. */
export async function rejectApp(appId: string, note: string, deps: AuthFetchDeps = {}): Promise<unknown> {
  return readBody(await authFetch(`/api/admin/apps/${encodeURIComponent(appId)}/reject`, jsonOpts('POST', { note }), deps), 'Failed to reject')
}

/** Patch the loginRequired gate (audited server-side). The app name is project-sourced. */
export async function patchApp(appId: string, patch: Record<string, unknown>, deps: AuthFetchDeps = {}): Promise<unknown> {
  return readBody(await authFetch(`/api/admin/apps/${encodeURIComponent(appId)}`, jsonOpts('PATCH', patch), deps), 'Failed to update app')
}

/** Disable (kill-switch) an approved app. */
export async function disableApp(appId: string, deps: AuthFetchDeps = {}): Promise<unknown> {
  return readBody(await authFetch(`/api/admin/apps/${encodeURIComponent(appId)}/disable`, jsonOpts('POST'), deps), 'Failed to disable')
}

/** Re-enable a disabled app. */
export async function enableApp(appId: string, deps: AuthFetchDeps = {}): Promise<unknown> {
  return readBody(await authFetch(`/api/admin/apps/${encodeURIComponent(appId)}/enable`, jsonOpts('POST'), deps), 'Failed to enable')
}

/**
 * Hard-delete an app, with the administrator's justification.
 *
 * THE REASON IS REQUIRED BY THE ROUTE — word-bounded, validated server-side — because
 * an administrator destroying somebody else's work with no undo and no export should have to
 * say why, and the `window.confirm` this used to go through could not collect it. It rides the
 * `app:delete` audit row, which is written before destruction and has no foreign key to the
 * app, so it outlives what it describes.
 *
 * A BODY ON A DELETE, mirroring `deleteProject`. RFC 9110 leaves content on DELETE undefined,
 * but nginx and the container ingress both forward it and the admin SPA is the only client.
 * Sending none is a 422, which is what this function did before the body existed.
 */
export async function deleteApp(appId: string, reason: string, deps: AuthFetchDeps = {}): Promise<unknown> {
  return readBody(
    await authFetch(
      `/api/admin/apps/${encodeURIComponent(appId)}`,
      { method: 'DELETE', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ reason }) },
      deps,
    ),
    'Failed to delete app',
  )
}

/** What was decided about one sent version, as the backend's `DecisionKind` sends it. */
export type DecisionKind = 'waiting' | 'published' | 'approved' | 'rejected' | 'withdrawn' | 'not_recorded'

const DECISION_KINDS: readonly DecisionKind[] = ['waiting', 'published', 'approved', 'rejected', 'withdrawn', 'not_recorded']

/** Where one sent version stands now, as the backend's `VersionState` sends it. */
export type VersionState =
  | 'waiting'
  | 'live'
  | 'replaced'
  | 'taken_offline'
  | 'publishing'
  | 'publish_failed'
  | 'not_published'
  | 'rejected'
  | 'withdrawn'
  | 'not_recorded'

const VERSION_STATES: readonly VersionState[] = [
  'waiting',
  'live',
  'replaced',
  'taken_offline',
  'publishing',
  'publish_failed',
  'not_published',
  'rejected',
  'withdrawn',
  'not_recorded',
]

export type AttemptStatus = 'running' | 'succeeded' | 'failed'

const ATTEMPT_STATUSES: readonly AttemptStatus[] = ['running', 'succeeded', 'failed']

export interface HistoryAttempt {
  status: AttemptStatus
  startedAt: string
  finishedAt: string | null
  failureCode: string | null
}

export interface HistoryDecision {
  kind: DecisionKind
  by: string | null
  at: string | null
  note: string | null
}

/** One send for publishing, numbered in the order it was sent. */
export interface HistoryVersion {
  kind: 'version'
  number: number
  commitSha: string | null
  submissionId: string | null
  sentAt: string
  sentBy: string | null
  /** The declaration stored with this send's decision, or null when none was. */
  declaration: SubmittedDeclaration | null
  decision: HistoryDecision
  attempts: HistoryAttempt[]
  state: VersionState
  publishedAt: string | null
  replacedBy: number | null
  replacedAt: string | null
}

/** Something done to the app outside any send; a disable carries the enable that ended it. */
export interface HistoryEvent {
  kind: 'event'
  action: string
  at: string
  by: string | null
  reenabledAt: string | null
}

export type HistoryEntry = HistoryVersion | HistoryEvent

export interface AppHistory {
  /** Newest first. */
  entries: HistoryEntry[]
  live: LiveVersion | null
  liveUrl: string | null
  /** The server read only its newest records, so the oldest entries may be missing. */
  truncated: boolean
}

function toAttempt(value: unknown): HistoryAttempt | null {
  if (!isRecord(value)) return null
  const status = ATTEMPT_STATUSES.find((member) => member === value.status)
  if (status === undefined || typeof value.startedAt !== 'string') return null
  return {
    status,
    startedAt: value.startedAt,
    finishedAt: asStringOrNull(value.finishedAt),
    failureCode: asStringOrNull(value.failureCode),
  }
}

function toDecision(value: unknown): HistoryDecision {
  const row = isRecord(value) ? value : {}
  return {
    kind: oneOf(DECISION_KINDS, row.kind, 'not_recorded'),
    by: asStringOrNull(row.by),
    at: asStringOrNull(row.at),
    note: asStringOrNull(row.note),
  }
}

/** Narrow one untrusted History entry. An entry this client cannot place in time or number is
 *  dropped rather than drawn as a blank card; any other unreadable field takes its absent meaning. */
function toHistoryEntry(value: unknown): HistoryEntry | null {
  if (!isRecord(value)) return null
  if (value.kind === 'event') {
    if (typeof value.at !== 'string') return null
    return {
      kind: 'event',
      action: asString(value.action),
      at: value.at,
      by: asStringOrNull(value.by),
      reenabledAt: asStringOrNull(value.reenabledAt),
    }
  }
  if (value.kind !== 'version' || typeof value.number !== 'number' || typeof value.sentAt !== 'string') return null
  return {
    kind: 'version',
    number: value.number,
    commitSha: asStringOrNull(value.commitSha),
    submissionId: asStringOrNull(value.submissionId),
    sentAt: value.sentAt,
    sentBy: asStringOrNull(value.sentBy),
    declaration: isRecord(value.declaration) ? value.declaration : null,
    decision: toDecision(value.decision),
    attempts: Array.isArray(value.attempts)
      ? value.attempts.map(toAttempt).filter((a): a is HistoryAttempt => a !== null)
      : [],
    state: oneOf(VERSION_STATES, value.state, 'not_recorded'),
    publishedAt: asStringOrNull(value.publishedAt),
    replacedBy: optionalNumber(value.replacedBy),
    replacedAt: asStringOrNull(value.replacedAt),
  }
}

/** The app's History: every version its owner sent for publishing, and the app's other events
 *  between them, newest first. */
export async function fetchHistory(appId: string, deps: AuthFetchDeps = {}): Promise<AppHistory> {
  const body = await readBody(
    await authFetch(`/api/admin/apps/${encodeURIComponent(appId)}/history`, {}, deps),
    'Failed to load the history',
  )
  const row = isRecord(body) ? body : {}
  return {
    entries: Array.isArray(row.entries)
      ? row.entries.map(toHistoryEntry).filter((e): e is HistoryEntry => e !== null)
      : [],
    live: toLiveVersion(row.live),
    liveUrl: asStringOrNull(row.liveUrl),
    truncated: row.truncated === true,
  }
}
