/**
 * Typed client for one-click deploy (`/api/projects/:projectId/{deploy,deployment}`),
 * mirroring `projectApi.ts`: every call is `fn(args, deps = {})` forwarding `deps` to
 * `authFetch`, responses arrive as `unknown` and pass through a narrower that throws
 * `ApiError` on a structurally-invalid row — never cast, never `any`.
 *
 * ONE CALL DECIDES, AND THEN EITHER PUBLISHES OR QUEUES. The answers ride in the deploy
 * body and are merged and scored by the server inside the same request, so there is no
 * "score my answers" endpoint to call first — one that merely reported a number would be
 * advisory, and a client that skipped it would reach the pipeline unscored. Since U9 that
 * one call has TWO success shapes (`DeployOutcome`): the deploy started, or the app was
 * routed into the administrator's queue at the exact version examined. `getDeployment` is
 * a progress poll, not a second decision: a deploy runs for minutes and the edge gateway
 * gives a request twenty seconds, so the work is detached and the client watches it.
 *
 * THE STATUS POLL ALSO CARRIES THE APP'S APPROVAL STATE (`DeploymentView.approval`, U12),
 * and that is not a layering slip. The citizen has two publish surfaces; the toolbar one
 * is mounted with a project id and no app id, so an app-scoped lifecycle read is not
 * addressable from it at all. Hanging the lifecycle off this response is what lets both
 * surfaces inherit one poll lifetime, one generation guard and one staleness story
 * instead of growing a second fetch-once-and-rot one of their own.
 *
 * The pre-publish REVIEW is a separate surface (`classificationApi.ts`): it pre-fills the
 * questionnaire from an automatic check of the saved code, but the publish request still
 * re-reads the STORED review server-side and merges there — nothing the browser learned
 * from that surface rides into this one as authority.
 *
 * The weights below are a DUPLICATE of the server's, kept by hand — there is no codegen
 * across the two languages. That is tolerable only because this copy decides nothing: it
 * drives the running total and the explanation prompt, and the deploy button stays enabled
 * even when the local total looks too HIGH — a refusal the server issues with its own
 * explanation is the correct outcome, not a UI failure to prevent. If the two ever drift,
 * the server is right and the UI is merely stale.
 */
import { ApiError, isRecord, optionalString, readApiError } from './apiError'
import { authFetch } from './api.js'
import type { AppStatus, ApprovalRoute, AuthFetchDeps } from './projectApi'

/** The six declared categories plus the optional explanation. */
export interface DataClassificationAnswers {
  credentialsSecrets: boolean
  healthData: boolean
  personalInformation: boolean
  financialData: boolean
  confidentialBusinessData: boolean
  publicData: boolean
  notes: string | null
}

export type ClassificationKey = keyof Omit<DataClassificationAnswers, 'notes'>

/**
 * `(key, label, weight, storedKey)` — THE questionnaire on this side of the wire: the
 * modal's question list, its running total, and the labels the admin review screen puts
 * on a stored declaration. Mirrors the backend's `DATA_CLASSIFICATION_QUESTIONS`
 * (`services/deploy/classification.py`). Keep in sync by hand — with ONE table, because
 * two hand-kept mirrors of one server table are two chances to reword a question in half
 * the product (`components/admin/declaration.ts` derives its list from this one).
 *
 * `storedKey` is the SAME question under its snake_case name, which is how it is spelled
 * inside the stored declaration document — that is stored data keyed the way the server
 * keys everything else, not a camelCase wire body. Carrying both spellings here is what
 * makes the pairing checkable in one place instead of inferred at a call site.
 */
export const DATA_CLASSIFICATION_QUESTIONS: ReadonlyArray<
  readonly [key: ClassificationKey, label: string, weight: number, storedKey: string]
> = [
  ['credentialsSecrets', 'Credentials / Secrets', 40, 'credentials_secrets'],
  ['healthData', 'Health Data', 25, 'health_data'],
  ['personalInformation', 'Personal Information (PII)', 20, 'personal_information'],
  ['financialData', 'Financial Data', 20, 'financial_data'],
  ['confidentialBusinessData', 'Confidential Business Data', 15, 'confidential_business_data'],
  ['publicData', 'Public Data', 0, 'public_data'],
]

/** AT OR BELOW this total the server deploys without a human — 0, so only a fully-clean
 *  declaration ever auto-publishes; any weighted category at all needs a person (issue
 *  #115: the gate previously ran the other way, auto-publishing the MORE sensitive
 *  declarations). Also the explanation threshold (issue #117 follow-up) — any total
 *  ABOVE this both needs a person AND is obliged to say why, never one without the
 *  other. Shown to set expectations — never used to disable the deploy button, because
 *  then the client would be the gate. */
export const AUTO_DEPLOY_MAX_SCORE = 0

/** The weighted total for a possibly-partial answer set; unanswered categories don't count. */
export function totalWeight(answers: Partial<Record<string, boolean | null>>): number {
  return DATA_CLASSIFICATION_QUESTIONS.reduce(
    (sum, [key, , weight]) => (answers[key] === true ? sum + weight : sum),
    0,
  )
}

