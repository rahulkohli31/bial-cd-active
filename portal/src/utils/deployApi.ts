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
  /** WHEN it was approved, beside which commit was. The chip's approved states name the
   *  date first and mute the build code beside it, because a date is the thing a person
   *  recognises. Null exactly when `approvedCommitSha` is — the two are written together
   *  in one place server-side and are never apart. */
  approvedAt: string | null
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
 * THE publish state (R38) — one server-computed field, spelled exactly as
 * `backend/src/api/v1/deploy/schemas.py`'s `PublishState` spells it. The server is its
 * sole author; this union is the client's whole copy, and the chip switches on it and on
 * nothing else.
 *
 * NOTHING HERE RECOMBINES ANYTHING. A client that mirrors a server decision from parts
 * has produced the same class of bug four times in this one feature
 * (`docs/solutions/ui-bugs/publish-dialog-scored-unmerged-answers-2026-08-21.md`), most
 * recently promising "this can publish automatically" moments before the server routed
 * the app to an administrator. `status` + `unpublishedAt` + `failureCode` + the approval
 * lineage + the pin are all still on the wire for the version rows to render, but not one
 * of them is read to decide what state the app is in.
 *
 * Three values look alike and are deliberately three, because the sentence under each is
 * different: `live_current` (the heads agree — nothing of theirs is waiting),
 * `live_newer_work` (they saved since it went live), and `live_drift_unknown` (the server
 * could not make the comparison — a storage read that would not answer, or a bundle saved
 * before the metadata stamp existed). The last one must never be spoken as the first: a
 * false "nothing of yours is waiting" is the exact failure this feature keeps shipping.
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
  /**
   * THE field the publish surface branches on, and the only one it branches on (R38).
   * TOTAL — never null, in every response shape including the empty envelope: there is no
   * state in which the server declines to answer, and a drift it could not determine is
   * its own value rather than an absent field.
   */
  publishState: PublishState
  /**
   * THE CITIZEN'S OWN LAST SAVE — which commit, and when (plan 002, U4).
   *
   * The server spends its ONE object-store metadata HEAD twice instead of once: the same read
   * that computes `publishState`'s drift now also returns the head it compared and the store's
   * last-modified on that bundle. No second call, and NO CONTAINER — which is the whole point,
   * because the rail draws this row on a project whose workspace is stopped, and `save-state`
   * attaches to a container before it can answer.
   *
   * THE TWO HALVES ARE INDEPENDENTLY NULL and neither is ever filled in from the other. A bundle
   * written before the metadata stamp existed has a last-modified but no head, so it can say WHEN
   * without saying WHICH — a renderer has to cover that mixed case, not only "both present" and
   * "both absent". `null` on either axis is "no claim", and must never be spoken as a version.
   *
   * NO COUNT RIDES BESIDE THEM and none can: the snapshot key is overwrite-latest with one bundle
   * per app and there is no version-history table, so "4 newer saves" has no source. The chip says
   * newer work exists; it does not count it.
   */
  savedHead: string | null
  savedAt: string | null
}

/**
 * NO PREDICATE OVER THESE FIELDS LIVES HERE ANY MORE, and none may come back. (The names
 * are deliberately not written out: a retirement guard walks this tree for them.)
 *
 * Four helpers went together, because they were four halves of one mistake. One answered
 * "is it serving traffic right now" from the status and the takedown stamp. One answered
 * "was that failed row actually a routing" from a set of codes, and the set was the other.
 * One turned the pipeline's phase tokens into citizen words. Every one of them re-decided,
 * on this side of the wire, something the server had already decided — and each was a
 * place where two surfaces reading one response could still disagree.
 *
 * `publishState` is where all four answers come from now. If a consumer needs one of them
 * and the field cannot say it, the fix belongs in the server that authors the field, not
 * in a helper here.
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

/**
 * THE publish state, parsed once here so nothing downstream re-checks a raw record.
 *
 * IT THROWS, and the reversal recorded twenty lines up at `toApprovalRoute` does not
 * apply. That reversal was for a SUPPLEMENTARY field: an unknown lineage could be
 * answered `null` — the conservative reading — while the surface still rendered
 * everything else. Here the field IS the surface. There is no conservative reading of
 * "we do not know what state this app is in" that is not itself a claim, and the two
 * candidates are both worse than throwing: guessing a state lies, and rendering nothing
 * is indistinguishable from a broken page on the only publishing surface the citizen
 * has. So it throws, and the chip owns what that looks like — one honest read-failure
 * chip with a re-read and no action, never a blank space where the affordance was.
 * A missing value throws for the same reason: a total field with a hole is a server
 * contract break, not a state.
 *
 * ── THE MIRROR-GAP REGISTER (L12) ──────────────────────────────────────────────────
 * L12 asks that a client mirroring a server decision either mirror the whole decision or
 * not at all, and that where it provably cannot see an input, the gap is written down
 * and proved one-directional. This client does not mirror the decision at all — it
 * consumes it — so what follows is the list of what it could never have seen anyway, and
 * why each gap can only ever cost a press, never a wrong promise.
 *
 * 1. THE TREE THE DECISION IS TAKEN AGAINST MAY BE SAVED INSIDE THE SAME REQUEST. This
 *    is the load-bearing one. `saveFirst` writes a new snapshot before the ladder runs,
 *    and ladder rule 3a defers to the pipeline, so the commit that gets judged need not
 *    exist when this read is taken. No read taken before the press can predict the
 *    outcome. ONE-DIRECTIONAL because the chip never promises an outcome: its button
 *    states the ceiling of what the press will attempt, and the server's answer states
 *    what happened. Publishing directly where the button said "Send update for review"
 *    reads as the better outcome, not as a contradiction.
 * 2. THE MERGED CLASSIFICATION SCORE. The server merges the STORED review with the
 *    submitted answers and scores inside the request. The weights in this module are a
 *    hand-kept duplicate that decides nothing (see the header) — the local total drives
 *    the running tally and the explanation prompt and never withholds the button.
 *    ONE-DIRECTIONAL: a refusal the server issues with its own explanation is the correct
 *    outcome, never a UI failure to prevent.
 * 3. THE SAVED SNAPSHOT'S HEAD. The server spends its one object-store metadata HEAD on
 *    the drift comparison and serves the ANSWER, not the head. So the client cannot
 *    compute drift and therefore cannot contradict the server about it — including that
 *    it cannot quietly resolve `live_drift_unknown` to `live_current`.
 * 4. THE COORDINATION LOCKS. `build_in_flight` (Redis) and `deploy_in_flight` (a
 *    deployments-table predicate) are refusals taken against state this read never
 *    queries. ONE-DIRECTIONAL: they arrive as a refusal after a press, with the server's
 *    own sentence, never as a button this surface withheld on a guess.
 * 5. OWNERSHIP. The gate is enforced with an ownership predicate in the query, not by
 *    this surface offering or withholding anything. A chip that offered nothing would
 *    still be refused if the request were forged, which is why nothing here is a security
 *    control.
 * ───────────────────────────────────────────────────────────────────────────────────
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
