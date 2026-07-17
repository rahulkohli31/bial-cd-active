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
import { authFetch } from './api.js'
import { getCsrfToken } from './auth.js'
import type {
  BuildSessionStatus,
  BuildSessionStatusResponse,
  ForceEndResponse,
  HeartbeatResponse,
  LockReleaseResponse,
  LockStateResponse,
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
    throw new ApiError(message, res.status, code)
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
  stop,
  getStatus,
  acquireLock,
  renewLock,
  releaseLock,
  forceEnd,
  heartbeat,
}
