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

// Bound every auth round-trip. A hung control-plane must not (a) hold the
// cross-tab Web Lock during a silent refresh and thereby wedge every other tab's
// refresh, nor (b) leave the RequireAuth bootstrap spinner spinning forever. The
// 10s timeout surfaces as an AbortError caught by each caller's existing catch,
// failing exactly like any other network error. Guarded so environments without
// AbortSignal.timeout (older/test runtimes) simply omit the bound.
const AUTH_FETCH_TIMEOUT_MS = 10_000
function authFetchTimeout() {
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
}

// Where a mid-session suspension bounces the user. The login screen keys its
// banner off this exact `?authError` value (LoginPage AUTH_ERROR_BANNERS).
const SUSPENDED_LOGIN_URL = '/login?authError=account_suspended'

// Where a stray pending-approval 403 bounces the user — any always-registered
// protected route works: the hard reload re-bootstraps from scratch, and
// RequireAuth resolves a fresh `status` from the server there.
const PENDING_REDIRECT_URL = '/dashboard'

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
    const profile = await res.json().catch(() => null)
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

// --- mid-session suspension (single-flight hard bounce to login) -------------

// The full-page navigation, isolated behind one indirection so jsdom tests can
// stub it (`window.location.assign` throws "Not implemented: navigation" in
// jsdom). A hard navigation — not react-router — because the callers (authFetch,
// fetchClaudeStream) have no router context and we WANT every in-flight page torn
// down, not a soft in-SPA transition that leaves stale trees mounted.
function hardRedirect(url) {
  window.location.assign(url)
}

/**
 * Wraps `action` so concurrent callers (several in-flight requests each hitting the
 * same gate) produce exactly ONE bounce — the first call runs `action`, every later
 * call is a no-op. Never resets: once a bounce fires, the page is being discarded
 * anyway (a hard redirect), so there's nothing to rearm. Each caller below gets its
 * OWN independent latch via its own closure — a pending bounce and a suspension
 * bounce are mutually exclusive in practice, but keeping their latches independent
 * avoids one gate's state accidentally silencing the other's.
 */
function bounceOnce(action) {
  let fired = false
  return () => {
    if (fired) return
    fired = true
    action()
  }
}

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
export const handleSuspendedSession = bounceOnce(() => {
  clearSession(SIGNOUT_REASONS.SUSPENDED)
  hardRedirect(SUSPENDED_LOGIN_URL)
})

/**
 * Stray pending-approval 403 recovery. This should be rare in practice — the
 * RequireAuth fast-path fix means a pending user's data-fetch calls normally
 * never fire (AwaitingApprovalPage renders instead of the real route) — but a
 * request already in flight when the fast-path re-evaluates, or a component
 * outside RequireAuth's tree, could still hit this. UNLIKE suspension, the
 * session is still valid — don't clear it. A hard reload just re-bootstraps
 * from scratch so RequireAuth resolves a fresh `status` from the server and
 * renders AwaitingApprovalPage instead of whatever stale tree produced the 403.
 */
export const handlePendingSession = bounceOnce(() => {
  hardRedirect(PENDING_REDIRECT_URL)
})

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
export async function logout() {
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
 *
 * @returns {string | null}
 */
export function getAccessToken() {
  return null
}
