/**
 * Typed client for the owner-facing approval flow (`/api/apps/:appId/{withdraw,status}`),
 * mirroring `projectApi.ts`: every call is `fn(args, deps = {})` forwarding `deps` to
 * `authFetch`, responses arrive as `unknown` and pass through a narrower that throws
 * `ApiError` on a structurally-invalid row — never cast, never `any`.
 *
 * THERE IS NO `submitForReview` HERE ANY MORE, and its absence is the point (R15a: one
 * route into the queue). The citizen-callable submit route was retired backend-side in
 * U8 — it was the only writer of the pending status that attached NO declaration, so a
 * queue item could arrive with nothing for an administrator to read. An app now enters
 * the queue exactly one way: the publish request itself routes it (`deployApi.startDeploy`
 * → `outcome: "routed_for_review"`). Re-adding a submit verb here would rebuild the second
 * differently-worded way in that requirement forbids; `approvalApi.test.ts` guards its
 * absence rather than having deleted the coverage.
 *
 * `withdraw` is the escape hatch that replaced re-submitting (P6): an owner pulls their
 * own PENDING submission back to draft. It sends NO body — the server knows which
 * submission is pending — and its 409 carries self-describing server copy, so the control
 * renders `err.message` directly without client-side string matching.
 */
import { ApiError, isRecord, readApiError } from './apiError'
import { authFetch } from './api'
import type { AppStatus, AuthFetchDeps } from './projectApi'

/** The owner's view of the app's approval lifecycle (GET /apps/:id/status). */
export interface AppApprovalStatus {
  appId: string
  status: AppStatus
  rejectionNote: string | null
  /** The submission under review — null until the first submit. */
  submissionId: string | null
  commitSha: string | null
  submittedAt: string | null
  /** The manual-runbook deploy marker — null until an admin marks the app deployed. */
  deployedAt: string | null
  /**
   * Where the app is live. Null both before any deploy AND when the admin recorded a
   * deploy without an address, so the Live link is gated on THIS — never on
   * `deployedAt` or `status === 'approved'`.
   */
  deployedUrl: string | null
}

/** What a successful withdrawal left behind (POST /apps/:id/withdraw): the app, back at
 *  draft. The queue item is REMOVED, not replaced — an administrator mid-review sees it
 *  disappear rather than change underneath them. */
export interface WithdrawResult {
  appId: string
  status: AppStatus
}

// NOTE: stricter than projectApi.ts's same-role helper — this one collapses '' to
// null. The name makes that difference visible at the call sites (a reader who knows
// projectApi's `asStringOrNull` won't mistake this for the identical behavior).
function nonEmptyStringOrNull(value: unknown): string | null {
  return typeof value === 'string' && value !== '' ? value : null
}

function toAppStatus(value: unknown): AppStatus {
  if (
    value === 'draft' ||
    value === 'pending' ||
    value === 'approved' ||
    value === 'rejected' ||
    value === 'disabled'
  ) {
    return value
  }
  // Unknown variants are dropped HERE, at the boundary, so the control's
  // `assertNever` switch stays unreached in practice (fail-first).
  throw new ApiError('The server returned an app status we could not read.', 500)
}

function toApprovalStatus(value: unknown): AppApprovalStatus {
  if (!isRecord(value) || typeof value.appId !== 'string' || value.appId === '') {
    throw new ApiError('The server returned an app we could not read.', 500)
  }
  return {
    appId: value.appId,
    status: toAppStatus(value.status),
    rejectionNote: nonEmptyStringOrNull(value.rejectionNote),
    submissionId: nonEmptyStringOrNull(value.submissionId),
    commitSha: nonEmptyStringOrNull(value.commitSha),
    submittedAt: nonEmptyStringOrNull(value.submittedAt),
    deployedAt: nonEmptyStringOrNull(value.deployedAt),
    // The server parses this as an https URL before it is ever stored, so the
    // narrower's job here is only shape (string-or-null), not scheme.
    deployedUrl: nonEmptyStringOrNull(value.deployedUrl),
  }
}

function toWithdrawResult(value: unknown): WithdrawResult {
  if (!isRecord(value) || typeof value.appId !== 'string' || value.appId === '') {
    throw new ApiError('The server returned an app we could not read.', 500)
  }
  return { appId: value.appId, status: toAppStatus(value.status) }
}

/**
 * Owner-scoped read of the app's approval lifecycle — the typed client for the live
 * `GET /apps/:id/status` route.
 *
 * NOT what the citizen's publish and review surfaces read any more (U12). They take the
 * lifecycle off the project-scoped deploy status instead, so both of them share one poll
 * lifetime and cannot end up telling the citizen two different things. Reach for this
 * only from a surface that genuinely holds an app id and nothing else.
 */
export async function getApprovalStatus(
  appId: string,
  deps: AuthFetchDeps = {},
): Promise<AppApprovalStatus> {
  const res = await authFetch(`/api/apps/${encodeURIComponent(appId)}/status`, {}, deps)
  if (!res.ok) throw await readApiError(res, 'Failed to read the app status')
  return toApprovalStatus(await res.json())
}

/**
 * Pull the owner's own PENDING submission back out of the queue (P6) — no body; the
 * server knows which submission is pending. Clears the pin, the declaration and the
 * lineage, and leaves the app at `draft`; the approved pin and the immutable submission
 * blob survive. A 409 means it was not pending any more (an administrator got there
 * first), and carries the server's own copy.
 */
export async function withdrawSubmission(
  appId: string,
  deps: AuthFetchDeps = {},
): Promise<WithdrawResult> {
  const res = await authFetch(
    `/api/apps/${encodeURIComponent(appId)}/withdraw`,
    { method: 'POST' },
    deps,
  )
  if (!res.ok) throw await readApiError(res, 'Failed to withdraw the submission')
  return toWithdrawResult(await res.json())
}
