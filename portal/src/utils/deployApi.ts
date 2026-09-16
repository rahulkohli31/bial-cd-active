/**
 * Typed client for one-click deploy (`/api/projects/:projectId/{deploy,deployment}`), mirroring
 * `projectApi.ts`: responses arrive as `unknown` through a narrower that throws `ApiError`.
 *
 * ONE CALL DECIDES, THEN PUBLISHES OR QUEUES: answers are merged and scored server-side in one
 * request, so `DeployOutcome` is either started or routed to the admin queue at the version
 * examined. `getDeployment` polls a detached job — the deploy outruns the gateway's 20s budget —
 * and that poll ALSO carries approval state, since the toolbar publish surface has a project id
 * and no app id. The pre-publish review only pre-fills; the publish re-reads the STORED review
 * server-side, so nothing the browser learned there is authoritative. The weights below decide
 * nothing — see `totalWeight`.
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
 * `(key, label, weight, storedKey)` — THE questionnaire on this side of the wire, mirroring the
 * backend's `DATA_CLASSIFICATION_QUESTIONS` (`services/deploy/classification.py`). Keep in sync
 * by hand, as ONE table — `components/admin/declaration.ts` derives its list from this one.
 * `storedKey` is the same question's snake_case spelling in the stored declaration document,
 * carried here so the pairing is checkable in one place instead of inferred at a call site.
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
 *  declaration ever auto-publishes; any weighted category at all needs a person (the gate
 *  previously ran the other way, auto-publishing the MORE sensitive declarations). Also
 *  the explanation threshold — any total ABOVE this both needs a person AND is obliged to
 *  say why, never one without the other. Shown to set expectations — never used to disable
 *  the deploy button, because then the client would be the gate. */
export const AUTO_DEPLOY_MAX_SCORE = 0

/**
 * The weighted total for a possibly-partial answer set; unanswered categories don't count.
 *
 * This copy of the weights DECIDES NOTHING — it drives the running total and the prompt, and the
 * deploy button stays enabled even at a high local total, because a server refusal is the correct
 * outcome, never a UI failure to prevent.
 */
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
 * deploying. An OUTCOME, not a failure: the platform did exactly what the dialog's
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

/** The app's approval lifecycle, carried on the deploy STATUS response.
 *
 *  It rides here rather than on a second, app-scoped call because the toolbar publish
 *  button is mounted with a project id and no app id — there is no second call it could
 *  make — and because a surface that reads its own lifecycle once on mount goes stale the
 *  moment the publish it is watching routes into the queue. */
export interface ApprovalState {
  status: AppStatus
  approvedCommitSha: string | null
  /** WHEN it was approved, beside which commit was. The chip's approved states name the
   *  date first and mute the build code beside it, because a date is the thing a person
   *  recognises. Null exactly when `approvedCommitSha` is — the two are written together
   *  in one place server-side and are never apart. */
  approvedAt: string | null
  /** WHICH lineage the current submission entered through. A `runbook` approval
   *  authorises the manual go-live runbook and never self-publishing, so anything
   *  rendering "you may publish this" reads the lineage as well as the pin. */
  approvalRoute: ApprovalRoute | null
  rejectionNote: string | null
  submittedSha: string | null
  submittedAt: string | null
}

export type DeploymentStatus = 'running' | 'succeeded' | 'failed'

/**
 * THE publish state — the server's `PublishState` (`backend/src/api/v1/deploy/schemas.py`),
 * one computed field. NOTHING HERE RECOMBINES the raw fields (status, unpublishedAt,
 * failureCode, approval, pin) to re-derive it — doing that has shipped the same bug four
 * times, most recently claiming "can auto-publish" moments before the server routed to a
 * human. `live_current`/`live_newer_work`/`live_drift_unknown` look alike but must stay
 * distinct: the last (comparison failed) must never be spoken as the first (nothing waiting).
 */
