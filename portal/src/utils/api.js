/**
 * Authenticated fetch for JSON API calls (admin console, future authed reads).
 *
 * Attaches the Bearer token when one exists (legacy Express callers) and, on a
 * pre-body 401, refreshes ONCE and retries — the same admission pattern as
 * fetchClaudeStream. In the cookie-session model refresh yields a success boolean
 * (not a token), so the retry carries no Authorization header and rides the
 * session cookie. Dependencies are injected so it's testable without a real
 * network or a React render.
 *
 * NO `X-CSRF-Token` HERE, DELIBERATELY. `auth.js` sends it on `/auth/refresh` and
 * `/auth/logout` because those are the only routes that verify it (`_csrf_ok`,
 * `backend/src/api/v1/auth/router.py`). No business router — projects, conversations, apps,
 * admin — depends on CSRF verification, so attaching the header here would be cargo cult. If a
 * business route ever starts enforcing double-submit CSRF, this is the seam to add it to.
 */
import { getAccessToken, refreshAccessToken, handleSuspendedSession } from './auth.js'
import { isSuspended, ApiError } from './apiError'

/**
 * The injectable dependency bag. Declared explicitly because TypeScript callers
 * (`projectApi.ts`) otherwise INFER these from the defaults — and `getAccessToken`
 * is a cookie-model shim whose body is `return null`, so the inferred `getToken`
 * would be `() => null`, rejecting every real implementation. `checkJs` is off,
 * but inference across the .js↔.ts boundary is not.
 *
 * @typedef {object} AuthFetchDeps
 * @property {() => string | null} [getToken]
 * @property {() => Promise<unknown>} [refresh]
 * @property {(url: string, opts?: RequestInit) => Promise<Response>} [fetchImpl]
 */

/**
 * @param {string} url
 * @param {RequestInit} [opts]
 * @param {AuthFetchDeps} [deps]
 * @returns {Promise<Response>}
 */
export async function authFetch(
  url,
  opts = {},
  { getToken = getAccessToken, refresh = refreshAccessToken, fetchImpl = fetch } = {},
) {
  const call = (token) =>
    fetchImpl(url, {
      ...opts,
      // Spread AFTER ...opts so a caller can't accidentally drop the cookie: the
      // FastAPI HttpOnly session cookie must ride on every business call (KD-10),
      // matching the `credentials: 'include'` on every auth.js round-trip.
      credentials: 'include',
      headers: {
        ...(opts.headers || {}),
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
    })

  /**
   * Mid-session suspension gate. Only a 403 can carry `{"detail":"Account suspended"}`, so
   * ONLY a 403 pays the clone+parse — every other response (an app-status payload holding
   * an `appKey`, raw attachment bytes, SSE) passes straight through untouched, never exposing
   * secret material to a needless parse. `res.clone()` leaves the original body intact for the
   * caller when this is NOT a suspension (a CSRF failure, or the super-admin gate).
   */
  const bounceIfSuspended = async (response) => {
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
      // the second response would otherwise reach the caller as a bare 403 — `fetchClaudeStream`
      // already re-checks, and the asymmetry was a latent hole rather than a deliberate choice.
      await bounceIfSuspended(res)
    }
  }
  return res
}
