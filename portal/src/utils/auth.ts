/**
 * Frontend auth/session store — COOKIE-session semantics (Entra ID via the
 * FastAPI control-plane). This is the cookie migration the file's old header
 * anticipated.
 *
 * The SPA holds NO tokens: the session JWT, refresh token, and CSRF token live in
 * cookies (session/refresh HttpOnly + host-only; csrf readable by JS) that the
 * browser attaches automatically. Auth state derives from a ONCE-CACHED
 * `GET /auth/me` — the "session context" — re-fetched only on bootstrap or an
 * explicit invalidate (after login/logout/refresh). A server-side revoke surfaces
 * on the next refresh/mutation 401.
 *
 * The legacy `getAccessToken`/`refreshAccessToken` exports remain as documented
 * shims so not-yet-migrated Express (Bearer) call sites still compile — there is
 * no bearer token in the cookie model, so `getAccessToken` returns null and those
 * Express calls 401 until each API migrates to the cookie session (KD-10).
 */

// Relative path: the vite dev proxy (and the production edge) route /api/v1/auth/*
// to the FastAPI control-plane, stripping the /api prefix (KD-8).
const AUTH_API = '/api/v1/auth'

/** Mirrors the backend's `ProfileLimits` (`backend/src/api/v1/auth/schemas.py`) —
 * the user's EFFECTIVE limits, camelCase on the wire. */
export interface ProfileLimits {
  dailyTokenLimit: number
  contextSoftLimit: number
  contextHardLimit: number
}

/** Mirrors the backend's `ChatKindInfo` (`backend/src/api/v1/auth/schemas.py`) — one entry
 * in the U16/R73 catalogue of what a chat kind IS: its wire value, its display name, and the
 * one line a citizen reads about what it does. `utils/chatKind.ts` is the only module that
 * reads this array; nothing else should hold a literal chat-kind name or description. */
export interface ChatKindInfo {
  value: string
  name: string
  description: string
}

/** Mirrors the backend's `UserProfile` — deliberately snake_case on the wire
 * (`display_name`/`is_admin` are the SPA contract, per the schema's own doc
 * comment) plus the client-added camelCase `isAdmin` mirror (`fetchMe`). */
export interface UserProfile {
  id: string
  email: string
  display_name: string | null
  is_admin: boolean
  isAdmin: boolean
  limits: ProfileLimits
  chat_kinds: ChatKindInfo[]
}

/** Full-page navigation target for "Sign in with Microsoft" (handled by FastAPI). */
export const LOGIN_URL = `${AUTH_API}/login`

const SIGNOUT_REASON_KEY = 'bial_signout_reason'
const LOCK_NAME = 'bial_token_refresh'

// Bound every auth round-trip. A hung control-plane must not (a) hold the
// cross-tab Web Lock during a silent refresh and thereby wedge every other tab's
// refresh, nor (b) leave the RequireAuth bootstrap spinner spinning forever. The
// 10s timeout surfaces as an AbortError caught by each caller's existing catch,
// failing exactly like any other network error. Guarded so environments without
// AbortSignal.timeout (older/test runtimes) simply omit the bound.
const AUTH_FETCH_TIMEOUT_MS = 10_000
function authFetchTimeout(): AbortSignal | undefined {
  return typeof AbortSignal !== 'undefined' && AbortSignal.timeout
    ? AbortSignal.timeout(AUTH_FETCH_TIMEOUT_MS)
    : undefined
}

// Pre-cookie localStorage keys, purged ONCE on first bootstrap so a stale Bearer
// token from before the migration can't linger.
const LEGACY_KEYS = ['bial_access_token', 'bial_refresh_token', 'bial_user']

// Valid one-time signout reasons (drive the login banner copy).
export const SIGNOUT_REASONS = {
  EXPIRED: 'session_expired',
  LOGGED_OUT: 'logged_out',
  SUSPENDED: 'account_suspended',
} as const

export type SignoutReason = (typeof SIGNOUT_REASONS)[keyof typeof SIGNOUT_REASONS]

// Where a mid-session suspension bounces the user. The login screen keys its
// banner off this exact `?authError` value (LoginPage AUTH_ERROR_BANNERS).
const SUSPENDED_LOGIN_URL = '/login?authError=account_suspended'

