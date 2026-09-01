/**
 * Authenticated fetch for JSON API calls (admin console, future authed reads).
 *
 * Attaches the Bearer token when one exists (legacy Express callers) and, on a
 * pre-body 401, refreshes ONCE and retries — the same admission pattern the turn
 * stream follows. In the cookie-session model refresh yields a success boolean
 * (not a token), so the retry carries no Authorization header and rides the
 * session cookie. Dependencies are injected so it's testable without a real
 * network or a React render.
 *
 * CSRF: business routes now DO enforce the signed double-submit token — conversation create and
 * mode-switch (`backend/src/api/v1/conversations/router.py` `RequireCsrf`), turn start/stop, and the
 * Build-it transition. This is the seam the older note pointed at, so we attach `X-CSRF-Token` on
 * every unsafe (mutating) method here, reading the same `csrf` cookie `auth.js` uses on
 * `/auth/refresh` / `/auth/logout`. Safe GETs carry no header; routes that don't verify it ignore it.
 */
import { getAccessToken, refreshAccessToken, handleSuspendedSession, getCsrfToken } from './auth'
import { isSuspended, ApiError } from './apiError'

/**
 * The injectable dependency bag. Declared explicitly because TypeScript callers
 * (`projectApi.ts`) otherwise INFER these from the defaults — and `getAccessToken`
 * is a cookie-model shim whose body is `return null`, so the inferred `getToken`
 * would be `() => null`, rejecting every real implementation. `checkJs` is off,
 * but inference across the .js↔.ts boundary is not.
 */
export interface AuthFetchDeps {
  getToken?: () => string | null
  refresh?: () => Promise<unknown>
  fetchImpl?: (url: string, opts?: RequestInit) => Promise<Response>
}

/**
 * The signed double-submit header for one attempt, or `{}` when no `csrf` cookie exists
 * (an un-verifying route simply ignores it). Deliberately a function, not a captured
 * value — see the call site.
 */
function csrfHeader(): Record<string, string> {
  const csrf = getCsrfToken()
  return csrf ? { 'X-CSRF-Token': csrf } : {}
}

export async function authFetch(
  url: string,
  opts: RequestInit = {},
  { getToken = getAccessToken, refresh = refreshAccessToken, fetchImpl = fetch }: AuthFetchDeps = {},
): Promise<Response> {
  // Signed double-submit CSRF rides every mutating request (create/switch-mode/etc.). GET/HEAD
  // are safe and carry no token; a route that doesn't verify it simply ignores the header.
  const method = (opts.method || 'GET').toUpperCase()
  const isMutating = method !== 'GET' && method !== 'HEAD'

  const call = (token?: string | null) =>
    fetchImpl(url, {
      ...opts,
      // Spread AFTER ...opts so a caller can't accidentally drop the cookie: the
      // FastAPI HttpOnly session cookie must ride on every business call (KD-10),
      // matching the `credentials: 'include'` on every auth.js round-trip.
      credentials: 'include',
      headers: {
        ...(opts.headers || {}),
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        // Read the token PER ATTEMPT, never once up front. `/auth/refresh` ROTATES the
        // `csrf` cookie, so a retry closing over the pre-refresh value trades the 401 for
        // a 403 on every mutating call. GETs only ever looked healthy here because they
        // carry no token at all.
        ...(isMutating ? csrfHeader() : {}),
      },
    })

  /**
   * Mid-session suspension gate. Only a 403 can carry `{"detail":"Account suspended"}`, so
   * ONLY a 403 pays the clone+parse — every other response (an app-status payload holding
   * an `appKey`, raw attachment bytes, SSE) passes straight through untouched, never exposing
   * secret material to a needless parse. `res.clone()` leaves the original body intact for the
   * caller when this is NOT a suspension (a CSRF failure, or the super-admin gate).
   */
  const bounceIfSuspended = async (response: Response): Promise<void> => {
    if (response.status !== 403) return
    const body = await response.clone().json().catch(() => null)
    if (!isSuspended(body, response.status)) return
    handleSuspendedSession()
    // Reject: the caller must not receive a usable response for a dead session.
    throw new ApiError('Account suspended', 403)
  }

  let res = await call(getToken())
  await bounceIfSuspended(res)

  if (res.status === 401) {
    // refreshAccessToken() returns a SUCCESS BOOLEAN in the cookie-session model,
    // not a bearer token — on success retry with NO Authorization header (the
    // session cookie rides along automatically); passing the boolean would send a
    // literal `Authorization: Bearer true`.
    const refreshed = await refresh()
    if (refreshed) {
      res = await call()
      // The retry gets the same gate as the first attempt. A suspension that only surfaces on
      // the second response would otherwise reach the caller as a bare 403 — the streaming path
      // re-checks too, and the asymmetry was a latent hole rather than a deliberate choice.
      await bounceIfSuspended(res)
    }
  }
  return res
}
