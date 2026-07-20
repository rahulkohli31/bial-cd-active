/**
 * Typed client for the owner-facing approval flow (`/api/apps/:appId/{submit,status}`),
 * mirroring `projectApi.ts`: every call is `fn(args, deps = {})` forwarding `deps` to
 * `authFetch`, responses arrive as `unknown` and pass through a narrower that throws
 * `ApiError` on a structurally-invalid row — never cast, never `any`.
 *
 * Submit sends NO body (APPROVAL R19): the artifact is the app's server-side git-bundle
 * snapshot, which the backend copies to an immutable per-submission blob. The three
 * distinct 409s (build session running / nothing to submit / illegal state) arrive with
 * self-describing server copy on the `ApiError` — the control renders `err.message`
 * directly, so the copy stays distinct without client-side string matching.
 */
import { ApiError, isRecord, readApiError } from './apiError'
import { authFetch } from './api.js'
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

/** What a successful submit minted (POST /apps/:id/submit). */
export interface SubmitResult {
  appId: string
  status: AppStatus
  submissionId: string
  commitSha: string
  submittedAt: string
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

function toSubmitResult(value: unknown): SubmitResult {
  const status = toApprovalStatus(value)
  // A successful submit ALWAYS mints a submission — absence is a broken response,
  // not a valid state.
  if (status.submissionId === null || status.commitSha === null || status.submittedAt === null) {
    throw new ApiError('The server returned a submission we could not read.', 500)
  }
  return {
    appId: status.appId,
    status: status.status,
    submissionId: status.submissionId,
    commitSha: status.commitSha,
    submittedAt: status.submittedAt,
  }
}

/** Owner-scoped read of the app's approval lifecycle. */
export async function getApprovalStatus(
  appId: string,
  deps: AuthFetchDeps = {},
): Promise<AppApprovalStatus> {
  const res = await authFetch(`/api/apps/${encodeURIComponent(appId)}/status`, {}, deps)
  if (!res.ok) throw await readApiError(res, 'Failed to read the app status')
  return toApprovalStatus(await res.json())
}

/**
 * Submit the app for review — no body; the server forks an immutable copy of the
 * latest build snapshot. 409s carry distinct, self-describing server copy.
 */
export async function submitForReview(appId: string, deps: AuthFetchDeps = {}): Promise<SubmitResult> {
  const res = await authFetch(
    `/api/apps/${encodeURIComponent(appId)}/submit`,
    { method: 'POST' },
    deps,
  )
  if (!res.ok) throw await readApiError(res, 'Failed to submit the app')
  return toSubmitResult(await res.json())
}
