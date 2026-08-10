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
 * CSRF (KTD-2): `start` / `stop` / all lock ops are mutating POSTs and carry the
 * signed double-submit token (`X-CSRF-Token`, reusing `auth.js` `getCsrfToken()`);
 * `getStatus` GET and the SSE GET (a separate transport, `buildSessionEvents.ts`)
 * are safe methods and carry NO token. This is net-new: no prior business route in
 * the portal enforces CSRF (`api.js` names itself "the seam to add it to").
 */
import { ApiError, extractApiCode, extractApiMessage, isRecord, readApiError } from './apiError'
import { authFetch } from './api'
import { getCsrfToken } from './auth'
import type {
  BuildSessionStatus,
  BuildSessionStatusResponse,
  ForceEndResponse,
  HeartbeatResponse,
  LockReleaseResponse,
  LockStateResponse,
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
/** Client renews at ⅓ of the TTL (5 min) — two renews of head-room before expiry. */
export const LOCK_RENEW_CADENCE_SECONDS = 300
/** Client heartbeats every 30 s while the tab is open. */
export const HEARTBEAT_CADENCE_SECONDS = 30
/** Heartbeat key TTL (3× cadence) — tolerate two missed beats before the reaper treats the session as idle. */
export const HEARTBEAT_TTL_SECONDS = 90

const BASE = '/api/build-sessions'
const JSON_HEADERS = { 'Content-Type': 'application/json' }

/**
 * Thrown by `start` (and `acquireLock`) on a `409 build_session_already_active`,
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

function asNumber(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0
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

function toLockStateResponse(value: unknown): LockStateResponse {
  if (!isRecord(value)) throw new ApiError('The server returned a lock state we could not read.', 500)
  return {
    sessionId: requireSessionId(value),
    held: value.held === true,
    ownerUserId: asString(value.ownerUserId),
    ttlSeconds: asNumber(value.ttlSeconds),
    expiresAt: asString(value.expiresAt),
  }
}

function toLockReleaseResponse(value: unknown): LockReleaseResponse {
  if (!isRecord(value)) throw new ApiError('The server returned a lock state we could not read.', 500)
  return { sessionId: requireSessionId(value), released: value.released === true }
}

function toHeartbeatResponse(value: unknown): HeartbeatResponse {
  if (!isRecord(value)) throw new ApiError('The server returned a heartbeat we could not read.', 500)
  return {
    sessionId: requireSessionId(value),
    alive: value.alive === true,
    cadenceSeconds: asNumber(value.cadenceSeconds),
    heartbeatExpiresAt: asString(value.heartbeatExpiresAt),
  }
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
 * A mutating POST with CSRF. `body === undefined` sends no JSON body (the lock ops
 * and heartbeat take none, C3 §3). A non-2xx becomes an `ApiError`, EXCEPT a
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

/** `acquire` — take the one-per-user lock explicitly. 409 → `BuildSessionAlreadyActiveError`. */
export async function acquireLock(sessionId: string, deps: AuthFetchDeps = {}): Promise<LockStateResponse> {
  const body = await postJson(`${BASE}/${encodeURIComponent(sessionId)}/lock/acquire`, undefined, 'Failed to acquire the build lock', deps)
  return toLockStateResponse(body)
}

/** `renew` — push `expiresAt` out by the TTL. Renewing a lock you no longer hold → `409 build_session_lock_lost`. */
export async function renewLock(sessionId: string, deps: AuthFetchDeps = {}): Promise<LockStateResponse> {
  const body = await postJson(`${BASE}/${encodeURIComponent(sessionId)}/lock/renew`, undefined, 'Failed to renew the build lock', deps)
  return toLockStateResponse(body)
}

/** `release` — graceful release after a clean stop. Idempotent. */
export async function releaseLock(sessionId: string, deps: AuthFetchDeps = {}): Promise<LockReleaseResponse> {
  const body = await postJson(`${BASE}/${encodeURIComponent(sessionId)}/lock/release`, undefined, 'Failed to release the build lock', deps)
  return toLockReleaseResponse(body)
}

/** `force-end` — the owner-only kill switch (`kill_switch()`), regardless of in-flight state. A non-owner → `403 build_session_forbidden`. */
export async function forceEnd(sessionId: string, deps: AuthFetchDeps = {}): Promise<ForceEndResponse> {
  const body = await postJson(`${BASE}/${encodeURIComponent(sessionId)}/lock/force-end`, undefined, 'Failed to force-end the build session', deps)
  return toForceEndResponse(body)
}

/** `heartbeat` — the portal's liveness ping. Heartbeating a session you don't own → `404`. */
export async function heartbeat(sessionId: string, deps: AuthFetchDeps = {}): Promise<HeartbeatResponse> {
  const body = await postJson(`${BASE}/${encodeURIComponent(sessionId)}/heartbeat`, undefined, 'Failed to send the heartbeat', deps)
  return toHeartbeatResponse(body)
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
  acquireLock: typeof acquireLock
  renewLock: typeof renewLock
  releaseLock: typeof releaseLock
  forceEnd: typeof forceEnd
  heartbeat: typeof heartbeat
}

/** The real, wired-by-default client (this track merges after SESSION-API, so no swap is needed at merge — KTD-6). */
export const buildSessionClient: BuildSessionClient = {
  start,
  relaunchPreview,
  stop,
  getStatus,
  acquireLock,
  renewLock,
  releaseLock,
  forceEnd,
  heartbeat,
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

/** Stop whatever the agent is doing in this project, and wait for it to settle.
 *
 *  The FIRST of the three steps behind "stop and switch" — stop, then save, then release —
 *  and the reason the other two work at all: both refuse while a session is live, so a dialog
 *  that skipped this offered two buttons the server declined.
 *
 *  `false` means nothing was running, which is a success to proceed on rather than a miss.
 *  A `true` is NOT a promise that the slot is free: the server's wait is bounded, so the
 *  authority on that is the next step's own refusal. Do not branch on it — just carry on and
 *  let save/release speak for themselves. */
export async function stopActiveBuild(
  projectId: string,
  deps: AuthFetchDeps = {},
): Promise<boolean> {
  const body = await postJson(
    `${BASE}/projects/${encodeURIComponent(projectId)}/stop-active-build`,
    undefined,
    'Could not stop the build in the other project',
    deps,
  )
  return isRecord(body) && body.stopped === true
}

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
  }
}

export interface PreviewState {
  alive: boolean
  previewUrl: string | null
}

/** Is the preview this tab is framing still real? (#83, second half.)
 *
 *  A reclaimed preview is visually IDENTICAL to a working one — the last render stays on
 *  screen, the iframe reports nothing, and a cross-origin pane cannot read a status code. Once
 *  a build ends there is no SSE and no timer left, and the teardown happens inside another
 *  project's request, so nothing can be pushed here. The tab has to ask.
 *
 *  One Redis hash read server-side — cheap enough to sit on a timer, unlike `fetchSaveState`,
 *  which runs two `git` execs inside the container per call. */
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
  if (!isRecord(body)) return { alive: false, previewUrl: null }
  return {
    alive: body.alive === true,
    previewUrl: typeof body.previewUrl === 'string' ? body.previewUrl : null,
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
  }
}
