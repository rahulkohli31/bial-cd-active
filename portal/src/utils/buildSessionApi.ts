/**
 * Typed client for the C3 build-session control API (`/api/build-sessions*`), the
 * portal's FIRST build-session control surface. Mirrors `projectApi.ts` exactly:
 * every call is `fn(args, deps = {})`, forwards `deps` to `authFetch` (cookie
 * session + one 401-refresh retry), and the edge rewrites `/api/*` → `/v1/*`.
 *
 * Wire format is camelCase (C3 `CamelModel`). Response bodies are untrusted network
 * input: they arrive as `unknown` and are narrowed with `toX()` guards — never cast,
 * never `any`. Every non-2xx becomes an `ApiError` (via `readApiError`) so callers
 * branch on `.status` / `.code` (409 / 403) instead of re-parsing envelopes.
 *
 * CSRF (KTD-2): `start` / `stop` / `forceEnd` are mutating POSTs and carry the
 * signed double-submit token (`X-CSRF-Token`, reusing `auth.js` `getCsrfToken()`);
 * `getStatus` GET and the SSE GET (a separate transport, `buildSessionEvents.ts`)
 * are safe methods and carry NO token. This is net-new: no prior business route in
 * the portal enforces CSRF (`api.js` names itself "the seam to add it to").
 */
import { ApiError, extractApiCode, extractApiMessage, isRecord, readApiError } from './apiError'
import { authFetch } from './api'
import { asCompileState } from './compileState'
import type { CompileState } from './compileState'
import { getCsrfToken } from './auth'
import type {
  BuildSessionStatus,
  BuildSessionStatusResponse,
  ForceEndResponse,
  RelaunchPreviewRequest,
  RelaunchPreviewResponse,
  StartBuildRequest,
  StartBuildResponse,
  StopBuildRequest,
  StopBuildResponse,
} from './buildSessionTypes'

/**
 * The dep bundle `authFetch` accepts, injectable so tests need no real network.
 * Derived straight from `authFetch`'s own JSDoc typedef so it can never drift
 * (identical to `projectApi.ts`'s `AuthFetchDeps`).
 */
export type AuthFetchDeps = NonNullable<Parameters<typeof authFetch>[2]>

// ─── Frozen TTL + cadence constants (mirror C3 §3) ───────────────────────────
// Re-exported here so U4's lifecycle hook keeps ONE source of truth and never
// invents its own intervals.

/** Lock auto-expires if not renewed (15 min) — a crashed session can't hold the sandbox forever. */
export const LOCK_TTL_SECONDS = 900
/** Heartbeat key TTL (3× cadence) — tolerate two missed beats before the reaper treats the session as idle. */
export const HEARTBEAT_TTL_SECONDS = 90

// THE TWO CLIENT CADENCES ARE GONE, along with `renewLock` and `heartbeat` themselves. U13
// deleted the blind keep-alive loop that was their only caller, and a client function with no
// caller is not neutral: it reads as a supported way to keep a session alive, and the next
// person needing one would have wired the loop straight back. What holds a turn open now is the
// R10 wall-clock lease the SERVER renews (U12) — legible to a sweep in another process, which a
// browser timer never was.
//
// The backend routes stay. They are the operator surface and the supervisor's, and nothing about
// deleting a browser client says anything about them.

const BASE = '/api/build-sessions'
const JSON_HEADERS = { 'Content-Type': 'application/json' }

/**
 * Thrown by `start` (and `relaunchPreview`) on a `409 build_session_already_active`,
 * carrying the EXISTING session's id so the caller can `getStatus` it and decide,
 * by comparing projectIds, between re-attach (same project) and block (cross-project)
 * — the `409` alone is not a self-describing discriminator (U5 identity model).
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

function toStartBuildResponse(value: unknown): StartBuildResponse {
  if (!isRecord(value)) throw new ApiError('The server returned a build session we could not read.', 500)
  return {
    sessionId: requireSessionId(value),
    projectId: requireProjectId(value),
    appId: asString(value.appId),
    status: toBuildSessionStatus(value.status),
    previewUrl: asStringOrNull(value.previewUrl),
    createdAt: asString(value.createdAt),
  }
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

function toForceEndResponse(value: unknown): ForceEndResponse {
  if (!isRecord(value)) throw new ApiError('The server returned a build session we could not read.', 500)
  return { sessionId: requireSessionId(value), status: toBuildSessionStatus(value.status) }
}

// ─── request plumbing ────────────────────────────────────────────────────────

/** The double-submit CSRF header for a mutating POST, or `{}` when no csrf cookie is readable (parity with `auth.js`). */
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
 * A mutating POST with CSRF. `body === undefined` sends no JSON body (`forceEnd` — the one
 * surviving lock op — takes none, C3 §3). A non-2xx becomes an `ApiError`, EXCEPT a
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

