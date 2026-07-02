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

/** Full-page navigation target for "Sign in with Microsoft" (handled by FastAPI). */
export const LOGIN_URL = `${AUTH_API}/login`

const SIGNOUT_REASON_KEY = 'bial_signout_reason'
const LOCK_NAME = 'bial_token_refresh'

// Pre-cookie localStorage keys, purged ONCE on first bootstrap so a stale Bearer
// token from before the migration can't linger.
const LEGACY_KEYS = ['bial_access_token', 'bial_refresh_token', 'bial_user']

// Valid one-time signout reasons (drive the login banner copy).
export const SIGNOUT_REASONS = {
  EXPIRED: 'session_expired',
  LOGGED_OUT: 'logged_out',
}

let legacyPurged = false
function purgeLegacyTokensOnce() {
  if (legacyPurged) return
  legacyPurged = true
  try {
    LEGACY_KEYS.forEach((k) => localStorage.removeItem(k))
  } catch {
    // best-effort — private-mode / disabled storage
  }
}

// --- cached session context (once-cached GET /auth/me) -----------------------

let sessionPromise = null // in-flight or resolved bootstrap promise
let cachedUser = null // last known profile, or null when signed out

async function fetchMe() {
  const req = () =>
    fetch(`${AUTH_API}/me`, { credentials: 'include', headers: { Accept: 'application/json' } })
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
    cachedUser = await res.json().catch(() => null)
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
export function bootstrapSession() {
  purgeLegacyTokensOnce()
  if (!sessionPromise) sessionPromise = fetchMe()
  return sessionPromise
}

/** Forget the cached session so the next bootstrap re-fetches /me. */
export function invalidateSession() {
  sessionPromise = null
  cachedUser = null
}

/** The cached profile ({ id, email, display_name }) or null. Sync. */
export function getStoredUser() {
  return cachedUser
}

/** Sync best-effort auth check — true once a session context is cached. */
export function isAuthenticated() {
  return cachedUser != null
}

// --- CSRF (non-HttpOnly cookie -> X-CSRF-Token header) -----------------------

function getCsrfToken() {
  try {
    const match = document.cookie.match(/(?:^|;\s*)(?:__Host-)?csrf=([^;]+)/)
    return match ? decodeURIComponent(match[1]) : null
  } catch {
    return null
  }
}

// --- signout-reason banner (one-time) ----------------------------------------

/** Drop the client session; optionally record a one-time login-banner reason. */
export function clearSession(reason) {
  invalidateSession()
  if (reason) {
    try {
      localStorage.setItem(SIGNOUT_REASON_KEY, reason)
    } catch {
      // best-effort
    }
  }
}

export function consumeSignoutReason() {
  try {
    const reason = localStorage.getItem(SIGNOUT_REASON_KEY)
    if (reason) localStorage.removeItem(SIGNOUT_REASON_KEY)
    return reason
  } catch {
    return null
  }
}

// --- silent refresh (cookie-based, cross-tab single-flight) ------------------

let inflight = null

/**
 * Silently refresh the cookie session via POST /auth/refresh. The Web-Locks
 * single-flight is REQUIRED (not an optimization): cookies are shared across
 * tabs, so two tabs racing the same refresh cookie would trip the server's
 * reuse-detection and force a full re-auth (KD-5/U8). Returns truthy on success,
 * null on failure — NOT a bearer token (the new session is in cookies). The name
 * is retained for the legacy call sites that still invoke it.
 */
export function refreshAccessToken() {
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

async function doRefresh() {
  const csrf = getCsrfToken()
  try {
    const res = await fetch(`${AUTH_API}/refresh`, {
      method: 'POST',
      credentials: 'include',
      headers: csrf ? { 'X-CSRF-Token': csrf } : {},
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
export async function logout() {
  const csrf = getCsrfToken()
  try {
    const res = await fetch(`${AUTH_API}/logout`, {
      method: 'POST',
      credentials: 'include',
      headers: csrf ? { 'X-CSRF-Token': csrf } : {},
    })
    clearSession(SIGNOUT_REASONS.LOGGED_OUT)
    return res.ok
  } catch {
    clearSession(SIGNOUT_REASONS.LOGGED_OUT)
    return false
  }
}

// --- legacy Bearer shims (retired; kept so Express call sites compile) --------

/** No bearer token exists in the cookie model — always null (KD-10 shim). */
export function getAccessToken() {
  return null
}
