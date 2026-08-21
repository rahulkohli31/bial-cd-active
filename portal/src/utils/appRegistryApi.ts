/**
 * App Registry data access — ADMIN-side thin wrappers over the registry endpoints
 * (/api/admin/apps/*, admin-gated server-side), all via authFetch (Bearer +
 * refresh-and-retry): list / approve / reject / patch / disable / enable /
 * mark-deployed / delete / audit.
 * Each throws an Error with a user-ready message on failure.
 *
 * The OWNER group (provision/submit/status/source) is RETIRED: the open-sandbox
 * submit flow lives in the typed `approvalApi.ts` (no client compile, no
 * client-supplied artifact), and provisioning happens inside the build session.
 */
import { authFetch } from './api'
import type { AuthFetchDeps } from './api'
import { ApiError, isRecord, readApiError } from './apiError'
import type { ApprovalRoute, AppStatus } from './projectApi'

/**
 * Read a successful response body as untrusted `unknown`, or throw an ApiError carrying
 * `.status` and `.code` so callers branch on 404 / 409 / `submission_withdrawn` instead
 * of string-matching a message.
 *
 * This USED to be `asJson<T>`, which parsed to `unknown` and handed the body straight
 * back typed as whatever `T` the call site named — an assertion by the caller, not
 * validation. The admin review screen now makes real decisions off these fields (which
 * lineage an app is on, whether a declaration exists, what is in dispute), so a server
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

const jsonOpts = (method: string, body?: unknown) => ({
  method,
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body || {}),
})

// ── Admin ──────────────────────────────────────────────────────────────────

/** The registry vocabularies, re-exported from the one module that declares them
 * (`projectApi`) so the admin screen keeps importing them from its own client. `draft` is
 * builder-side and never shown in AppRegistryPanel's admin tabs, but is a real value the
 * status field can hold. */
export type { ApprovalRoute, AppStatus }

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

/**
 * Which lineage the current submission entered through (R17a/P5). `null` is a real
 * value — never submitted, or a row that predates the publish flow — and the screen
 * keys the runbook affordances off it, so an unrecognised string must land as `null`
 * (the conservative reading: show the runbook controls, which the server refuses anyway)
 * rather than be waved through as a lineage nobody defined.
 */
function asApprovalRoute(value: unknown): ApprovalRoute | null {
  return value === 'runbook' || value === 'self_publish' ? value : null
}

/**
 * The submitted data-classification declaration (R15), exactly as the publish gate wrote
 * it. Left as `unknown` on purpose: the questionnaire is expected to be reworded, the
 * document is stored data rather than a wire schema, and U10's drift block is landing in
 * it in parallel — so the SCREEN narrows the parts it renders, defensively, and an
 * unrecognised addition renders as nothing instead of failing the whole admin queue.
 * `null` means no declaration at all: a runbook-lineage row, or one queued before this
 * feature shipped. Never contains evidence locations (OD-B).
 */
export type SubmittedDeclaration = Record<string, unknown>

/** Mirrors the backend's `AdminAppOut` (`backend/src/api/v1/admin/schemas.py`,
 * `CamelModel`-based — camelCase on the wire, same base as the feedback/user
 * schemas). Every field from the real schema is included even though
 * AppRegistryPanel.jsx doesn't read all of them today (ownerId,
 * hasApprovedSnapshot, approvedSubmissionId, approvedCommitSha, approvedBy,
 * approvedAt, deployedAt, rejectionNote, createdAt, updatedAt) — this is the
 * real wire contract, not a guess at what's consumed. Datetimes serialize as
 * ISO strings on the wire. */
export interface RegistryApp {
  appId: string
  name: string
  ownerId: string
  ownerUsername: string | null
  status: AppStatus
  loginRequired: boolean
  hasApprovedSnapshot: boolean
  submissionId: string | null
  commitSha: string | null
  submittedAt: string | null
  approvedSubmissionId: string | null
  approvedCommitSha: string | null
  approvedBy: string | null
  approvedAt: string | null
  deployedAt: string | null
  deployedUrl: string | null
  redeployNeeded: boolean
  /** `runbook` | `self_publish` | null — the review screen's runbook-affordance switch (R17a). */
  approvalRoute: ApprovalRoute | null
  /** What the publish flow attached at submit (R15), or null when nothing did. */
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
 * the boundary (`.claude/rules/fail-first.md`). Every other field has a defined absent
 * meaning and takes it: a missing lineage IS null, a missing declaration IS null, an
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
    loginRequired: value.loginRequired === true,
    hasApprovedSnapshot: value.hasApprovedSnapshot === true,
    submissionId: asStringOrNull(value.submissionId),
    commitSha: asStringOrNull(value.commitSha),
    submittedAt: asStringOrNull(value.submittedAt),
    approvedSubmissionId: asStringOrNull(value.approvedSubmissionId),
    approvedCommitSha: asStringOrNull(value.approvedCommitSha),
    approvedBy: asStringOrNull(value.approvedBy),
    approvedAt: asStringOrNull(value.approvedAt),
    deployedAt: asStringOrNull(value.deployedAt),
    deployedUrl: asStringOrNull(value.deployedUrl),
    redeployNeeded: value.redeployNeeded === true,
    approvalRoute: asApprovalRoute(value.approvalRoute),
    declaration: isRecord(value.declaration) ? value.declaration : null,
    // Null is a REAL value here — "no number to show" — and must survive as null rather
    // than becoming 0, which would render as an empty database (see `fmtBytes`).
    databaseBytes: typeof value.databaseBytes === 'number' ? value.databaseBytes : null,
    rejectionNote: asStringOrNull(value.rejectionNote),
    createdAt: asString(value.createdAt),
    updatedAt: asString(value.updatedAt),
  }
}