// ─── control operations (C3 §2) ─────────────────────────────────────────────

/**
 * `start` — create a build session for a project. 409 → `BuildSessionAlreadyActiveError` (carries
 * the existing id).
 *
 * `conversationId` is sent ONLY when supplied (mirroring `stop`'s optional `reason`): the field is
 * an additive, optional amendment to the frozen C3 body, so omitting it must produce the exact
 * pre-R3 request. When sent, the server grounds the build in that thread's attachments.
 */
export async function start(args: StartBuildRequest, deps: AuthFetchDeps = {}): Promise<StartBuildResponse> {
  const payload: Record<string, string> = { projectId: args.projectId, prompt: args.prompt }
  if (args.conversationId !== undefined) payload.conversationId = args.conversationId
  const body = await postJson(BASE, payload, 'Failed to start build session', deps)
  return toStartBuildResponse(body)
}

/**
 * `relaunch` — restore a project's saved app into a fresh, ready sandbox and get its live URL (#43).
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

// ─── lock operations (C3 §3) ─────────────────────────────────────────────────
//
// `acquireLock` and `releaseLock` are GONE (U28): nothing called them — the portal's blind
// keep-alive loop that was their only caller was itself deleted back in U13, same as
// `renewLock` and `heartbeat` before them (see the note above). `forceEnd` is the one lock
// op still reachable from the UI, fed by relaunch's 409.

/** `force-end` — the owner-only kill switch (`kill_switch()`), regardless of in-flight state. A non-owner → `403 build_session_forbidden`. */
export async function forceEnd(sessionId: string, deps: AuthFetchDeps = {}): Promise<ForceEndResponse> {
  const body = await postJson(`${BASE}/${encodeURIComponent(sessionId)}/lock/force-end`, undefined, 'Failed to force-end the build session', deps)
  return toForceEndResponse(body)
}

/**
 * The dependency bag the C3 client + event feed accept, so U4's hook and U5's page
 * can swap in the scripted mock (dev/test) or the real transport (prod default).
 * The client half is the `buildSessionApi` module surface; the feed half is the
 * `EventSource` factory (`buildSessionEvents.ts`).
 */
export interface BuildSessionClient {
  start: typeof start
  relaunchPreview: typeof relaunchPreview
  stop: typeof stop
  getStatus: typeof getStatus
  forceEnd: typeof forceEnd
}

/** The real, wired-by-default client (this track merges after SESSION-API, so no swap is needed at merge — KTD-6). */
export const buildSessionClient: BuildSessionClient = {
  start,
  relaunchPreview,
  stop,
  getStatus,
  forceEnd,
}

// --- the save model (U5b / KTD-5e) ---------------------------------------------------------

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
}

/** Two readings that say the same thing. Every field is a primitive, so this is exact rather
 *  than an approximation — and it exists so a poll that keeps reporting the same answer stops
 *  handing consumers a new object to re-render for. */
export const sameSaveState = (a: SaveState | null, b: SaveState | null): boolean =>
  a === b ||
  (a !== null &&
    b !== null &&
    a.appId === b.appId &&
    a.dirty === b.dirty &&
    a.containerHead === b.containerHead &&
    a.savedHead === b.savedHead)

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