/** The 202 body: the deploy has barely begun and this is the id to poll. */
export interface StartedDeploy {
  outcome: 'started'
  deploymentId: string
  appId: string
  status: string
}

/**
 * The 200 body when the publish gate ROUTED the app to an administrator instead of
 * deploying (U9). An OUTCOME, not a failure: the platform did exactly what the dialog's
 * "Send for review" button said it would, so it renders informationally and never wears
 * the red badge.
 */
export interface RoutedForReview {
  outcome: 'routed_for_review'
  appId: string
  submissionId: string
  commitSha: string
  submittedAt: string
  /** The server's own citizen-facing sentence, so both publish surfaces say the same
   *  words without owning copy of their own. */
  message: string
}

/** One POST, two success shapes, discriminated by `outcome` — switch on it rather than
 *  sniffing which keys happen to be present. */
export type DeployOutcome = StartedDeploy | RoutedForReview

/** The app's approval lifecycle, carried on the deploy STATUS response (U12).
 *
 *  It rides here rather than on a second, app-scoped call because the toolbar publish
 *  button is mounted with a project id and no app id — there is no second call it could
 *  make — and because a surface that reads its own lifecycle once on mount goes stale the
 *  moment the publish it is watching routes into the queue. */
export interface ApprovalState {
  status: AppStatus
  approvedCommitSha: string | null
  /** WHICH lineage the current submission entered through. A `runbook` approval
   *  authorises the manual go-live runbook and never self-publishing (P5), so anything
   *  rendering "you may publish this" reads the lineage as well as the pin. */
  approvalRoute: ApprovalRoute | null
  rejectionNote: string | null
  submittedSha: string | null
  submittedAt: string | null
}

export type DeploymentStatus = 'running' | 'succeeded' | 'failed'

/**
 * The latest deploy attempt, or an all-null envelope when there has never been one —
 * "never deployed" is a normal state a client renders as a Deploy button, not an error.
 */
export interface DeploymentView {
  deploymentId: string | null
  appId: string | null
  status: DeploymentStatus | null
  step: string | null
  url: string | null
  headSha: string | null
  failureCode: string | null
  failureDetail: string | null
  startedAt: string | null
  finishedAt: string | null
  /**
   * Set when an administrator took the published container down (#113). This is a SECOND
   * axis, not a status: an unpublished deployment still reads `succeeded`, because that is
   * still how the attempt ended. Anything that renders a live-app link must test this too —
   * `status === 'succeeded'` alone will happily link a URL that 404s.
   */
  unpublishedAt: string | null
  /**
   * The APP's approval lifecycle, not the deployment's (U12). Null has exactly one
   * meaning — this project has no app row yet — never "we couldn't read it".
   */
  approval: ApprovalState | null
}

/** Is this deployment actually serving traffic right now? The one predicate every "it's
 *  live" affordance should branch on — succeeded AND not taken down. */
export function isLive(deployment: DeploymentView | null | undefined): boolean {
  return deployment?.status === 'succeeded' && !deployment.unpublishedAt
}

/**
 * Deployment failure codes that are NOT failures — the pipeline stopped because the
 * platform ROUTED this version to an administrator instead (ASM20: modelled as the
 * existing failed terminal state with a distinct code rather than a fourth status,
 * because a partial unique index depends on the status set).
 *
 * A LOOKUP, deliberately, rather than an `=== SOME_CONSTANT` comparison: this is the
 * seam the drift-routed publish plugs into, and a set is extended by adding one line
 * with no branch to re-reason about. Every member here renders through
 * `isRoutedForReview` as the informational waiting state and suppresses the red badge.
 *
 * `classification_below_threshold` is DELIBERATELY ABSENT and must not come back: the
 * terminal refusal it named was retired in U9 — the declaration that used to dead-end
 * now routes into a real queue — and the gate no longer emits it at all.
 */
const ROUTED_FAILURE_CODES: ReadonlySet<string> = new Set(['routed_for_review'])

/** Did this deploy stop because the version was routed for review rather than because
 *  something broke? The one predicate both publish surfaces branch on to choose the
 *  informational presentation over the red failure badge. */
export function isRoutedForReview(failureCode: string | null | undefined): boolean {
  return typeof failureCode === 'string' && ROUTED_FAILURE_CODES.has(failureCode)
}

/** The 409 raised when the workspace is ahead of the last save; retry with `saveFirst`. */
export const UNSAVED_CHANGES = 'unsaved_changes'

function readString(value: unknown, field: string): string {
  if (typeof value !== 'string' || value.length === 0) {
    throw new ApiError(`The server sent a deployment we could not read (${field}).`, 500)
  }
  return value
}

function optionalStatus(value: unknown): DeploymentStatus | null {
  return value === 'running' || value === 'succeeded' || value === 'failed' ? value : null
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
  throw new ApiError('The server sent an app status we could not read.', 500)
}

