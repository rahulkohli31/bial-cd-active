/**
 * Typed client for the pre-publish classification review
 * (`/api/projects/:projectId/classification-review`), mirroring `deployApi.ts`: every call is
 * `fn(args, deps = {})`, and a body arrives as `unknown` and is narrowed or refused.
 *
 * TWO VERBS, ONE SHAPE. The POST ensures a review exists for the saved version under the live
 * class definitions; the GET only reads, for the dialog to poll. Both carry the live policy and
 * classes the dialog scores with. `headSha` is the version saved now and `reviewedSha` the
 * version the stored review examined; `current` says the review examined the saved version
 * under the live class definitions, and answers ride only on a current, complete review.
 * THE BROWSER IS NEVER THE SOURCE: the publish request re-reads the stored review server-side.
 */
import { ApiError, isRecord, optionalString, readApiError } from './apiError'
import { authFetch } from './api.js'
import type { AuthFetchDeps } from './projectApi'

/**
 * `nothing_to_review` is the "no saved code yet" state; `not_reviewed` means saved code with no
 * review ever claimed (a GET-only state — the POST is what claims one); an aged-out running
 * review arrives as `failed`, never as an immortal `running`.
 */
export type ClassificationReviewStatus =
  | 'nothing_to_review'
  | 'not_reviewed'
  | 'running'
  | 'complete'
  | 'failed'

export type ClassKind = 'hard_block' | 'scored'

/** One active class as the dialog renders and scores it. The title is all an owner is shown. */
export interface ReviewClass {
  key: string
  title: string
  kind: ClassKind
  /** Null exactly for a hard block. */
  weight: number | null
}

export interface ReviewPolicy {
  /** A score at or under this publishes by itself. */
  threshold: number
  ownersCanChangeAnswers: boolean
}

/** The reviewer's answer on one class. The reason is multi-line PROSE — render it in a
 *  whitespace-preserving plain element, never through the shared markdown renderer. */
export interface ClassReview {
  verdict: 'yes' | 'no'
  reason: string
}

/** The server strips evidence locations and class descriptions before this body is built. */
export interface ClassificationReview {
  status: ClassificationReviewStatus
  policy: ReviewPolicy
  /** Hard blocks first, then by weight: the order the dialog renders them in. */
  classes: ReviewClass[]
  /** The version saved now and when. Both null in the nothing-to-review state. */
  headSha: string | null
  savedAt: string | null
  reviewedSha: string | null
  /** When the stored review settled. Null while it runs. */
  checkedAt: string | null
  current: boolean
  /** Keyed by class key. Present only on a current, complete review. */
  verdicts: Record<string, ClassReview> | null
  /** The failure taxonomy, only when `status === 'failed'`: the stable machine bucket and the
   *  owner sentence for it. Render the sentence — the copy is the server's. */
  failureCode: string | null
  failureMessage: string | null
  /** Whether asking again can help, already AND-ed with the server's attempt cap. */
  retryable: boolean
}

/** The server's 503 error code when object storage is unreachable — and so is publishing. */
export const STORAGE_UNAVAILABLE = 'storage_unavailable'

function invalid(field: string): ApiError {
  return new ApiError(`The server sent a review we could not read (${field}).`, 500)
}

function readStatus(value: unknown): ClassificationReviewStatus {
  if (
    value === 'nothing_to_review' ||
    value === 'not_reviewed' ||
    value === 'running' ||
    value === 'complete' ||
    value === 'failed'
  ) {
    return value
  }
  throw invalid('status')
}

function readInteger(value: unknown, field: string): number {
  if (typeof value !== 'number' || !Number.isInteger(value)) throw invalid(field)
  return value
}

function readText(value: unknown, field: string): string {
  if (typeof value !== 'string' || value.length === 0) throw invalid(field)
  return value
}

function readPolicy(value: unknown): ReviewPolicy {
  if (!isRecord(value) || typeof value.ownersCanChangeAnswers !== 'boolean') throw invalid('policy')
  return {
    threshold: readInteger(value.threshold, 'policy.threshold'),
    ownersCanChangeAnswers: value.ownersCanChangeAnswers,
  }
}

/** Built from exactly the four fields, so a description a future body carries cannot ride along. */
function readClass(value: unknown): ReviewClass {
  if (!isRecord(value)) throw invalid('class')
  const { kind } = value
  if (kind !== 'hard_block' && kind !== 'scored') throw invalid('class.kind')
  return {
    key: readText(value.key, 'class.key'),
    title: readText(value.title, 'class.title'),
    kind,
    weight: kind === 'scored' ? readInteger(value.weight, 'class.weight') : null,
  }
}

function readClasses(value: unknown): ReviewClass[] {
  if (!Array.isArray(value)) throw invalid('classes')
  return value.map(readClass)
}

function readVerdicts(value: unknown): Record<string, ClassReview> | null {
  if (value === null || value === undefined) return null
  if (!isRecord(value)) throw invalid('verdicts')
  const verdicts: Record<string, ClassReview> = {}
  for (const [key, entry] of Object.entries(value)) {
    if (!isRecord(entry)) throw invalid(key)
    const { verdict, reason } = entry
    if (verdict !== 'yes' && verdict !== 'no') throw invalid(`${key}.verdict`)
    if (typeof reason !== 'string') throw invalid(`${key}.reason`)
    verdicts[key] = { verdict, reason }
  }
  return verdicts
}

function toClassificationReview(body: unknown): ClassificationReview {
  if (!isRecord(body)) throw invalid('body')
  const status = readStatus(body.status)
  const failureMessage = optionalString(body.failureMessage)
  if (status === 'failed' && failureMessage === null) throw invalid('failureMessage')
  return {
    status,
    policy: readPolicy(body.policy),
    classes: readClasses(body.classes),
    headSha: optionalString(body.headSha),
    savedAt: optionalString(body.savedAt),
    reviewedSha: optionalString(body.reviewedSha),
    checkedAt: optionalString(body.checkedAt),
    current: body.current === true,
    verdicts: readVerdicts(body.verdicts),
    failureCode: optionalString(body.failureCode),
    failureMessage,
    // Fail closed: no server flag, no re-check affordance.
    retryable: body.retryable === true,
  }
}

/**
 * Ensure a review exists for the current saved version, and answer with the current state.
 * Opening the publish dialog calls this; "Check again" calls it again (there is no separate
 * retry verb). 200 and 202 both resolve — the body's `status` says whether to poll. Throws
 * `ApiError` otherwise, notably the 503 `storage_unavailable` whose message is the sentence to show.
 */
export async function ensureClassificationReview(
  projectId: string,
  deps: AuthFetchDeps = {},
): Promise<ClassificationReview> {
  const res = await authFetch(
    `/api/projects/${encodeURIComponent(projectId)}/classification-review`,
    { method: 'POST' },
    deps,
  )
  if (!res.ok) throw await readApiError(res, 'Failed to start the automatic check')
  return toClassificationReview(await res.json())
}

/** Read the current review state — what the dialog polls while a run is in flight. Never
 *  starts a run. */
export async function getClassificationReview(
  projectId: string,
  deps: AuthFetchDeps = {},
): Promise<ClassificationReview> {
  const res = await authFetch(
    `/api/projects/${encodeURIComponent(projectId)}/classification-review`,
    {},
    deps,
  )
  if (!res.ok) throw await readApiError(res, 'Failed to read the automatic check')
  return toClassificationReview(await res.json())
}