export type PublishState =
  | 'nothing_built'
  | 'draft'
  | 'in_review'
  | 'changes_requested'
  | 'approved_ready_to_publish'
  | 'approved_needs_review_again'
  | 'starting_up'
  | 'live_current'
  | 'live_newer_work'
  | 'live_drift_unknown'
  | 'taken_offline'
  | 'switched_off'
  | 'did_not_start'

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
   * Set when an administrator took the published container down. This is a SECOND
   * axis, not a status: an unpublished deployment still reads `succeeded`, because that is
   * still how the attempt ended. Anything that renders a live-app link must test this too —
   * `status === 'succeeded'` alone will happily link a URL that 404s.
   */
  unpublishedAt: string | null
  /**
   * The APP's approval lifecycle, not the deployment's. Null has exactly one
   * meaning — this project has no app row yet — never "we couldn't read it".
   */
  approval: ApprovalState | null
  /**
   * THE field the publish surface branches on, and the only one it branches on.
   * TOTAL — never null, in every response shape including the empty envelope: there is no
   * state in which the server declines to answer, and a drift it could not determine is
   * its own value rather than an absent field.
   */
  publishState: PublishState
  /**
   * THE CITIZEN'S OWN LAST SAVE. The server reuses the ONE object-store metadata HEAD that
   * computes `publishState`'s drift — no second call, no container needed (works on a
   * stopped workspace). The two fields are INDEPENDENTLY NULL: a pre-metadata-stamp bundle
   * has a last-modified but no head, so `null` on either axis means "no claim", never a
   * version. No count rides beside them — one overwrite-latest bundle per app, no
   * version-history table, so "N newer saves" has no source.
   */
  savedHead: string | null
  savedAt: string | null
  /**
   * WHY the pair above is absent, which the pair itself cannot say. See `SavedState`: only
   * `never_saved` removes the rail's row, and `null` — a server that did not say, or a value
   * this client does not know — keeps it.
   */
  savedState: SavedState | null
}

/**
 * NO PREDICATE OVER THESE FIELDS LIVES HERE ANY MORE (names deliberately omitted — a
 * retirement guard walks this tree for them). Four helpers were retired together, each
 * re-deciding server-side state the server already decided — is-it-serving, was-that-a-
 * routing, phase-token-to-citizen-words — each a place two surfaces could disagree.
 * `publishState` answers all four now; a gap the field can't cover gets fixed in the
 * server that authors it, not a new helper here.
 */

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
    approvedAt: optionalString(value.approvedAt),
    approvalRoute: toApprovalRoute(value.approvalRoute),
    rejectionNote: optionalString(value.rejectionNote),
    submittedSha: optionalString(value.submittedSha),
    submittedAt: optionalString(value.submittedAt),
  }
}

/**
 * WHY THE SAVED PAIR IS ABSENT — the three answers that used to be one.
 *
 * `savedHead`/`savedAt` are both null in four different situations and the panel spoke all
 * of them with one sentence, "LAST SAVED — We could not tell". On a project that has never
 * been saved that sentence is not merely vague, it is false in the frightening direction: a
 * citizen reads it as the platform having lost their work, on the panel they open precisely
 * when they are unsure their work is safe.
 *
 * So the server now says WHICH, and the client renders the never-saved case as no row at
 * all. `store_unconfigured` and `storage_error` keep the "could not tell" wording, which is
 * what it was written for — a save that exists and could not be read is a genuine gap in
 * the record, not an absence of work.
 */
export type SavedState = 'saved' | 'never_saved' | 'store_unconfigured' | 'storage_error'

const SAVED_STATES: ReadonlySet<string> = new Set<SavedState>([
  'saved',
  'never_saved',
  'store_unconfigured',
  'storage_error',
])

/**
 * NULL IS THE CONSERVATIVE READING, and it is `toApprovalRoute`'s policy rather than
 * `toPublishState`'s: an unrecognised value must not blank the citizen's whole status panel
 * over a supplementary field. It must also not be read as `never_saved` — the one value
 * that REMOVES a row. "No claim" keeps the row and its honest "could not tell", so a server
 * that grows a fifth member fails towards saying too little rather than towards telling a
 * citizen their save was never made.
 */
function toSavedState(value: unknown): SavedState | null {
  return typeof value === 'string' && SAVED_STATES.has(value) ? (value as SavedState) : null
}

const PUBLISH_STATES: ReadonlySet<string> = new Set<PublishState>([
  'nothing_built',
  'draft',
  'in_review',
  'changes_requested',
  'approved_ready_to_publish',
  'approved_needs_review_again',
  'starting_up',
  'live_current',
  'live_newer_work',
  'live_drift_unknown',
  'taken_offline',
  'switched_off',
  'did_not_start',
])