/**
 * WHAT A STOP ACHIEVED — THREE NAMED STATES, never a boolean (plan 002, U9).
 *
 * The boolean this replaces said the wrong thing twice over: the server hardcoded success on both
 * branches, while its own docstrings promised a timeout would read as "still running". So the one
 * answer a caller must never act on — a stop that has NOT finished — arrived wearing the same face
 * as one that had. And `false` already meant "nothing was running", which is a success the caller
 * proceeds on, so a timeout folded into it would take a container out from under a task still
 * writing to it.
 *
 *   `stopped`              proceed. Something was running and has finished unwinding.
 *   `nothing_was_running`  proceed. There was nothing to stop.
 *   `still_running`        DO NOT PROCEED. The wait expired, or something holds the app anyway.
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
 * NOTHING HOLDS A REQUEST OPEN FOR THE LENGTH OF A STOP any more, and that is what removed a
 * dependency nobody could satisfy: the old shape's budget had to sit under the request timeout of
 * the gateway in front of the service, a number recorded nowhere in this repo and owned by the
 * client's network.
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
 * HOW OFTEN TO ASK, AND FOR HOW LONG. Ordinary tuning, chosen with the code open, and neither
 * number constrains anything: the read is cheap, nothing is held open, and the ceiling only
 * decides when the dialog stops saying "closing…" and starts saying it could not.
 *
 * THE CEILING IS DELIBERATELY BELOW THE SERVER'S OWN STOP BUDGET, which is the opposite of what
 * this said when the two were closer together. That budget is derived from the work a stop
 * actually waits on, and the derivation now includes the snapshot a build's unwind writes — four
 * bounded steps of two minutes each — which puts it a little over eight minutes. Nobody should be
 * held in front of a modal for eight minutes to learn whether they may switch projects, so the
 * browser stops WAITING first, at two.
 *
 * WHICH COSTS NOTHING, BECAUSE EXPIRING HERE IS NOT A VERDICT. The stop runs as a detached task
 * server-side; the state read is the authority and is idempotent; and the hand-over proceeds on
 * nothing but a settled answer. So reaching this ceiling ends the WAIT, not the stop, and a
 * second press picks it up wherever it has got to. What must not be said at this point is that
 * anything failed — the likeliest reason for passing two minutes is a large app being packed up
 * exactly as it should be, which is why the sentence in `handOverWorkspace` says so.
 */
const STOP_POLL_MS = 1200
const STOP_CEILING_MS = 120_000
/**
 * HOW LONG ONE READ MAY HANG BEFORE IT IS ABANDONED — and why this is a REAL timer and not the
 * injected clock's.
 *
 * The ceiling below is checked BETWEEN iterations, so it can only fire if each iteration actually
 * returns. `authFetch` sets no timeout of its own: a connection that opens and then stalls never
 * settles, the `while` never re-evaluates, and the two-minute ceiling silently becomes forever —
 * with `ReclaimWorkspaceDialog` holding Escape and the overlay click disabled the whole time. So
 * every read is abandoned on its own deadline and retried, which is the same treatment a dropped
 * connection already gets: a read that gave up decided nothing.
 *
 * It uses `setTimeout` rather than `clock.sleep` deliberately. The injected clock exists so a test
 * can reach the ceiling without waiting two minutes, and its `sleep` resolves immediately — racing
 * a read against an instant sleep would abandon every read in every test. This bound is about a
 * socket, not about pacing, so it belongs on the real timer either way.
 */
const STOP_READ_TIMEOUT_MS = 15_000