let legacyPurged = false
function purgeLegacyTokensOnce(): void {
  if (legacyPurged) return
  legacyPurged = true
  try {
    LEGACY_KEYS.forEach((k) => localStorage.removeItem(k))
  } catch {
    // best-effort — private-mode / disabled storage
  }
}

// --- cached session context (once-cached GET /auth/me) -----------------------

let sessionPromise: Promise<UserProfile | null> | null = null // in-flight or resolved bootstrap promise
let cachedUser: UserProfile | null = null // last known profile, or null when signed out

async function fetchMe(): Promise<UserProfile | null> {
  const req = () =>
    fetch(`${AUTH_API}/me`, {
      credentials: 'include',
      headers: { Accept: 'application/json' },
      signal: authFetchTimeout(),
    })
  try {
    let res = await req()
    if (res.status === 401) {
      // The short-lived session JWT expired — try a silent cookie refresh, then
      // retry /me ONCE. Only a dead refresh cookie logs the user out.
      if (await refreshAccessToken()) res = await req()
    }
    if (!res.ok) {
      cachedUser = null
      return null
    }
    // UNCHECKED (matches pre-migration behavior): asserted against the verified
    // backend contract (backend/src/api/v1/auth/schemas.py's UserProfile), not
    // re-validated here.
    const profile = (await res.json().catch(() => null)) as UserProfile | null
    // `/auth/me` returns the snake-cased `is_admin`; expose it as the camelCase `isAdmin`
    // the UI reads (Navbar admin link + AdminPage gate). Fail-closed to false.
    if (profile) profile.isAdmin = profile.is_admin === true
    cachedUser = profile
    return cachedUser
  } catch {
    cachedUser = null // network blip on bootstrap — treat as signed out
    return null
  }
}

/**
 * Resolve the session ONCE and cache it. Ordinary navigations reuse the cache
 * (no refetch, no spinner); only the initial bootstrap or an explicit
 * invalidate/clear re-hits /me. Also purges any pre-cookie Bearer tokens.
 */
export function bootstrapSession(): Promise<UserProfile | null> {
  purgeLegacyTokensOnce()
  if (!sessionPromise) sessionPromise = fetchMe()
  return sessionPromise
}

/** Forget the cached session so the next bootstrap re-fetches /me. */
export function invalidateSession(): void {
  sessionPromise = null
  cachedUser = null
}

/** The cached profile ({ id, email, display_name }) or null. Sync. */
export function getStoredUser(): UserProfile | null {
  return cachedUser
}

/** Sync best-effort auth check — true once a session context is cached. */
export function isAuthenticated(): boolean {
  return cachedUser != null
}

// --- CSRF (non-HttpOnly cookie -> X-CSRF-Token header) -----------------------

// Exported so the first business-route client that enforces double-submit CSRF —
// the C3 build-session control API (`buildSessionApi.ts`) — reuses this exact
// cookie read instead of re-implementing it (ADR-0007; ORIG-§5 reuse-don't-reimplement).
// Additive: `auth.js`'s own `doRefresh`/`logout` still call it unchanged.
export function getCsrfToken(): string | null {
  try {
    const match = document.cookie.match(/(?:^|;\s*)(?:__Host-)?csrf=([^;]+)/)
    return match ? decodeURIComponent(match[1]) : null
  } catch {
    return null
  }
}

// --- signout-reason banner (one-time) ----------------------------------------

/** Drop the client session; optionally record a one-time login-banner reason. */
export function clearSession(reason?: SignoutReason): void {
  invalidateSession()
  if (reason) {
    try {
      localStorage.setItem(SIGNOUT_REASON_KEY, reason)
    } catch {
      // best-effort
    }
  }
}

export function consumeSignoutReason(): string | null {
  try {
    const reason = localStorage.getItem(SIGNOUT_REASON_KEY)
    if (reason) localStorage.removeItem(SIGNOUT_REASON_KEY)
    return reason
  } catch {
    return null
  }
}

// --- mid-session suspension (single-flight hard bounce to login) -------------

// The full-page navigation, isolated behind one indirection so jsdom tests can
// stub it (`window.location.assign` throws "Not implemented: navigation" in
// jsdom). A hard navigation — not react-router — because the callers (authFetch,
// fetchClaudeStream) have no router context and we WANT every in-flight page torn
// down, not a soft in-SPA transition that leaves stale trees mounted.
function hardRedirect(url: string): void {
  window.location.assign(url)
}

