/**
 * Typed client for the build-session control API (`/api/build-sessions*`), mirroring
 * `projectApi.ts`: every call is `fn(args, deps = {})`, forwards `deps` to `authFetch` (cookie
 * session + one 401-refresh retry); the edge rewrites `/api/*` → `/v1/*`.
 *
 * Wire format is camelCase; bodies are untrusted `unknown`, narrowed with `toX()` guards — never
 * cast, never `any`. Every non-2xx becomes an `ApiError`, so callers branch on `.status`/`.code`.
 *
 * CSRF: `relaunchPreview` / `stop` (and the project-scoped save / release / stop-active
 * calls below) are mutating POSTs and carry the signed double-submit token (`X-CSRF-Token`,
 * reusing `auth.ts` `getCsrfToken()`); `getStatus` GET and the SSE GET (a separate transport,
 * `buildSessionEvents.ts`) are safe methods and carry NO token. This is net-new: no prior
 * business route in the portal enforces CSRF.
 */
import { ApiError, extractApiCode, extractApiMessage, isRecord, readApiError } from './apiError'
import { authFetch } from './api'
import { asCompileState } from './compileState'
import type { CompileState } from './compileState'
import { getCsrfToken } from './auth'
import type {
  BuildSessionStatus,
  BuildSessionStatusResponse,
  RelaunchPreviewRequest,
  RelaunchPreviewResponse,
  SharedPreviewResponse,
  StopBuildRequest,
  StopBuildResponse,
} from './buildSessionTypes'

/**
 * The dep bundle `authFetch` accepts, injectable so tests need no real network.
 * Derived straight from `authFetch`'s own JSDoc typedef so it can never drift
 * (identical to `projectApi.ts`'s `AuthFetchDeps`).
 */
export type AuthFetchDeps = NonNullable<Parameters<typeof authFetch>[2]>

// NO KEEP-ALIVE CLIENT HERE, and none may be added back: the `renew` / `heartbeat` routes one
// would call are retired, so it would be a browser timer POSTing at a 404. The lock and
// heartbeat TTLs are the backend schema's and are not mirrored here.

const BASE = '/api/build-sessions'
const JSON_HEADERS = { 'Content-Type': 'application/json' }

/**
 * Thrown by `relaunchPreview` on a `409 build_session_already_active`, carrying the EXISTING
 * session's id — the `409` alone is not a self-describing discriminator.
 *
 * Its one live handler is `StartAppControl`, which reports "a build is already running in this
 * project" through the workspace state.
 */
export class BuildSessionAlreadyActiveError extends ApiError {
  readonly existingSessionId: string | null

  constructor(message: string, existingSessionId: string | null) {
    super(message, 409, 'build_session_already_active')
    this.name = 'BuildSessionAlreadyActiveError'
    this.existingSessionId = existingSessionId
  }
}

// ─── narrowing helpers (parse untrusted responses at the boundary) ───────────