/**
 * Wait for a stop to genuinely finish, narrating while it does.
 *
 * IT POLLS THE STATE RATHER THAN WATCHING A CLOCK, which is the whole of the fix. A container
 * declared dead when it was merely slow has destroyed unsaved work in this repo before, precisely
 * because a timeout was read as a verdict.
 *
 * A DROPPED CONNECTION IS NOT A VERDICT EITHER. A failed read is retried until the ceiling rather
 * than treated as "still running for ever" — the browser losing the network is not evidence about
 * the other project's turn — and the caller can simply ask again afterwards, because the stop
 * itself is running server-side and the state read is idempotent.
 *
 * BUT A DECIDED ANSWER IS NOT A BLIP, AND RETRYING ONE IS ITS OWN DEFECT (review #38). A session
 * that expired mid-hand-over answers 401 to every read; `authFetch` attempts a refresh on each of
 * them, so the dialog sat on "Closing the other app…" for two minutes issuing a hundred reads and
 * a hundred refresh attempts — the very traffic the refresh path documents as tripping
 * reuse-detection — and then answered with the ceiling's own sentence, which describes the other
 * project still packing its work away and had nothing to do with what actually failed. A
 * 403 (or a 404 from a project deleted in another tab) behaved the same way. Those statuses are
 * answers the poll cannot change, so they travel straight out to the dialog, which says the true
 * thing immediately. Everything else — an abort, a dropped socket, a 5xx — is still a blip.
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

/** Hand the workspace over: STOP, then optionally SAVE, then RELEASE — the whole of what the
 *  #83 refusal dialog's buttons do to the server, in the one order that works.
 *
 *  THE ORDER IS THE DESIGN, and it lives here rather than in each caller because it is an
 *  invariant of these three endpoints, not of any one surface. Save and release BOTH refuse
 *  while an agent is writing, so a sequence that saved first would simply fail — and a save
 *  that slipped past that guard would bundle a tree caught mid-edit as the version Relaunch
 *  later restores. Stopping settles the turn (terminal frame, billing, `finish_turn_sandbox`)
 *  and only then is there a coherent tree to save.
 *
 *  THE STOP IS UNCONDITIONAL, not gated on `ReclaimBlocked.building`. `building` describes what
 *  to SAY, not what to do: it is true only for a Write turn, because that is the one whose
 *  interruption costs the user something. But an Ask or Plan turn holds the container just as
 *  firmly — every mode pins it — and `release` refuses for either, so gating the stop on it left
 *  the other modes in the dead end this flow exists to remove: a dialog whose buttons the server
 *  declines. Stopping when nothing is running is free and says so.
 *
 *  AND IT WAITS FOR THE STOP TO GENUINELY FINISH (plan 002, U9). The ask returns immediately now;
 *  the state read is the authority, and this proceeds only on one of the two settled answers. A
 *  stop that times out is reported as still running and the transfer DOES NOT PROCEED — which is
 *  the difference between a clean stop and a timeout that this repo has shipped confused before.
 *
 *  REJECTS RATHER THAN SWALLOWS. A failed save must not be followed by a release — that is
 *  precisely the data loss the dialog exists to prevent — so the rejection travels back to the
 *  caller, which is the only thing still mounted that can report it. */
