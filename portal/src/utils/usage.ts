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
export function notifyUsageChanged(): void {
  try {
    window.dispatchEvent(new CustomEvent(USAGE_EVENT))
  } catch {
    // window/CustomEvent unavailable (SSR/tests) — best-effort only.
  }
}

/**
 * Subscribe to usage-changed signals. Returns an unsubscribe function.
 *
 * `handler` receives a plain `Event`, not `CustomEvent<T>` — `notifyUsageChanged`
 * dispatches with no `detail` (this is a bare signal, not a payload carrier), and
 * nothing anywhere reads `.detail` off it.
 */
export function onUsageChanged(handler: (event: Event) => void): () => void {
  if (typeof window === 'undefined') return () => {}
  window.addEventListener(USAGE_EVENT, handler)
  return () => window.removeEventListener(USAGE_EVENT, handler)
}

/** The real shape of `GET /api/usage/today` (`backend/src/api/v1/usage/schemas.py`). */
export interface UsageToday {
  used: number
  limit: number
  remaining: number
  resetsAt: string
}

/**
 * Fetch the authenticated caller's own daily usage. Returns
 * `{ used, limit, remaining, resetsAt }`, or null when the server declines
 * (e.g. a 401 mid-logout) — null hides the badge. Auth rides the session
 * cookie (`credentials: 'include'`); the proxy rewrites /api → /v1.
 */
export async function fetchUsageToday(fetchImpl: typeof fetch = fetch): Promise<UsageToday | null> {
  try {
    const res = await fetchImpl('/api/usage/today', { credentials: 'include' })
    if (!res.ok) return null
    // UNVALIDATED (unlike projectApi.ts's toProject/toProjectsPage): trusts the
    // server's shape as-is, matching today's behavior exactly. A malformed 200
    // body still crashes or NaNs in Navbar today, same as before this migration
    // — not fixed here. Flagged as a follow-up for Rahul.
    const body: unknown = await res.json()
    return body as UsageToday
  } catch {
    return null
  }
}