// ── THE MIRROR-GAP REGISTER ──────────────────────────────────────────────────────────────
// A client mirroring a server decision must mirror it whole or not at all, and where
// it provably cannot see an input, write the gap down and prove it one-directional. This
// client does not mirror the decision — it consumes it — so below is everything it could
// never have seen, and why each gap costs only a press, never a wrong promise.
//
// 1. SAVE TIMING. `saveFirst` can write a new snapshot inside this same request (ladder
//    rule 3a defers to the pipeline), so the commit judged need not exist when this read is
//    taken. One-directional: the button states a ceiling on the attempt, never the outcome
//    — publishing directly beats what it promised, never contradicts it.
// 2. MERGED CLASSIFICATION SCORE. The server merges the stored review with submitted
//    answers and scores in-request; the local weights (see file header) drive only the
//    running tally, never withhold the button — a server refusal-with-explanation is
//    correct, never a UI failure to prevent.
// 3. THE SAVED SNAPSHOT'S HEAD. The server spends its one metadata HEAD on the drift
//    comparison and serves the ANSWER, not the head — this client cannot compute drift,
//    so it cannot quietly resolve `live_drift_unknown` to `live_current`.
// 4. COORDINATION LOCKS. `build_in_flight` (Redis) and `deploy_in_flight` (a DB predicate)
//    refuse against state this read never queries — they arrive as a post-press refusal
//    with the server's own sentence, never a button withheld on a guess.
// 5. OWNERSHIP. Enforced by a query predicate server-side, not by this surface offering or
//    withholding anything — a forged request is refused regardless, so nothing here is a
//    security control.
// ───────────────────────────────────────────────────────────────────────────────────────

/**
 * THE publish state, parsed once here so nothing downstream re-checks a raw record.
 *
 * IT THROWS: no conservative reading of "we don't know this app's state" exists that isn't
 * itself a claim, and guessing or rendering nothing (the only publishing surface) are both
 * worse — the chip owns the read-failure UI, one honest retry, never a blank space. A
 * missing value throws too: a total field with a hole is a server-contract break, not a state.
 */
function toPublishState(value: unknown): PublishState {
  if (typeof value === 'string' && PUBLISH_STATES.has(value)) {
    return value as PublishState
  }
  throw new ApiError('The server sent a publish state we could not read.', 500)
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
    publishState: toPublishState(body.publishState),
    savedHead: optionalString(body.savedHead),
    savedAt: optionalString(body.savedAt),
    savedState: toSavedState(body.savedState),
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
 * Ask to publish. Two success shapes via `outcome`: `started` (202, poll the id) or
 * `routed_for_review` (200, queued pinned to `commitSha`) — an OUTCOME, not a failure, and
 * both surfaces render it informationally. Throws `ApiError` otherwise: 409
 * `app_disabled`/`unsaved_changes`/`snapshot_moved`, 409 `waiting_for_review`
 * (`error.detail` carries the pending state, no second call needed), 422
 * `explanation_required`, 503 `storage_unavailable`.
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

/**
 * Recycle the revision this application is ALREADY RUNNING — same version, same address, same
 * data. 202 with the deployment id to poll, because the container operation behind it outlives
 * the edge gateway's twenty seconds.
 *
 * IT CANNOT RUN A NEWER COMMIT, and that is the rule rather than a detail: re-running the deploy
 * path against whatever is saved now would put work no reviewer has seen into production. That is
 * enforced server-side; nothing a client passes can change which version comes back, which is why
 * this call carries no body at all.
 *
 * Throws `ApiError` on every refusal, each with a stated reason the caller shows verbatim: 409
 * `never_deployed` / `not_live` / `taken_offline` (publish it again instead) / `app_disabled` /
 * `deploy_in_flight`, and 503 `publishing_unavailable`.
 */
export async function restartApp(
  projectId: string,
  deps: AuthFetchDeps = {},
): Promise<{ deploymentId: string }> {
  const res = await authFetch(
    `/api/projects/${encodeURIComponent(projectId)}/restart`,
    { method: 'POST' },
    deps,
  )
  if (!res.ok) throw await readApiError(res, 'Could not restart the app')
  const body = (await res.json()) as { deploymentId?: unknown }
  return { deploymentId: typeof body.deploymentId === 'string' ? body.deploymentId : '' }
}

/**
 * Take the application out of production. The container goes; the application row, its chats, its
 * data and its files all stay, and Publish again puts it back at the same address.
 *
 * IT IS NOT DELETE AND IT IS NOT THE ADMINISTRATOR'S DISABLE. Delete removes the application;
 * `disable` additionally severs its database credential and is a lever only an administrator
 * holds. This one removes a container and nothing else — which is why its confirmation says what
 * is KEPT rather than what is lost.
 *
 * Throws `ApiError` on 409 `never_deployed` / `deploy_in_flight`, and on 503
 * `teardown_unconfirmed`, which means the removal was not observed rather than that it failed —
 * retrying is the right move and is safe.
 */
export async function takeAppDown(
  projectId: string,
  deps: AuthFetchDeps = {},
): Promise<{ message: string }> {
  const res = await authFetch(
    `/api/projects/${encodeURIComponent(projectId)}/takedown`,
    { method: 'POST' },
    deps,
  )
  if (!res.ok) throw await readApiError(res, 'Could not take the app down')
  const body = (await res.json()) as { message?: unknown }
  return { message: typeof body.message === 'string' ? body.message : '' }
}
