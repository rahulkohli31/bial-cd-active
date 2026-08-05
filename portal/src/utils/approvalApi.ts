/**
 * Typed client for the owner-facing approval flow (`/api/apps/:appId/{submit,status}`),
 * mirroring `projectApi.ts`: every call is `fn(args, deps = {})` forwarding `deps` to
 * `authFetch`, responses arrive as `unknown` and pass through a narrower that throws
 * `ApiError` on a structurally-invalid row — never cast, never `any`.
 *
 * Submit's artifact is still server-side (the git-bundle snapshot the backend copies to
 * an immutable per-submission blob), but submit now ALSO requires a body (V4): the
 * six-question data-classification questionnaire. The three distinct 409s (build session
 * running / nothing to submit / illegal state) — and now the 422 on an incomplete
 * questionnaire or a missing required explanation — arrive with self-describing server
 * copy on the `ApiError`; the control renders `err.message` directly, so the copy stays
 * distinct without client-side string matching.
 */
import { ApiError, isRecord, readApiError } from './apiError'
import { authFetch } from './api.js'
import type { AppStatus, AuthFetchDeps } from './projectApi'

const JSON_HEADERS = { 'Content-Type': 'application/json' }

/**
 * The six-category data-classification questionnaire (V4, task-sheet order — do not
 * reorder or rename). Every category is a required Yes/No; `notes` is optional unless
 * `totalWeight(answers) >= NOTES_REQUIRED_AT` (the server enforces the same gate, so a
 * client bypass still 422s — this is the UX half, not the boundary).
 */
export interface DataClassificationAnswers {
  credentialsSecrets: boolean
  healthData: boolean
  personalInformation: boolean
  financialData: boolean
  confidentialBusinessData: boolean
  publicData: boolean
  notes: string | null
}

/**
 * `(key, label, weight)` — the single source of truth for the modal's question list
 * and the running-total calculation, mirroring the backend's
 * `DATA_CLASSIFICATION_QUESTIONS` (`app_registry.py`). Keep in sync by hand; there is
 * no shared codegen between the two languages here.
 */
export const DATA_CLASSIFICATION_QUESTIONS: ReadonlyArray<
  readonly [key: keyof Omit<DataClassificationAnswers, 'notes'>, label: string, weight: number]
> = [
  ['credentialsSecrets', 'Credentials / Secrets', 40],
  ['healthData', 'Health Data', 25],
  ['personalInformation', 'Personal Information (PII)', 20],
  ['financialData', 'Financial Data', 20],
  ['confidentialBusinessData', 'Confidential Business Data', 15],
  ['publicData', 'Public Data', 0],
]

/** The soft-gate threshold: at or above this weighted total, `notes` is required. */
export const NOTES_REQUIRED_AT = 25

/** The weighted total for a (possibly partial) answer set — unanswered categories don't count. */
export function totalWeight(answers: Partial<Record<string, boolean | null>>): number {
  return DATA_CLASSIFICATION_QUESTIONS.reduce(
    (sum, [key, , weight]) => (answers[key] === true ? sum + weight : sum),
    0,
  )
}

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
  /**
   * Null for a never-submitted app AND for a submission predating this feature — both
   * render as "no answers on file", distinguishable from an answered set (which always
   * carries all six keys, by construction server-side).
   */
  dataClassification: DataClassificationAnswers | null
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

// The six category keys, exactly as they arrive on the wire (camelCase) — used to
// validate a `dataClassification` object has every one, never trusted from a bare
// `Object.keys` walk (an extra or missing key must fail loud, not pass through).
const _CATEGORY_KEYS = DATA_CLASSIFICATION_QUESTIONS.map(([key]) => key)

function toDataClassificationAnswers(value: unknown): DataClassificationAnswers | null {
  if (value === null || value === undefined) return null
  if (!isRecord(value)) {
    throw new ApiError('The server returned a data-classification answer we could not read.', 500)
  }
  for (const key of _CATEGORY_KEYS) {
    if (typeof value[key] !== 'boolean') {
      throw new ApiError(
        'The server returned a data-classification answer we could not read.',
        500,
      )
    }
  }
  return {
    credentialsSecrets: value.credentialsSecrets as boolean,
    healthData: value.healthData as boolean,
    personalInformation: value.personalInformation as boolean,
    financialData: value.financialData as boolean,
    confidentialBusinessData: value.confidentialBusinessData as boolean,
    publicData: value.publicData as boolean,
    notes: nonEmptyStringOrNull(value.notes),
  }
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
    dataClassification: toDataClassificationAnswers(value.dataClassification),
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
 * Submit the app for review, with the data-classification questionnaire attached
 * (V4) — the server forks an immutable copy of the latest build snapshot and
 * records `answers` atomically with the status change, in the SAME request. 409s
 * (build session running / nothing to submit / illegal state) and the 422s (an
 * incomplete questionnaire, or a missing required explanation above the soft-gate
 * threshold) carry distinct, self-describing server copy.
 */
export async function submitForReview(
  appId: string,
  answers: DataClassificationAnswers,
  deps: AuthFetchDeps = {},
): Promise<SubmitResult> {
  const res = await authFetch(
    `/api/apps/${encodeURIComponent(appId)}/submit`,
    { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({ answers }) },
    deps,
  )
  if (!res.ok) throw await readApiError(res, 'Failed to submit the app')
  return toSubmitResult(await res.json())
}