// A suspended user's page usually has several requests in flight; each one 403s
// and calls handleSuspendedSession. This latch makes the teardown single-flight
// so they produce exactly ONE navigation. It never resets — once we're bouncing
// to /login the whole page is being discarded anyway.
let alreadyBouncing = false

/**
 * Mid-session suspension teardown. An admin deactivated this user while they were
 * signed in, so the control-plane now answers every authed request with
 * `403 {"detail":"Account suspended"}`. Drop the cached session, record why, and
 * hard-navigate to the login screen's (non-alarming) suspension banner.
 *
 * Idempotent / single-flight: concurrent 403s from several in-flight requests
 * produce exactly one navigation. Lives here (not in api.js) so `authFetch` and
 * `fetchClaudeStream` — which does NOT go through `authFetch` — share one path.
 */
export function handleSuspendedSession(): void {
  if (alreadyBouncing) return
  alreadyBouncing = true
  clearSession(SIGNOUT_REASONS.SUSPENDED)
  hardRedirect(SUSPENDED_LOGIN_URL)
}

// --- silent refresh (cookie-based, cross-tab single-flight) ------------------

let inflight: Promise<true | null> | null = null

/**
 * Silently refresh the cookie session via POST /auth/refresh. The Web-Locks
 * single-flight is REQUIRED (not an optimization): cookies are shared across
 * tabs, so two tabs racing the same refresh cookie would trip the server's
 * reuse-detection and force a full re-auth (KD-5/U8). Returns truthy on success,
 * null on failure — NOT a bearer token (the new session is in cookies). The name
 * is retained for the legacy call sites that still invoke it.
 */
export function refreshAccessToken(): Promise<true | null> {
  // In-tab: a single `inflight` promise coalesces concurrent callers into ONE
  // network refresh. Cross-tab: the Web Lock serializes the actual refresh so two
  // tabs never present the same refresh cookie at once and trip reuse-detection.
  if (inflight) return inflight
  const hasLocks = typeof navigator !== 'undefined' && navigator.locks?.request
  const run = hasLocks ? navigator.locks.request(LOCK_NAME, doRefresh) : doRefresh()
  inflight = Promise.resolve(run)
    .catch(() => null)
    .finally(() => {
      inflight = null
    })
  return inflight
}

async function doRefresh(): Promise<true | null> {
  const csrf = getCsrfToken()
  try {
    const res = await fetch(`${AUTH_API}/refresh`, {
      method: 'POST',
      credentials: 'include',
      headers: csrf ? { 'X-CSRF-Token': csrf } : {},
      signal: authFetchTimeout(),
    })
    if (res.ok) return true
    if (res.status === 401 || res.status === 403) {
      // The refresh cookie is dead. Show the "expired" banner only if we HAD a
      // session (a csrf cookie was present) — a first-time visitor hitting a
      // guarded route shouldn't see "your session expired".
      clearSession(csrf ? SIGNOUT_REASONS.EXPIRED : undefined)
      return null
    }
    return null // 5xx / transient — keep the session, let the next call retry
  } catch {
    return null // network blip — fail open
  }
}

// --- logout (POST /auth/logout) ----------------------------------------------

/**
 * Server-side logout (bumps token_version, revokes refresh families, clears
 * cookies). Best-effort: always drops the client session and records LOGGED_OUT;
 * returns true on success, false otherwise. The caller navigates away regardless
 * — never trap the user's intent to leave.
 */
export async function logout(): Promise<boolean> {
  const csrf = getCsrfToken()
  try {
    const res = await fetch(`${AUTH_API}/logout`, {
      method: 'POST',
      credentials: 'include',
      headers: csrf ? { 'X-CSRF-Token': csrf } : {},
      signal: authFetchTimeout(),
    })
    clearSession(SIGNOUT_REASONS.LOGGED_OUT)
    return res.ok
  } catch {
    clearSession(SIGNOUT_REASONS.LOGGED_OUT)
    return false
  }
}

// --- legacy Bearer shims (retired; kept so Express call sites compile) --------

/**
 * No bearer token exists in the cookie model — always null (KD-10 shim). The
 * declared return type is widened to `string | null` on purpose: this is the
 * default of `authFetch`'s injectable `getToken` seam, and a bare `null` literal
 * narrows every TypeScript caller's dep bag to `() => null`.
 */
export function getAccessToken(): string | null {
  return null
}
