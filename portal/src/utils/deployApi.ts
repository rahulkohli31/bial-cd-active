/**
 * Typed client for one-click deploy (`/api/projects/:projectId/{deploy,deployment}`),
 * mirroring `projectApi.ts`: every call is `fn(args, deps = {})` forwarding `deps` to
 * `authFetch`, responses arrive as `unknown` and pass through a narrower that throws
 * `ApiError` on a structurally-invalid row — never cast, never `any`.
 *
 * ONE CALL DECIDES AND DEPLOYS. The answers ride in the deploy body and are scored by the
 * server inside the same request that publishes, so there is no "score my answers"
 * endpoint to call first — one that merely reported a number would be advisory, and a
 * client that skipped it would reach the pipeline unscored. `getDeployment` is a progress
 * poll, not a second decision: a deploy runs for minutes and the edge gateway gives a
 * request twenty seconds, so the work is detached and the client watches it.
 *
 * The weights below are a DUPLICATE of the server's, kept by hand — there is no codegen
 * across the two languages. That is tolerable only because this copy decides nothing: it
 * drives the running total and the explanation prompt, and the deploy button stays enabled
 * even when the local total looks too low, precisely so the server's verdict is the one the
 * citizen sees. If the two ever drift, the server is right and the UI is merely stale.
 */
import { ApiError, isRecord, readApiError } from './apiError'
import { authFetch } from './api.js'
import type { AuthFetchDeps } from './projectApi'

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
 * `(key, label, weight)` — the source of truth for the modal's question list and its
 * running total, mirroring the backend's `DATA_CLASSIFICATION_QUESTIONS`
 * (`services/deploy/classification.py`). Keep in sync by hand.
 */
export const DATA_CLASSIFICATION_QUESTIONS: ReadonlyArray<
  readonly [key: ClassificationKey, label: string, weight: number]
> = [
  ['credentialsSecrets', 'Credentials / Secrets', 40],
  ['healthData', 'Health Data', 25],
  ['personalInformation', 'Personal Information (PII)', 20],
  ['financialData', 'Financial Data', 20],
  ['confidentialBusinessData', 'Confidential Business Data', 15],
  ['publicData', 'Public Data', 0],
]

/** At or above this total the explanation stops being optional. The server enforces the
 *  same gate with a 422, so this is the UX half, not the boundary. */
export const NOTES_REQUIRED_AT = 25

/** At or above this total the server deploys without a human. Shown to set expectations —
 *  never used to disable the deploy button, because then the client would be the gate. */
export const AUTO_DEPLOY_AT = 50

/** The weighted total for a possibly-partial answer set; unanswered categories don't count. */
export function totalWeight(answers: Partial<Record<string, boolean | null>>): number {
  return DATA_CLASSIFICATION_QUESTIONS.reduce(
    (sum, [key, , weight]) => (answers[key] === true ? sum + weight : sum),
    0,
  )
}

/** The 202 body: the deploy has barely begun and this is the id to poll. */
export interface StartedDeploy {
  deploymentId: string
  appId: string
  status: string
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
}

/** The machine-readable refusal from the classification gate. Branch on this, never on the
 *  message text — the copy is the server's and is expected to change. */
export const CLASSIFICATION_REFUSED = 'classification_below_threshold'

/** The 409 raised when the workspace is ahead of the last save; retry with `saveFirst`. */
export const UNSAVED_CHANGES = 'unsaved_changes'

function readString(value: unknown, field: string): string {
  if (typeof value !== 'string' || value.length === 0) {
    throw new ApiError(`The server sent a deployment we could not read (${field}).`, 500)
  }
  return value
}

function optionalString(value: unknown): string | null {
  if (value === null || value === undefined) return null
  return typeof value === 'string' ? value : null
}

function optionalStatus(value: unknown): DeploymentStatus | null {
  return value === 'running' || value === 'succeeded' || value === 'failed' ? value : null
}

function toStartedDeploy(body: unknown): StartedDeploy {
  if (!isRecord(body)) {
    throw new ApiError('The server sent a deploy response we could not read.', 500)
  }
  return {
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
 * Start a deploy. Resolves on 202 with the id to poll; throws `ApiError` otherwise —
 * notably 409 `classification_below_threshold` (the score was too low, with the server's
 * own explanation on `.message`), 409 `unsaved_changes`, and 422 when the questionnaire is
 * incomplete or an obligatory explanation is missing.
 */
export async function startDeploy(
  projectId: string,
  request: StartDeployRequest,
  deps: AuthFetchDeps = {},
): Promise<StartedDeploy> {
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
  return toStartedDeploy(await res.json())
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