function toApprovalRoute(value: unknown): ApprovalRoute | null {
  // NULL is a real state — a never-submitted draft has no lineage — and an UNKNOWN
  // literal answers null too, which is the conservative reading rather than the lax one:
  // every consumer branches on `=== 'self_publish'`, so "no claim" withholds the
  // self-publish affordance instead of granting it. Throwing here (the earlier policy)
  // was strictly worse — it propagated through the deploy hook's loadError and blanked
  // the citizen's whole Publish card over a field the gate re-decides server-side
  // anyway. This matches the admin client's documented policy for the same wire value.
  if (value === 'runbook' || value === 'self_publish') return value
  return null
}

/** Null only when the project has no app yet — parse-don't-validate at the boundary so
 *  no consumer downstream ever re-checks a raw record. */
function toApprovalState(value: unknown): ApprovalState | null {
  if (value === null || value === undefined) return null
  if (!isRecord(value)) {
    throw new ApiError('The server sent an approval state we could not read.', 500)
  }
  return {
    status: toAppStatus(value.status),
    approvedCommitSha: optionalString(value.approvedCommitSha),
    approvalRoute: toApprovalRoute(value.approvalRoute),
    rejectionNote: optionalString(value.rejectionNote),
    submittedSha: optionalString(value.submittedSha),
    submittedAt: optionalString(value.submittedAt),
  }
}

function toDeployOutcome(body: unknown): DeployOutcome {
  if (!isRecord(body)) {
    throw new ApiError('The server sent a deploy response we could not read.', 500)
  }
  if (body.outcome === 'routed_for_review') {
    return {
      outcome: 'routed_for_review',
      appId: readString(body.appId, 'appId'),
      submissionId: readString(body.submissionId, 'submissionId'),
      commitSha: readString(body.commitSha, 'commitSha'),
      submittedAt: readString(body.submittedAt, 'submittedAt'),
      message: readString(body.message, 'message'),
    }
  }
  return {
    outcome: 'started',
    deploymentId: readString(body.deploymentId, 'deploymentId'),
    appId: readString(body.appId, 'appId'),
    status: readString(body.status, 'status'),
  }
}

function toDeploymentView(body: unknown): DeploymentView {
  if (!isRecord(body)) {
    throw new ApiError('The server sent a deployment we could not read.', 500)
  }
  return {
    deploymentId: optionalString(body.deploymentId),
    appId: optionalString(body.appId),
    status: optionalStatus(body.status),
    step: optionalString(body.step),
    url: optionalString(body.url),
    headSha: optionalString(body.headSha),
    failureCode: optionalString(body.failureCode),
    failureDetail: optionalString(body.failureDetail),
    startedAt: optionalString(body.startedAt),
    finishedAt: optionalString(body.finishedAt),
    unpublishedAt: optionalString(body.unpublishedAt),
    approval: toApprovalState(body.approval),
  }
}

export interface StartDeployRequest {
  answers: DataClassificationAnswers
  /** The citizen's explicit "save and deploy". Default false is the safe default: a deploy
   *  ships the last SAVED version, so deploying over unsaved work unasked publishes
   *  something they never chose. */
  saveFirst?: boolean
}

/**
 * Ask to publish. TWO success shapes since U9, discriminated by `outcome`: `started`
 * (202, the deploy is running and this is the id to poll) and `routed_for_review` (200,
 * the app went into the administrator's queue pinned to `commitSha` and nothing was
 * published). The second is an OUTCOME, not a failure — it resolves, and both publish
 * surfaces render it informationally.
 *
 * Throws `ApiError` otherwise — notably 409 `app_disabled`, 409 `waiting_for_review`
 * (a version is already in the queue; `error.detail` carries the pending state so a
 * surface renders the waiting text without a second call), 409 `unsaved_changes`, 409
 * `snapshot_moved`, 422 `explanation_required`, and 503 `storage_unavailable`.
 */
export async function startDeploy(
  projectId: string,
  request: StartDeployRequest,
  deps: AuthFetchDeps = {},
): Promise<DeployOutcome> {
  const res = await authFetch(
    `/api/projects/${encodeURIComponent(projectId)}/deploy`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ answers: request.answers, saveFirst: request.saveFirst ?? false }),
    },
    deps,
  )
  if (!res.ok) throw await readApiError(res, 'Failed to start the deploy')
  return toDeployOutcome(await res.json())
}

/** The latest deploy attempt for this project — what the client polls while one runs. */
export async function getDeployment(
  projectId: string,
  deps: AuthFetchDeps = {},
): Promise<DeploymentView> {
  const res = await authFetch(
    `/api/projects/${encodeURIComponent(projectId)}/deployment`,
    {},
    deps,
  )
  if (!res.ok) throw await readApiError(res, 'Failed to read the deployment')
  return toDeploymentView(await res.json())
}

/** Plain-language copy for the pipeline's phase labels. The server calls `step` display-only
 *  and never branches on it, so an unrecognised phase falls back to something honest rather
 *  than rendering a raw token or, worse, throwing. */
export function stepLabel(step: string | null): string {
  switch (step) {
    case 'claimed':
      return 'Getting ready'
    case 'packing':
      return 'Packaging your app'
    case 'building':
      return 'Building'
    case 'provisioning':
      return 'Setting up the server'
    case 'starting':
      return 'Starting it up'
    case 'live':
      return 'Live'
    default:
      return 'Working'
  }
}