function asString(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

function asStringOrNull(value: unknown): string | null {
  return typeof value === 'string' ? value : null
}

function asNumberOrNull(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

/** A status we don't recognize is unusable — fail closed rather than let the UI render an undefined lifecycle. */
function toBuildSessionStatus(value: unknown): BuildSessionStatus {
  if (
    value === 'provisioning' ||
    value === 'building' ||
    value === 'ready' ||
    value === 'ended' ||
    value === 'failed'
  ) {
    return value
  }
  throw new ApiError('The server returned a build session we could not read.', 500)
}

/** A build session with no `sessionId` is not a session — fail at the boundary (parity with `projectApi.toProject`). */
function requireSessionId(value: Record<string, unknown>): string {
  if (typeof value.sessionId !== 'string' || value.sessionId === '') {
    throw new ApiError('The server returned a build session we could not read.', 500)
  }
  return value.sessionId
}

/**
 * `projectId` drives the 409 reattach-vs-block routing (the projectId comparison IS the
 * gate, not the bare 409) — a session response without one would silently mis-route every
 * reattach decision, so it fails at the boundary like a missing `sessionId` (mirror guard).
 */
function requireProjectId(value: Record<string, unknown>): string {
  if (typeof value.projectId !== 'string' || value.projectId === '') {
    throw new ApiError('The server returned a build session we could not read.', 500)
  }
  return value.projectId
}

function toRelaunchPreviewResponse(value: unknown): RelaunchPreviewResponse {
  if (!isRecord(value)) throw new ApiError('The server returned a preview we could not read.', 500)
  // No sessionId/createdAt on this shape (Decision 6) — do NOT reuse requireSessionId here.
  return {
    appId: asString(value.appId),
    previewUrl: asString(value.previewUrl),
    status: toBuildSessionStatus(value.status),
    // Absent/malformed reads as false — the label is an honesty aid, never a gate.
    restoredFromFailedBuild: value.restoredFromFailedBuild === true,
    // Absent reads as TRUE, unlike the flag above, and the asymmetry is deliberate: this field is
    // new, and every server that predates it only ever answered once the app was serving. Reading
    // a missing value as `false` would put a permanent "not ready yet" on those correct responses.
    ready: value.ready !== false,
  }
}

function toBuildSessionStatusResponse(value: unknown): BuildSessionStatusResponse {
  if (!isRecord(value)) throw new ApiError('The server returned a build session we could not read.', 500)
  return {
    sessionId: requireSessionId(value),
    projectId: requireProjectId(value),
    appId: asString(value.appId),
    status: toBuildSessionStatus(value.status),
    previewUrl: asStringOrNull(value.previewUrl),
    lastSeq: asNumberOrNull(value.lastSeq),
    createdAt: asString(value.createdAt),
    updatedAt: asString(value.updatedAt),
  }
}

function toStopBuildResponse(value: unknown): StopBuildResponse {
  if (!isRecord(value)) throw new ApiError('The server returned a build session we could not read.', 500)
  return { sessionId: requireSessionId(value), status: toBuildSessionStatus(value.status) }
}

// ─── request plumbing ────────────────────────────────────────────────────────

/** The double-submit CSRF header for a mutating POST, or `{}` when no csrf cookie is readable (parity with `auth.ts`). */
function csrfHeaders(): Record<string, string> {
  const csrf = getCsrfToken()
  return csrf ? { 'X-CSRF-Token': csrf } : {}
}

/** The session id a `build_session_already_active` body carries (top-level or under `error`), or null. */
function existingSessionIdOf(body: unknown): string | null {
  if (!isRecord(body)) return null
  if (typeof body.sessionId === 'string') return body.sessionId
  const err = body.error
  if (isRecord(err) && typeof err.sessionId === 'string') return err.sessionId
  return null
}

/**
 * A plain GET with the same error handling. Separate from `postJson` rather than a flag on it,
 * because a GET carries no CSRF header and no body — and a helper that took "is this a mutation"
 * as an argument would be one edit away from sending one that did.
 */
async function getJson(
  url: string,
  fallback: string,
  deps: AuthFetchDeps,
  signal?: AbortSignal,
): Promise<unknown> {
  const res = await authFetch(url, signal ? { signal } : {}, deps)
  if (!res.ok) {
    const errBody: unknown = await res.json().catch(() => null)
    throw new ApiError(extractApiMessage(errBody, res.status, fallback), res.status, extractApiCode(errBody))
  }
  return res.json().catch(() => null)
}

/**
 * A mutating POST with CSRF. `body === undefined` sends no JSON body — the project-scoped
 * commands (`saveProject` / `releaseProject` / `stopActiveBuild`) name the target in the path
 * and carry nothing else. A non-2xx becomes an `ApiError`, EXCEPT a
 * `409 build_session_already_active` which becomes the richer
 * `BuildSessionAlreadyActiveError` carrying the existing session id.
 */
async function postJson(url: string, body: unknown, fallback: string, deps: AuthFetchDeps): Promise<unknown> {
  const hasBody = body !== undefined
  const res = await authFetch(
    url,
    {
      method: 'POST',
      headers: { ...(hasBody ? JSON_HEADERS : {}), ...csrfHeaders() },
      ...(hasBody ? { body: JSON.stringify(body) } : {}),
    },
    deps,
  )
  if (!res.ok) {
    const errBody: unknown = await res.json().catch(() => null)
    const code = extractApiCode(errBody)
    const message = extractApiMessage(errBody, res.status, fallback)
    if (res.status === 409 && code === 'build_session_already_active') {
      throw new BuildSessionAlreadyActiveError(message, existingSessionIdOf(errBody))
    }
    // CARRY THE WHOLE ERROR OBJECT. This built its own ApiError and dropped everything but
    // the message and code, so `sandbox_reclaim_blocked` arrived with no projectId — and
    // `asReclaimBlocked` returned null, so Relaunch rendered the refusal as red text in the
    // preview pane instead of the dialog that offers to save the other project.
    const details = isRecord(errBody) && isRecord(errBody.error) ? errBody.error : null
    throw new ApiError(message, res.status, code, details)
  }
  return res.json()
}

// ─── control operations ─────────────────────────────────────────────

// `start` IS GONE: nothing here provisions a session — a build happens inside the turn's own
// transaction. The ROUTE is untouched; deleting a browser client says nothing about it.

/**
 * `relaunch` — restore a project's saved app into a fresh, ready sandbox and get its live URL.
 * A mutating POST (carries CSRF). Project-scoped, not session-scoped: the torn-down session is gone.
 * `postJson` already turns a `409 build_session_already_active` into `BuildSessionAlreadyActiveError`
 * (a build is running); 404 = nothing to relaunch, 503 = transient/retryable.
 */
export async function relaunchPreview(
  args: RelaunchPreviewRequest,
  deps: AuthFetchDeps = {},
): Promise<RelaunchPreviewResponse> {
  const body = await postJson(`${BASE}/relaunch`, { projectId: args.projectId }, 'Failed to relaunch the preview', deps)
  return toRelaunchPreviewResponse(body)
}

/** `stop` — graceful stop (snapshot → teardown → release). Idempotent. `reason` is only sent when supplied. */
export async function stop(sessionId: string, args: StopBuildRequest = {}, deps: AuthFetchDeps = {}): Promise<StopBuildResponse> {
  const body = args.reason !== undefined ? { reason: args.reason } : {}
  const res = await postJson(`${BASE}/${encodeURIComponent(sessionId)}/stop`, body, 'Failed to stop build session', deps)
  return toStopBuildResponse(res)
}

/** `getStatus` — the poll surface and the source of the framable `previewUrl` + `lastSeq`. A safe GET: no CSRF. */
export async function getStatus(sessionId: string, deps: AuthFetchDeps = {}): Promise<BuildSessionStatusResponse> {
  const res = await authFetch(`${BASE}/${encodeURIComponent(sessionId)}`, {}, deps)
  if (!res.ok) throw await readApiError(res, 'Failed to load build session status')
  return toBuildSessionStatusResponse(await res.json())
}

// ─── lock operations — THERE ARE NONE LEFT ─────────────────────────────────
//
// `acquireLock` and `releaseLock` are gone: nothing called them — the portal's blind
// keep-alive loop that was their only caller was itself deleted, same as `renewLock` and
// `heartbeat` before them (see the note above).
//
// `forceEnd` is gone too, and so is the ROUTE it spoke to. It was the owner-only kill switch
// for a session stuck mid-`building` that never emits a terminal `ended`, but its one control
// was the block banner's Force-end button, deleted with the banner — so no surface could reach
// it any more, and keeping a client for it only advertised a way to end a build that a citizen
// could not actually take. What a live build offers now is `stop` (graceful, the whole
// interrupt vocabulary of a turn) and, project-scoped, `stopActiveBuild`.

/**
 * The dependency bag the client + event feed accept, so a hook and a page
 * can swap in the scripted mock (dev/test) or the real transport (prod default).
 * The client half is the `buildSessionApi` module surface; the feed half is the
 * `EventSource` factory (`buildSessionEvents.ts`).
 */
export interface BuildSessionClient {
  relaunchPreview: typeof relaunchPreview
  stop: typeof stop
  getStatus: typeof getStatus
}

/** The real, wired-by-default client — already the final implementation, so no later swap between mock and real is needed. */
export const buildSessionClient: BuildSessionClient = {
  relaunchPreview,
  stop,
  getStatus,
}

// --- the save model ---------------------------------------------------------

export interface SaveResult {
  appId: string
  headSha: string | null
}

export interface SaveState {
  appId: string | null
  /** TRI-STATE, and the null is load-bearing: `null` means UNKNOWN — no live workspace to
   *  compare, or a bundle the server could not read. Rendering it as clean would tell the
   *  user their work is safe when nothing actually checked. */
  dirty: boolean | null
  containerHead: string | null
  savedHead: string | null
  /** WHEN THE PLATFORM LAST PUT THIS APP'S NEWEST TREE SOMEWHERE IT CAN BE BROUGHT BACK FROM —
   *  an ISO instant, or `null` if it never has. Kept as the string it arrived as: the only
   *  question anything asks of it is null-vs-not, and no surface here does date arithmetic.
   *
   *  IT IS NOT A SECOND `savedHead` AND MAY NEVER BE READ AS ONE. A recovery copy is the
   *  platform's own doing; a saved version is the citizen's, Save stays MANUAL, and `dirty`
   *  stays true while this is set. What it licenses is a truer WARNING, never a claim of
   *  safety-by-saving — the rail's `saveSentence` is where that reasoning is written down. */
  recoveryAt: string | null
}

/**
 * IS THE PLATFORM HOLDING A COPY OF THIS TREE THAT IT CAN PUT BACK? Named once because three
 * surfaces ask it — the rail's sentence, the in-place exit dialog and the browser-unload prompt —
 * and three hand-written readings of one fact are three chances for them to disagree about the
 * same app in the same moment.
 *
 * ANYTHING THAT IS NOT AN ACTUAL INSTANT IS "NO", `undefined` INCLUDED, and that is the whole
 * reason this is a function rather than `!== null` written out three times. Written that way, an
 * `undefined` — a caller that never set the field, a test double that predates it, a body the
 * server did not send — reads as YES. That is the one direction this fact may never fail in:
 * every consumer uses a YES to STOP warning somebody, so an absent field would silently disarm a
 * warning about work that exists only inside a container. Absent means warn.
 */
export const canBePutBack = (recoveryAt: string | null | undefined): boolean =>
  typeof recoveryAt === 'string' && recoveryAt !== ''

/** Two readings that say the same thing. Every field is a primitive, so this is exact rather
 *  than an approximation — and it exists so a poll that keeps reporting the same answer stops
 *  handing consumers a new object to re-render for.
 *
 *  EVERY FIELD MEANS EVERY FIELD, AND OMITTING ONE IS NOT A MISSED OPTIMISATION — IT DISCARDS
 *  THE NEW VALUE. The caller keeps the PREVIOUS object whenever this answers "same"
 *  (`useWorkspaceState`: `sameSaveState(prev, state) ? prev : state`), so a field this cannot
 *  see never reaches the screen at all: the reading that changed is thrown away and the rail
 *  goes on saying the sentence that belonged to the old one. `recoveryAt` is polled like the
 *  rest of them, and it decides which sentence the rail says. */
export const sameSaveState = (a: SaveState | null, b: SaveState | null): boolean =>
  a === b ||
  (a !== null &&
    b !== null &&
    a.appId === b.appId &&
    a.dirty === b.dirty &&
    a.containerHead === b.containerHead &&
    a.savedHead === b.savedHead &&
    a.recoveryAt === b.recoveryAt)

/** Push the project's current tree to durable storage. THE USER'S CLICK — nothing else writes
 *  the bundle. A 409 means the workspace is no longer running, and is surfaced, never
 *  swallowed: a Save that reports success having stored nothing is the worst outcome here. */
export async function saveProject(projectId: string, deps: AuthFetchDeps = {}): Promise<SaveResult> {
  const body = await postJson(
    `${BASE}/projects/${encodeURIComponent(projectId)}/save`,
    undefined,
    'Could not save your work',
    deps,
  )
  if (!isRecord(body)) throw new ApiError('The server returned a save we could not read.', 500)
  return {
    appId: typeof body.appId === 'string' ? body.appId : projectId,
    headSha: typeof body.headSha === 'string' ? body.headSha : null,
  }
}

/** The workspace is one-per-user, so opening a second project needs the first to give up its
 *  container. THE ONLY ROUTE THAT DESTROYS ONE ON PURPOSE — the start path used to do this
 *  silently, inside the request for a different project, with the user's unsaved work in it.
 *  `released: false` is a success: the workspace was already gone, which is what was asked. */
export async function releaseProject(
  projectId: string,
  deps: AuthFetchDeps = {},
): Promise<boolean> {
  const body = await postJson(
    `${BASE}/projects/${encodeURIComponent(projectId)}/release`,
    undefined,
    'Could not close the other workspace',
    deps,
  )
  return isRecord(body) && body.released === true
}

function toSharedPreviewResponse(value: unknown): SharedPreviewResponse {
  if (!isRecord(value)) throw new ApiError('The server returned a preview we could not read.', 500)
  return {
    appId: typeof value.appId === 'string' ? value.appId : '',
    previewUrl: typeof value.previewUrl === 'string' ? value.previewUrl : '',
    ready: value.ready === true,
    snapshotTakenAt: typeof value.snapshotTakenAt === 'string' ? value.snapshotTakenAt : null,
  }
}

/**
 * Open a project a colleague shared with you (#198). Attaches to an already-live view if one
 * is up (a reopened tab, a second click); otherwise restores one from the owner's latest
 * SAVED snapshot.
 *
 * IT DOES TAKE THE CALLER'S OWN ONE-PER-USER SLOT — the same slot a build occupies — via the
 * identical `_holding_user_lock` skeleton `relaunchPreview` runs under. A prior docstring here
 * said the opposite ("registers no build session... nothing here occupies the caller's own
 * slot"), which was true of the build-SESSION bookkeeping and false of the thing that actually
 * matters to a caller: whether pressing this can conflict with something else. It can — a
 * `409 sandbox_reclaim_blocked` here means exactly what it means on a relaunch, and the caller
 * has to handle it the same way (see `asReclaimBlocked` / `ReclaimBlocked.isSharedView`).
 *
 * NOT "READ-ONLY" EITHER, for the same Key Decision 3 reason the product copy already gets
 * right: the recipient can create, update and delete the owner's records through the app's own
 * UI. "Can use", never "view only" — these two functions open the door, nothing more.
 */
export async function launchSharedPreview(
  projectId: string,
  deps: AuthFetchDeps = {},
): Promise<SharedPreviewResponse> {
  const body = await postJson(
    `${BASE}/projects/${encodeURIComponent(projectId)}/shared-launch`,
    undefined,
    'Could not open this shared project',
    deps,
  )
  return toSharedPreviewResponse(body)
}

/**
 * Re-restore a shared project from whatever is CURRENTLY saved (#198) — unlike Launch, never
 * attaches to an already-live view even when one is up, since the owner may have saved
 * something newer since it came up. `snapshotTakenAt` on the response is how the caller
 * learns whether anything actually moved.
 */
export async function refreshSharedPreview(
  projectId: string,
  deps: AuthFetchDeps = {},
): Promise<SharedPreviewResponse> {
  const body = await postJson(
    `${BASE}/projects/${encodeURIComponent(projectId)}/shared-refresh`,
    undefined,
    'Could not refresh this shared project',
    deps,
  )
  return toSharedPreviewResponse(body)
}

/**
 * WHAT A STOP ACHIEVED — THREE NAMED STATES, never a boolean.
 *
 * The boolean this replaced hardcoded success on both branches, so a stop that had NOT
 * finished — the one answer a caller must never act on — read identically to one that had.
 * Folding a timeout into `false` ("nothing was running") would take a container out from
 * under a task still writing to it.
 *
 *   `stopped`              proceed — something ran and has finished unwinding.
 *   `nothing_was_running`  proceed — there was nothing to stop.
 *   `still_running`        DO NOT PROCEED — the wait expired, or something still holds the app.
 */
export type StopState = 'stopped' | 'nothing_was_running' | 'still_running'

/** The two states a hand-over may act on. Named rather than inlined, because "which of these
 *  means go" is the whole decision and it should have one place to be read. */
export function stopSettled(state: StopState): boolean {
  return state === 'stopped' || state === 'nothing_was_running'
}

function readStopState(body: unknown): StopState {
  const state = isRecord(body) ? body.state : undefined
  if (state === 'stopped' || state === 'nothing_was_running' || state === 'still_running') {
    return state
  }
  // AN UNREADABLE ANSWER IS "STILL RUNNING", which is the only safe default: it refuses to
  // proceed. Reading it as settled would let an unparseable body take somebody's container.
  return 'still_running'
}

/**
 * ASK for the stop. Returns immediately with the state at the instant the ask landed — usually
 * `still_running`, because the unwind has barely begun.
 *
 * NOTHING HOLDS A REQUEST OPEN FOR THE LENGTH OF A STOP any more: the old shape's budget had
 * to sit under the request timeout of the gateway in front of the service, a number recorded
 * nowhere in this repo and owned by the client's network.
 */
export async function stopActiveBuild(projectId: string, deps: AuthFetchDeps = {}): Promise<StopState> {
  return readStopState(
    await postJson(
      `${BASE}/projects/${encodeURIComponent(projectId)}/stop-active-build`,
      undefined,
      'Could not stop the build in the other project',
      deps,
    ),
  )
}

/** READ the real state, from the source of truth rather than from elapsed time. */
export async function readStopStateOf(
  projectId: string,
  deps: AuthFetchDeps = {},
  signal?: AbortSignal,
): Promise<StopState> {
  return readStopState(
    await getJson(
      `${BASE}/projects/${encodeURIComponent(projectId)}/stop-state`,
      'Could not check on the other project',
      deps,
      signal,
    ),
  )
}

/**
 * HOW OFTEN TO ASK, AND FOR HOW LONG. Ordinary tuning; neither number constrains anything —
 * the read is cheap, nothing is held open, and the ceiling only decides when the dialog stops
 * saying "closing…" and starts saying it could not.
 *
 * DELIBERATELY BELOW THE SERVER'S OWN STOP BUDGET (~8 minutes: four bounded two-minute steps,
 * including the unwind's snapshot write). Nobody should sit in front of a modal for eight
 * minutes to learn whether they may switch projects, so the browser stops WAITING first, at two.
 *
 * EXPIRING HERE IS NOT A VERDICT. The stop runs as a detached server-side task; the state read
 * is the authority and is idempotent; the hand-over proceeds only on a settled answer. Reaching
 * this ceiling ends the WAIT, not the stop — a second press picks it up wherever it got to, and
 * nothing here may say it failed (the likely cause is a large app still being packed up, per
 * `handOverWorkspace`'s sentence).
 */
export const STOP_POLL_MS = 1200
export const STOP_CEILING_MS = 120_000
/**
 * HOW LONG ONE READ MAY HANG BEFORE IT IS ABANDONED — a REAL timer, not the injected clock's.
 *
 * The ceiling below is checked BETWEEN iterations, so it can only fire if each iteration
 * returns. `authFetch` sets no timeout of its own: a stalled connection never settles, the
 * `while` never re-evaluates, and the two-minute ceiling silently becomes forever, with the
 * dialog stuck holding Escape and its overlay click disabled. So every read is abandoned on
 * its own deadline and retried — the same treatment a dropped connection already gets.
 *
 * `setTimeout`, not `clock.sleep`: the injected clock's `sleep` resolves instantly for tests,
 * so racing a read against it would abandon every read in every test. This bound is about a
 * socket, not about pacing.
 */
const STOP_READ_TIMEOUT_MS = 15_000

/**
 * Wait for a stop to genuinely finish, narrating while it does.
 *
 * IT POLLS THE STATE RATHER THAN WATCHING A CLOCK — a container declared dead when merely slow
 * has destroyed unsaved work in this repo before, because a timeout was read as a verdict.
 *
 * A DROPPED CONNECTION IS NOT A VERDICT EITHER: a failed read is retried until the ceiling
 * rather than treated as "still running forever", because the stop itself runs server-side and
 * the state read is idempotent — the caller can simply ask again.
 *
 * BUT A DECIDED ANSWER IS NOT A BLIP, AND RETRYING ONE IS ITS OWN DEFECT. A session that expired
 * mid-hand-over answers 401 to every read; `authFetch` retries a refresh on each one, so the
 * dialog once sat on "Closing the other app…" for two minutes issuing a hundred refresh attempts
 * — traffic that trips reuse-detection — before answering with the ceiling's own sentence, which
 * had nothing to do with what actually failed. 403 (and a 404 from a project deleted in another
 * tab) behave the same way: the poll cannot change those answers, so they travel straight to the
 * dialog, which says the true thing immediately. Everything else — an abort, a dropped socket, a
 * 5xx — is still a blip.
 */
export interface StopWaitClock {
  now: () => number
  sleep: (ms: number) => Promise<void>
}

/** The real one. Injectable, because a test that actually waited two minutes for the ceiling
 *  would be a test nobody runs — and the ceiling's behaviour is exactly what must be proven. */
export const REAL_CLOCK: StopWaitClock = {
  now: () => Date.now(),
  sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
}

/**
 * The statuses a repeat of the same read can only answer the same way: the session is gone, this
 * user may not ask, or there is no such project. Deliberately NOT 5xx — a server that fell over
 * mid-stop may well be up on the next tick, and the stop is still running behind it.
 */
const DECIDED_STATUSES: ReadonlySet<number> = new Set([401, 403, 404])

const isDecided = (err: unknown): boolean => err instanceof ApiError && DECIDED_STATUSES.has(err.status)

export async function awaitStopSettled(
  projectId: string,
  deps: AuthFetchDeps = {},
  clock: StopWaitClock = REAL_CLOCK,
): Promise<StopState> {
  const { now, sleep } = clock
  const deadline = now() + STOP_CEILING_MS
  let last: StopState = 'still_running'
  while (now() < deadline) {
    const abandon = new AbortController()
    const bell = setTimeout(() => abandon.abort(), STOP_READ_TIMEOUT_MS)
    try {
      last = await readStopStateOf(projectId, deps, abandon.signal)
      if (stopSettled(last)) return last
    } catch (err) {
      if (isDecided(err)) throw err
      // Retried below. See the docblock: a read that failed — or was abandoned — decided nothing.
    } finally {
      clearTimeout(bell)
    }
    await sleep(STOP_POLL_MS)
  }
  return last
}

/** Hand the workspace over: STOP, then optionally SAVE, then RELEASE — what the refusal
 *  dialog's buttons do to the server, in the one order that works.
 *
 *  TAKES THE WHOLE `ReclaimBlocked`, NOT A BARE PROJECT ID (#198) — the one thing that makes
 *  the branch below impossible to drop at a call site again. Four surfaces (`StartAppControl`,
 *  `ProjectWorkspace`, `ConversationSurface`, and this page itself) each used to call this with
 *  `blocked.projectId` alone, and none of them learned that a shared occupant's `projectId`
 *  names its OWNER, never the caller — so `stopActiveBuild`'s `owned_project_or_404` 404'd
 *  every one of them. Passing the whole object means the discriminant travels with the id it
 *  qualifies, in one place, rather than needing to be re-remembered at every call site.
 *
 *  A SHARED OCCUPANT NEVER REACHES `stopActiveBuild`/`saveProject`/`releaseProject` AT ALL —
 *  none of the three would even resolve the right project for it. `giveUpSharedView` is the
 *  entire remedy: no id, no ownership check, reaping under the caller's own registry key.
 *  `save` is accepted but ignored on this arm — a shared view's `dirty` is always `false`, so
 *  `ReclaimWorkspaceDialog`'s own `copyFor` never even renders a Save button for it.
 *
 *  THE ORDER ON THE ORDINARY ARM IS THE DESIGN: save and release BOTH refuse while an agent is
 *  writing, so saving first would simply fail (or, past that guard, bundle a tree caught
 *  mid-edit) — stopping settles the turn first, and only then is there a coherent tree to save.
 *
 *  THE STOP IS UNCONDITIONAL, not gated on `ReclaimBlocked.building` (true only for a Write
 *  turn). Every mode pins the container and `release` refuses for all of them, so gating the
 *  stop on the Write-only flag would leave Ask/Plan turns stuck in the dead end this flow
 *  removes. Stopping when nothing is running is free.
 *
 *  IT WAITS FOR THE STOP TO GENUINELY FINISH: the ask returns immediately, the state read is
 *  the authority, and this proceeds only on a settled answer — a timeout reports still
 *  running and the transfer DOES NOT PROCEED (shipped confused before, without this).
 *
 *  REJECTS RATHER THAN SWALLOWS: a failed save must not be followed by a release, so the
 *  rejection travels back to the only thing still mounted that can report it. */
export async function handOverWorkspace(
  blocked: ReclaimBlocked,
  save: boolean,
  deps: AuthFetchDeps = {},
  narrate: (step: HandoverStep) => void = () => {},
  clock: StopWaitClock = REAL_CLOCK,
): Promise<void> {
  if (blocked.isSharedView) {
    // NARRATE AFTER, NOT BEFORE. A caller that reads its OWN narration callback as a record of
    // how far the hand-over got (`StartAppControl.tsx`'s `useTakeBack`, which infers "was
    // anything stopped?" from the last step it observed) must see NO step at all when this
    // throws — nothing here ever stops anything, on either outcome, so a step recorded before
    // the call would make a rejection look exactly like a successful stop of the OWNER's app.
    await giveUpSharedView(deps)
    narrate('releasing')
    return
  }
  const projectId = blocked.projectId
  narrate('stopping')
  const asked = await stopActiveBuild(projectId, deps)
  const settled = stopSettled(asked) ? asked : await awaitStopSettled(projectId, deps, clock)
  if (!stopSettled(settled)) {
    // NOT AN ERROR OF OURS, AND NOT A REASON TO TAKE THE CONTAINER. Everything the citizen has is
    // still where it was; what failed is the wait, and asking again is the remedy.
    throw new ApiError(
      // THE TRUE THING AT TWO MINUTES, which is not "that project failed". The
      // browser's ceiling is under the server's on purpose — see `STOP_CEILING_MS` — so arriving
      // here almost always means the other app is still putting its work away, and the wait is
      // what ran out rather than the stop. Nothing has been taken from either project, and asking
      // again resumes the same stop instead of starting a second one.
      'The other app is still saving its work. Nothing has changed — give it a moment and try again.',
      409,
      'stop_did_not_settle',
    )
  }
  if (save) {
    narrate('saving')
    await saveProject(projectId, deps)
  }
  narrate('releasing')
  await releaseProject(projectId, deps)
}

/**
 * What a hand-over is doing right now, so the dialog can say it rather than spin.
 *
 * STOPS AT `starting` DELIBERATELY. An `opening` member once existed for the chat being
 * opened, but nothing could ever produce it: the retry's last act is a navigate, which
 * unmounts the dialog-publishing surface in the same commit that would carry the step. The
 * destination narrates its own wait, so an unreachable member was dead code with copy attached.
 */
export type HandoverStep = 'stopping' | 'saving' | 'releasing' | 'starting'

/** The project standing in the way, read off a `sandbox_reclaim_blocked` 409. */
export interface ReclaimBlocked {
  projectId: string
  projectName: string
  /** TRI-STATE like `SaveState.dirty`: `true` = known unsaved work, `null` = the server reached
   *  the workspace but could not ask it. Both block; only the copy differs, because promising
   *  "nothing to lose" when nobody could check is the one wrong answer available here.
   *  Always `null` when `building` — see below. */
  dirty: boolean | null
  /** An agent is WRITING in that project right now, so this is a different choice with a
   *  different cost: resolving it stops work in progress, not just a container.
   *
   *  `dirty` is null here because the server deliberately did not ask — a `git status` taken
   *  mid-write describes an instant nobody cares about — so the dialog must not say "unsaved
   *  changes". And Save/Release both refuse until the build stops, which is why this variant
   *  runs `stopActiveBuild` first instead of offering them directly. */
  building: boolean
  /**
   * AN AGENT IS MID-TURN IN THERE, OF ANY KIND — deliberately wider than `building`, and a
   * separate field. `building` marks only write-capable turns; widening it would show a stop
   * button and hammer icon to someone who only asked a question, short-circuiting the escape
   * hatch that lets a pristine container be reclaimed without asking. This field carries the
   * wider sentence instead — "their agent is still working" — over a workspace already
   * reported as holding nothing to lose.
   *
   * Absent reads as false: an older backend that omits it cannot have an agent to report, and
   * defaulting the other way would falsely mark every citizen's other project busy.
   */
  agentWorking: boolean
  /**
   * WHICH REMEDY ACTUALLY WORKS (#198). `projectId`/`projectName` above name a project the
   * CALLER OWNS when this is an ordinary build occupant — `stopActiveBuild`/`release` both
   * gate on `owned_project_or_404`, which that caller satisfies. When `isSharedView` is true,
   * the occupant is a colleague's shared view and `projectId` names its OWNER instead, whom a
   * recipient never owns — those same two routes would 404 them out of their own slot. Route
   * to `giveUpSharedView` instead, which needs no project id at all.
   *
   * Absent reads as false: an older backend that omits it never produced a shared occupant.
   */
  isSharedView: boolean
}

/** Narrow a thrown error to the refusal, or `null` for anything else.
 *
 *  Branch on the CODE, never the 409 alone: the same status also carries
 *  `build_session_already_active`, which has no remedy — treating the two alike would offer a
 *  Save button for a build that is simply still running.
 *
 *  STRUCTURAL, not `instanceof`: the refusal arrives as two different error types (`ApiError`
 *  from `relaunchPreview`, `TurnStartError` from `startTurn`) sharing the same `{code, details}`
 *  shape, so keying on the shape works on whichever path the user took. */
export function asReclaimBlocked(err: unknown): ReclaimBlocked | null {
  if (!isRecord(err) || err.code !== 'sandbox_reclaim_blocked') return null
  const d = err.details
  if (!isRecord(d) || typeof d.projectId !== 'string' || typeof d.projectName !== 'string') {
    return null
  }
  return {
    projectId: d.projectId,
    projectName: d.projectName,
    dirty: typeof d.dirty === 'boolean' ? d.dirty : null,
    // Absent reads as false — an older backend that does not send the field cannot have a
    // build to report, and defaulting the other way would show the stop dialog for a project
    // nobody is building.
    building: d.building === true,
    agentWorking: d.agentWorking === true,
    isSharedView: d.isSharedView === true,
  }
}

/**
 * Give up whatever colleague's shared view currently holds the caller's OWN slot (#198,
 * requirement 24's self-service exit). No `project_id`, because a `ReclaimBlocked` naming a
 * shared occupant carries its OWNER's project — never the recipient's — so neither
 * `stopActiveBuild` nor `release` can be reached with an id that passes `owned_project_or_404`;
 * this is the one door a recipient can always use, regardless of whether they have ever built
 * anything of their own. `released: false` is a success — nothing was there to give up, or what
 * was there was the caller's own build sandbox instead (not this function's job).
 */
export async function giveUpSharedView(deps: AuthFetchDeps = {}): Promise<boolean> {
  const body = await postJson(
    `${BASE}/shared-view/release`,
    undefined,
    'Could not close that shared app',
    deps,
  )
  return isRecord(body) && body.released === true
}

/** What is (or is not) serving a project's preview right now.
 *
 *  SIX STATES, because `alive: false` used to mean all of them at once and one was an error:
 *
 *   - `alive`       — a container is serving this project; `previewUrl` is framable.
 *   - `asleep`      — built before, nothing serving it now; the next prompt restores it from
 *                     the durable copy. NOT a failure — never styled as one.
 *   - `starting`    — a build, relaunch, or turn sandbox-start is IN FLIGHT. Not `alive` (no
 *                     container yet), not `asleep` (a start is under way) — grouped with
 *                     `alive` as "just a wait", never as a "gone" state inviting a remedy.
 *   - `slot_taken`  — another of this user's projects holds the one-per-user workspace.
 *   - `never_built` — nothing has ever been built here.
 *   - `unknown`     — the server could not read its coordination store, so it claims NOTHING.
 *                     Rendering this as "gone" puts the bug back. */
export const PREVIEW_LIFE_STATES = ['alive', 'asleep', 'starting', 'slot_taken', 'never_built', 'unknown'] as const
export type PreviewLifeState = (typeof PREVIEW_LIFE_STATES)[number]

export interface PreviewState {
  state: PreviewLifeState
  /** Strictly `state === 'alive'`. Kept because the server keeps it; branch on `state`. */
  alive: boolean
  previewUrl: string | null
  /** `slot_taken` only, and null when the server could not attribute the live container to
   *  any project of this user's — naming the wrong project is worse than naming none. */
  occupyingProjectName: string | null
  /** The id behind that name, and the REMEDY's only input: "another project holds your
   *  workspace" is a dead end without something to navigate to. Goes missing WITH the name and
   *  for the same reason — the server withholds the whole attribution rather than guessing, so
   *  a surface that has one and not the other is reading a body this parser did not produce. */
  occupyingProjectId: string | null
  /** TRI-STATE, exactly like `SaveState.dirty`: `true` = the server could restore this app
   *  from the recovery copy or the saved bundle, `false` = confirmed it could not, `null` =
   *  NO CLAIM, so the UI promises nothing and keeps whatever it already knew. Two ways to
   *  reach that null and they mean the same thing to us: the object store was unreachable, or
   *  `state === 'alive'` and the poll did not ask (a running app renders no restore
   *  affordance, so the answer could not change the screen and is not worth a Blob round trip
   *  every 45 seconds). This is why `hasSavedBuild` reads it with `??` and not `||`. */
  restorable: boolean | null
}

/** Two readings that say the same thing — see `sameSaveState`. `alive` is omitted deliberately:
 *  its own docblock above pins it to `state === 'alive'`, so comparing it could only ever agree
 *  with the comparison of `state` that is already here. */
export const samePreviewState = (a: PreviewState | null, b: PreviewState | null): boolean =>
  a === b ||
  (a !== null &&
    b !== null &&
    a.state === b.state &&
    a.previewUrl === b.previewUrl &&
    a.occupyingProjectName === b.occupyingProjectName &&
    a.occupyingProjectId === b.occupyingProjectId &&
    a.restorable === b.restorable)

function asPreviewLifeState(value: unknown, alive: boolean): PreviewLifeState {
  // An unrecognised (or absent) state falls back to what `alive` can prove and NO further:
  // a live container is `alive`, and anything else is `unknown` — never a confident "gone".
  // The fallback exists for a tab that outlives a deploy, not as a normal path.
  return PREVIEW_LIFE_STATES.find((s) => s === value) ?? (alive ? 'alive' : 'unknown')
}

/** Is the preview this tab is framing still real — and if not, why?
 *
 *  A reclaimed preview is visually IDENTICAL to a working one — the last render stays on
 *  screen and a cross-origin pane cannot read a status code. Once a build ends there is no
 *  SSE and no timer left, and the teardown happens inside another project's request, so
 *  nothing can be pushed here — the tab has to ask.
 *
 *  Cheap by contract: one Redis hash read, at most two rows, at most two object-store HEADs,
 *  no container call — unlike `fetchSaveState`, which runs two `git` execs per call. */
export async function fetchPreviewState(
  projectId: string,
  deps: AuthFetchDeps = {},
): Promise<PreviewState> {
  const res = await authFetch(
    `${BASE}/projects/${encodeURIComponent(projectId)}/preview-state`,
    {},
    deps,
  )
  if (!res.ok) throw await readApiError(res, 'Could not check the preview')
  const body: unknown = await res.json().catch(() => null)
  // An unreadable body proves nothing about a container. `unknown`, not "gone" — the old
  // `{alive: false}` here was the same over-claim this whole reshape exists to remove.
  if (!isRecord(body)) {
    return {
      state: 'unknown',
      alive: false,
      previewUrl: null,
      occupyingProjectName: null,
      occupyingProjectId: null,
      restorable: null,
    }
  }
  const alive = body.alive === true
  return {
    state: asPreviewLifeState(body.state, alive),
    alive,
    previewUrl: typeof body.previewUrl === 'string' ? body.previewUrl : null,
    occupyingProjectName:
      typeof body.occupyingProjectName === 'string' ? body.occupyingProjectName : null,
    // Same discipline as the name beside it: anything that is not literally a string is
    // `null`, never coerced. A number, an object or an empty-ish value would otherwise become
    // a route the go-to action navigates into and 404s on.
    occupyingProjectId:
      typeof body.occupyingProjectId === 'string' ? body.occupyingProjectId : null,
    // Anything that is not literally a boolean stays UNKNOWN — the same rule `dirty` follows,
    // and for the same reason: coercing here is how a missing field becomes a false promise.
    restorable: typeof body.restorable === 'boolean' ? body.restorable : null,
  }
}

/**
 * What is the app compiling right now — for a tab with NO LIVE TURN.
 *
 * A turn's `compile` frame stops at the terminal, so a tab reloaded after a red turn has
 * nothing covering a broken preview and reads the framework's error screen under a
 * live-preview label; this call is the producer that outlives the turn.
 *
 * Its own call, not a field on `preview-state` (whose budget is frozen at no container call).
 * Unreadable answers `unknown` — the pane HOLDS its cover, never `clean` — and this never
 * throws: a signal about an app that may already be broken must not become a second failure.
 */
export async function fetchCompileState(
  projectId: string,
  deps: AuthFetchDeps = {},
): Promise<CompileState> {
  try {
    const res = await authFetch(
      `${BASE}/projects/${encodeURIComponent(projectId)}/compile-state`,
      {},
      deps,
    )
    if (!res.ok) return 'unknown'
    const body: unknown = await res.json().catch(() => null)
    return asCompileState(isRecord(body) ? body.state : null)
  } catch {
    return 'unknown'
  }
}

/**
 * Is the app this tab is framing still the citizen's app?
 *
 * THE TURN MAY NEVER COME: every other integrity check runs at the start of a turn, which
 * catches a reversion between messages but nothing for someone reading, in another tab, or at
 * lunch — a completion claim stays displayed for as long as the page stays open.
 *
 * ONLY A POSITIVE `reverted` MEANS ANYTHING — the server's own boolean, not derived here. Four
 * states can come back and only one may retract a claim; `state !== 'intact'` would also
 * retract on the two that mean "we could not tell", which this verdict exists to prevent.
 *
 * NEVER THROWS, answering `false` on anything unreadable — this runs on a background timer
 * beside a preview the citizen is looking at, and a probe that failed the page would be a
 * second failure caused by the check for the first one.
 */
export async function checkWorkspace(
  projectId: string,
  deps: AuthFetchDeps = {},
): Promise<boolean> {
  try {
    const res = await authFetch(
      `${BASE}/projects/${encodeURIComponent(projectId)}/workspace-check`,
      { method: 'POST' },
      deps,
    )
    if (!res.ok) return false
    const body: unknown = await res.json().catch(() => null)
    return isRecord(body) && body.reverted === true
  } catch {
    return false
  }
}

/** Is there unsaved work? Compared by COMMIT server-side, so it survives a reload and a
 *  second tab — neither of which a local dirty flag would. */
export async function fetchSaveState(
  projectId: string,
  deps: AuthFetchDeps = {},
): Promise<SaveState> {
  const res = await authFetch(
    `${BASE}/projects/${encodeURIComponent(projectId)}/save-state`,
    {},
    deps,
  )
  if (!res.ok) throw await readApiError(res, 'Could not check for unsaved work')
  const body: unknown = await res.json().catch(() => null)
  if (!isRecord(body)) throw new ApiError('The server returned a save state we could not read.', 500)
  return {
    appId: typeof body.appId === 'string' ? body.appId : null,
    // Anything that is not literally a boolean stays UNKNOWN. Coercing here is exactly how a
    // missing field becomes a confident "all saved".
    dirty: typeof body.dirty === 'boolean' ? body.dirty : null,
    containerHead: typeof body.containerHead === 'string' ? body.containerHead : null,
    savedHead: typeof body.savedHead === 'string' ? body.savedHead : null,
    // Whitelisted one at a time like its siblings, and narrowed the same way: anything that is
    // not literally a string is `null`. Defaulting the other way is not available here — a
    // fabricated instant would tell a citizen their work can be brought back on the strength of
    // a field the server never sent.
    recoveryAt: typeof body.recoveryAt === 'string' ? body.recoveryAt : null,
  }
}
