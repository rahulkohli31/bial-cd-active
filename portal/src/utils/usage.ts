/**
 * Daily-token-usage badge helpers (interim). Isolates the meter's data fetch and the
 * "usage changed, refetch" signal so its two consumers — the navigation panel's ring and the
 * workspace toolbar's compact one, both through `useUsageToday` — stay thin, and so both pieces
 * are testable without a render.
 *
 * The signal is a window CustomEvent: the meter and the chat state have no shared React
 * parent — the meter reads from the navigation panel and a turn completes deep inside the
 * workspace — so a lightweight global event is genuinely the lightest cross-component channel.
 */
const USAGE_EVENT = 'bial:usage-refresh'

/** Tell every token counter to refetch — after a turn ends, and as a build's steps land. */
export function notifyUsageChanged(): void {
  try {
    window.dispatchEvent(new CustomEvent(USAGE_EVENT))
  } catch {
    // window/CustomEvent unavailable (SSR/tests) — best-effort only.
  }
}

/** The most often a build's steps may refresh the counter. */
export const STEP_REFRESH_WINDOW_MS = 5_000

/**
 * Refresh the counter as a build's steps land, at most once per window. A step inside the window
 * schedules one refresh at its end, so the last step is always read. `cancel` drops that pending
 * refresh when the build's own final refresh takes over.
 */
export function createStepUsageRefresh(windowMs = STEP_REFRESH_WINDOW_MS): {
  step: () => void
  cancel: () => void
} {
  let quietUntil = 0
  let trailing: ReturnType<typeof setTimeout> | null = null
  const refresh = () => {
    quietUntil = Date.now() + windowMs
    notifyUsageChanged()
  }
  return {
    step() {
      const wait = quietUntil - Date.now()
      if (wait <= 0) refresh()
      else
        trailing ??= setTimeout(() => {
          trailing = null
          refresh()
        }, wait)
    },
    cancel() {
      if (trailing !== null) clearTimeout(trailing)
      trailing = null
    },
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
 * (e.g. a 401 mid-logout) — null is "no reading", which `useUsageToday` shows as
 * nothing only when it has no earlier reading to keep. Auth rides the session
 * cookie (`credentials: 'include'`); the proxy rewrites /api → /v1.
 */
export async function fetchUsageToday(fetchImpl: typeof fetch = fetch): Promise<UsageToday | null> {
  try {
    const res = await fetchImpl('/api/usage/today', { credentials: 'include' })
    if (!res.ok) return null
    // UNVALIDATED (unlike projectApi.ts's toProject/toProjectsPage): trusts the
    // server's shape as-is, matching today's behavior exactly. A malformed 200
    // body still crashes or NaNs in the meter today, same as before this migration
    // — not fixed here. Flagged as a follow-up for Rahul.
    const body: unknown = await res.json()
    return body as UsageToday
  } catch {
    return null
  }
}
