/**
 * Authenticated fetch for JSON API calls (admin console, future authed reads).
 *
 * Attaches the Bearer token when one exists (legacy Express callers) and, on a
 * pre-body 401, refreshes ONCE and retries — the same admission pattern as
 * fetchClaudeStream. In the cookie-session model refresh yields a success boolean
 * (not a token), so the retry carries no Authorization header and rides the
 * session cookie. Dependencies are injected so it's testable without a real
 * network or a React render.
 */
import { getAccessToken, refreshAccessToken } from './auth.js'

export async function authFetch(
  url,
  opts = {},
  { getToken = getAccessToken, refresh = refreshAccessToken, fetchImpl = fetch } = {},
) {
  const call = (token) =>
    fetchImpl(url, {
      ...opts,
      headers: {
        ...(opts.headers || {}),
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
    })

  let res = await call(getToken())
  if (res.status === 401) {
    // refreshAccessToken() returns a SUCCESS BOOLEAN in the cookie-session model,
    // not a bearer token — on success retry with NO Authorization header (the
    // session cookie rides along automatically); passing the boolean would send a
    // literal `Authorization: Bearer true`.
    const refreshed = await refresh()
    if (refreshed) res = await call()
  }
  return res
}