export async function handOverWorkspace(
  projectId: string,
  save: boolean,
  deps: AuthFetchDeps = {},
  narrate: (step: HandoverStep) => void = () => {},
  clock: StopWaitClock = REAL_CLOCK,
): Promise<void> {
  narrate('stopping')
  const asked = await stopActiveBuild(projectId, deps)
  const settled = stopSettled(asked) ? asked : await awaitStopSettled(projectId, deps, clock)
  if (!stopSettled(settled)) {
    // NOT AN ERROR OF OURS, AND NOT A REASON TO TAKE THE CONTAINER. Everything the citizen has is
    // still where it was; what failed is the wait, and asking again is the remedy.
    throw new ApiError(
      // THE TRUE THING AT TWO MINUTES, which is not "that project failed" (review #45). The
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
 * IT STOPS AT `starting`, AND THAT IS THE WHOLE SEQUENCE (review #39/#82). There was an `opening`
 * member here for the chat being opened, with copy already written for it, and nothing could ever
 * produce it: the retry's last act is a navigate, which unmounts the surface publishing the dialog
 * in the very commit that would have carried the new step, so no render can reach it. The wait it
 * was meant to describe is the destination's own — the chat surface and the app pane both narrate
 * themselves as they come up — and a member no writer can set is dead code with copy attached.
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
   * AN AGENT IS MID-TURN IN THERE, OF ANY KIND (plan 002, U9) — deliberately wider than
   * `building`, and deliberately a SEPARATE field.
   *
   * `building` marks only turns whose toolset can WRITE, and the server records why: widening
   * that one put a stop button and a hammer icon in front of someone who had only asked a
   * question, and short-circuited the escape hatch that lets a pristine container be reclaimed
   * without asking. This is the wide answer, for a different sentence — "their agent is still
   * working, and transferring will stop it" — which the dialog has to be able to say over a
   * workspace it has just reported as holding nothing to lose.
   *
   * Absent reads as false: an older backend that does not send it cannot have an agent to report,
   * and defaulting the other way would tell every citizen their other project is busy.
   */
  agentWorking: boolean
}

/** Narrow a thrown error to the #83 refusal, or `null` for anything else.
 *
 *  Branch on the CODE, never the 409 alone: the same status also carries
 *  `build_session_already_active`, which has no remedy the user can act on, and treating the
 *  two alike would offer a Save button for a build that is simply still running.
 *
 *  STRUCTURAL, not `instanceof`, because the refusal arrives as two different error types —
 *  `ApiError` from `relaunchPreview`, `TurnStartError` from `startTurn` — and both carry the
 *  same `{code, details}` shape. Keying on the shape means the modal works on whichever path
 *  the user happened to take. */
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
  }
}

/** What is (or is not) serving a project's preview right now — C3 §8.3.
 *
 *  FIVE STATES AND AN UNKNOWN, because `alive: false` used to mean all five at once and one
 *  of them was not a state at all but an error:
 *
 *   - `alive`       — a container is serving this project; `previewUrl` is framable.
 *   - `asleep`      — built before, nothing serving it now. The next prompt brings it back
 *                     from the durable copy. NOT a failure, and nothing may style it as one.
 *   - `starting`    — U13: a build, a relaunch, or a turn's sandbox start is IN FLIGHT for this
 *                     project right now. Not `alive` (no container yet) and not `asleep` (a
 *                     start is actively under way) — the server's own action mapping (C3 §10.3)
 *                     groups it with `alive` as "nothing to offer, just a wait", so it must NOT
 *                     be treated as a "gone" state that invites a remedy.
 *   - `slot_taken`  — another of this user's projects holds the one-per-user workspace.
 *   - `never_built` — nothing has ever been built here.
 *   - `unknown`     — the server could not read its coordination store, so it claims NOTHING.
 *                     A client that renders this as "gone" has put the bug back. */
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
   *  `state === 'alive'` and the poll did not ask (C3 §8.3 — a running app renders no restore
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

/** Is the preview this tab is framing still real — and if not, why? (#83, C3 §8.3.)
 *
 *  A reclaimed preview is visually IDENTICAL to a working one — the last render stays on
 *  screen, the iframe reports nothing, and a cross-origin pane cannot read a status code. Once
 *  a build ends there is no SSE and no timer left, and the teardown happens inside another
 *  project's request, so nothing can be pushed here. The tab has to ask.
 *
 *  Cheap by contract (C3 §8.3): one Redis hash read, at most two rows, at most two object-store
 *  HEADs, and no container call at all — unlike `fetchSaveState`, which runs two `git` execs
 *  inside the container per call. */
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

/** Is there unsaved work? Compared by COMMIT server-side, so it survives a reload and a
 *  second tab — neither of which a local dirty flag would. */
/**
 * What is the app compiling right now — for a tab with NO LIVE TURN (R17/R18).
 *
 * During a turn the state arrives on the turn stream as a `compile` frame. That producer stops
 * at the terminal, so a tab that reloads after a red turn has nothing to cover a broken preview
 * with: the pane comes up uncovered and the citizen reads the framework's error screen under a
 * live-preview label. This is the producer that outlives the turn.
 *
 * Deliberately its own call rather than a field on `preview-state`, whose cost budget is frozen
 * at no container call of any kind (C3 §8.3). Anything unreadable answers `unknown`, which the
 * pane HOLDS its cover on — never `clean`. It never throws for the same reason: this is a
 * signal about an app that may already be broken, and it must not become a second failure.
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
 * Is the app this tab is framing still the citizen's app? (U4, R4/R7.)
 *
 * THE TURN MAY NEVER COME. Every other integrity check runs at the start of a turn, which catches
 * every reversion between one message and the next — and nothing at all for someone who is
 * reading, or in another tab, or at lunch. The "Build complete — your app is live below" claim
 * goes on being displayed for as long as the page stays open.
 *
 * ONLY A POSITIVE `reverted` MEANS ANYTHING, and it is the server's boolean rather than something
 * derived here. Four states can come back and only one of them may retract a completion claim;
 * writing `state !== 'intact'` on this side would retract on the two that mean "we could not
 * tell", which is the mistake the whole verdict is shaped to prevent.
 *
 * NEVER THROWS, and answers `false` on anything unreadable. This runs on a background timer
 * beside a preview the citizen is looking at: a probe that could fail the page would be a second
 * failure caused by the check for the first one.
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
  }
}