/** List registry apps, optionally filtered by status. */
export async function listApps(status?: string, deps: AuthFetchDeps = {}): Promise<RegistryApp[]> {
  const q = status ? `?status=${encodeURIComponent(status)}` : ''
  const body = await readBody(await authFetch(`/api/admin/apps${q}`, {}, deps), 'Failed to load apps')
  const apps = isRecord(body) ? body.apps : null
  return Array.isArray(apps) ? apps.map(toRegistryApp) : []
}

/** How many apps sit in each registry status — the waiting-count badge's source (P1). */
export type AppStatusCounts = Record<AppStatus, number>

/**
 * The per-status counts (P1). A dedicated route, NOT a `listApps(...).length`: the
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

/** Approve a pending app, pinning EXACTLY the reviewed submission (D5): the server
 * refuses (409) when the app was re-submitted since the admin reviewed it. */
export async function approveApp(appId: string, submissionId: string, deps: AuthFetchDeps = {}): Promise<unknown> {
  return readBody(
    await authFetch(`/api/admin/apps/${encodeURIComponent(appId)}/approve`, jsonOpts('POST', { submissionId }), deps),
    'Failed to approve',
  )
}

/** Record that the go-live runbook was run for the approved submission, optionally
 * recording WHERE the app now lives (R5) — the URL the owner's Live link points at.
 * Omitting `deployedUrl` keeps whatever address is already recorded (the server treats
 * an absent field as "leave it alone"), which is the routine re-deploy case. The https
 * check lives server-side: a bad URL comes back as a 422 whose message the caller shows. */
export async function markDeployed(appId: string, deployedUrl?: string, deps: AuthFetchDeps = {}): Promise<unknown> {
  return readBody(
    await authFetch(
      `/api/admin/apps/${encodeURIComponent(appId)}/mark-deployed`,
      jsonOpts('POST', deployedUrl ? { deployedUrl } : {}),
      deps,
    ),
    'Failed to mark deployed',
  )
}

/** Reject a pending app. The note is REQUIRED since U13 (P3) — a rejection is the only
 *  thing that travels back to the developer, and an empty one reached them as a bare red
 *  badge. Length is enforced server-side (422 below 20 characters or above 1000); the UI
 *  disables the action rather than letting an admin discover the floor by hitting it. */
export async function rejectApp(appId: string, note: string, deps: AuthFetchDeps = {}): Promise<unknown> {
  return readBody(await authFetch(`/api/admin/apps/${encodeURIComponent(appId)}/reject`, jsonOpts('POST', { note }), deps), 'Failed to reject')
}

/** Patch the loginRequired gate (audited server-side). The app name is project-sourced (#48). */
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

/** Hard-delete an app (audited; blobs swept, registry row and app database removed). */
export async function deleteApp(appId: string, deps: AuthFetchDeps = {}): Promise<unknown> {
  return readBody(await authFetch(`/api/admin/apps/${encodeURIComponent(appId)}`, { method: 'DELETE' }, deps), 'Failed to delete app')
}

/** Mirrors the backend's `AuditEventOut` (`backend/src/api/v1/admin/schemas.py`,
 * `CamelModel`-based). `resourceType` and `detail` are part of the real row but
 * have no reader in AppRegistryPanel.jsx today. */
export interface AuditEvent {
  id: string
  actorId: string | null
  username: string | null
  action: string
  resourceType: string
  resourceId: string | null
  detail: Record<string, unknown> | null
  count: number | null
  createdAt: string
}

/** Narrow one untrusted audit row. Unlike an app row there is no id the UI acts on —
 *  the drawer only renders — so an unreadable row degrades field-by-field rather than
 *  failing the whole trail. `id` still has to be something: it is the React key. */
function toAuditEvent(value: unknown): AuditEvent {
  const row = isRecord(value) ? value : {}
  return {
    id: typeof row.id === 'string' && row.id !== '' ? row.id : `unreadable-${asString(row.action)}`,
    actorId: asStringOrNull(row.actorId),
    username: asStringOrNull(row.username),
    action: asString(row.action),
    resourceType: asString(row.resourceType),
    resourceId: asStringOrNull(row.resourceId),
    detail: isRecord(row.detail) ? row.detail : null,
    count: typeof row.count === 'number' ? row.count : null,
    createdAt: asString(row.createdAt),
  }
}

/** The app's audit trail (data mutations + admin actions), newest-first. */
export async function fetchAudit(appId: string, deps: AuthFetchDeps = {}): Promise<AuditEvent[]> {
  const body = await readBody(await authFetch(`/api/admin/apps/${encodeURIComponent(appId)}/audit`, {}, deps), 'Failed to load audit')
  const events = isRecord(body) ? body.events : null
  return Array.isArray(events) ? events.map(toAuditEvent) : []
}
