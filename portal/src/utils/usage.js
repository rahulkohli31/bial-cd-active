/**
 * Daily-token-usage badge helpers (interim). Isolates the navbar indicator's
 * data fetch and the "usage changed, refetch" signal so the single consumer
 * (Navbar) stays thin and both pieces are testable without a render.
 *
 * The signal is a window CustomEvent: the Navbar is rendered *inside* each page
 * (no shared React parent holding both the navbar and the chat state), so a
 * lightweight global event is genuinely the lightest cross-component channel.
 */
const USAGE_EVENT = 'bial:usage-refresh'

/** Tell the navbar badge to refetch (after a completed assistant turn). */
export function notifyUsageChanged() {
  try {
    window.dispatchEvent(new CustomEvent(USAGE_EVENT))
  } catch {
    // window/CustomEvent unavailable (SSR/tests) — best-effort only.
  }
}

/** Subscribe to usage-changed signals. Returns an unsubscribe function. */
export function onUsageChanged(handler) {
  if (typeof window === 'undefined') return () => {}
  window.addEventListener(USAGE_EVENT, handler)
  return () => window.removeEventListener(USAGE_EVENT, handler)
}

/**
 * Fetch the authenticated caller's own daily usage. Returns
 * `{ used, limit, remaining, resetsAt }`, or null when the server declines
 * (e.g. a 401 mid-logout) — null hides the badge. Auth rides the session
 * cookie (`credentials: 'include'`); the proxy rewrites /api → /v1.
 */
export async function fetchUsageToday(fetchImpl = fetch) {
  try {
    const res = await fetchImpl('/api/usage/today', { credentials: 'include' })
    if (!res.ok) return null
    return await res.json()
  } catch {
    return null
  }
}
