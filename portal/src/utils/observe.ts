/**
 * The marks only the browser can make: three named counts, and one start's wait.
 *
 * WHY THIS EXISTS — the server sees a start and a container answer, but never a citizen
 * open a project, open a chat, or first see their app; the screen reports what only it sees.
 *
 * A "visit" is ONE PROJECT ID PER PAGE LOAD: the module-state guards below reset only on
 * reload (also covering StrictMode's double effects) and never evict — bounded to
 * "projects one person opens without reloading," and deliberately under-counting a tab
 * that reopens the same project.
 *
 * `project_opened_chat` fires ONLY for a project this load already marked open — else the
 * ratio's denominator could fall below its numerator. The app-visible clock is gated on
 * the project already having an app.
 *
 * No visibility listener, deliberately: the server's ceiling already discards runaway
 * durations and a listener would drop genuinely slow journeys too — known biases (reload
 * abandons the measurement; a backgrounded tab gets REFUSED by that ceiling) make this a
 * floor on a healthy journey, not an average.
 *
 * `markAppVisible` fires when the framed document itself vouches that it is showing
 * something — its `bial:app-mounted` beacon — plus the cover coming down. A bodyless 502
 * ships no script at all, so it can no longer stop this clock; until 2026-09-11 the mark
 * rode the frame's `load` event, which fires for a 500 as readily as a 200, and that gap
 * became a production incident. It still measures time-until-looking rather than
 * time-until-known-good, because a page that paints and then throws has been seen (a
 * confirmed reversion is excluded at the pane, see `LivePreview.tsx`). Every call is
 * fire-and-forget, the server's `count(...)` contract.
 *
 * A START'S WAIT runs from the click that began it to the same stop, and is reported against the
 * id the server gave that start; the server attaches it to the caller's own start row, and labels
 * come from that row, never from here. One start is timed at a time, because a person has one
 * workspace. A click that began no start, a start nobody saw, and a start abandoned for another
 * project or by leaving the workspace report nothing.
 */
import { authFetch } from './api'

/**
 * The three names the beacon route allows — NOT the gate (that's `_CEILING_BY_NAME` in
 * `backend/src/api/v1/observations/router.py`; a name only here 400s forever in silence,
 * since this path is fire-and-forget). A runtime array, not a bare type union, so the
 * silence is testable: the type derives from these values, and
 * `observationContract.test.ts` reconciles them against the server's allowlist.
 */
export const OBSERVATION_NAMES = [
  'project_opened',
  'project_opened_chat',
  'project_to_app_visible_ms',
] as const

export type ObservationName = (typeof OBSERVATION_NAMES)[number]

/** Project ids marked open in THIS page load. Also the StrictMode guard. */
const openedProjects = new Set<string>()
/** Project ids whose chat-open has already been marked in this load. */
const chatOpenedProjects = new Set<string>()
/** Project id → when its page mounted. Present only for a project that already had an app, and
 *  DELETED when the duration is sent, which is what makes the duration fire at most once. */
const appVisibleClocks = new Map<string, number>()

/**
 * The project whose starts are being timed, and the start bound to it. REPLACED, never reassigned
 * to another project: a click's answer lands on the timing it was made under, and once that timing
 * has been abandoned nothing can read it.
 */
let timing: { projectId: string; start: { id: string; clickedAt: number } | null } | null = null

async function beacon(path: string, body: Record<string, unknown>): Promise<void> {
  try {
    await authFetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
  } catch {
    // Recovered by DROPPING the observation, which is the whole contract: a measurement must
    // never fail the thing it is measuring, and there is nothing a citizen could do about a
    // beacon that did not land. The server-side counter swallows for the same reason.
  }
}

function send(name: ObservationName, value?: number): void {
  void beacon('/api/observations', value === undefined ? { name } : { name, value })
}

/**
 * A project page mounted. At most one beacon per project id per page load.
 *
 * `hasApp` starts the clock — a project with nothing built has nothing to first-see.
 */
export function markProjectOpened(projectId: string, { hasApp }: { hasApp: boolean }): void {
  if (!projectId || openedProjects.has(projectId)) return
  openedProjects.add(projectId)
  if (hasApp) appVisibleClocks.set(projectId, Date.now())
  send('project_opened')
}

/**
 * A chat was opened from a project. At most one per project id per page load, and NEVER for a
 * project this load never opened — see the deep-link note above.
 */
export function markChatOpened(projectId: string | null): void {
  if (!projectId) return
  if (!openedProjects.has(projectId)) return
  if (chatOpenedProjects.has(projectId)) return
  chatOpenedProjects.add(projectId)
  send('project_opened_chat')
}

/**
 * The citizen is looking at their own app: the framed document vouched that it is showing
 * something AND the cover is down.
 *
 * Sends nothing when no clock was started for this project — the project had no app, or this load
 * never opened its page (a deep link straight into a chat). Defaulting a missing mark to page-load
 * time would measure a different journey and pollute the only number there is.
 */
export function markAppVisible(projectId: string | null): void {
  if (!projectId) return
  const startedAt = appVisibleClocks.get(projectId)
  if (startedAt === undefined) return
  appVisibleClocks.delete(projectId)
  send('project_to_app_visible_ms', Date.now() - startedAt)
}

/**
 * A click that may start this project's app. Hand what this returns the server's answer: the id of
 * the start the click began, or `null` when it began none.
 *
 * A click that joins a start already timed leaves it timed from the click that began it; a click
 * whose answer names a different start times that start from this click.
 */
export function markStartClicked(projectId: string): (startId: string | null) => void {
  if (timing?.projectId !== projectId) timing = { projectId, start: null }
  const mine = timing
  const clickedAt = Date.now()
  return (startId) => {
    if (!startId || mine.start?.id === startId) return
    mine.start = { id: startId, clickedAt }
  }
}

/**
 * This project's app is what the person is looking at — the same stop as `markAppVisible`. Reports
 * the start timed for it, once. A stop before the server named the start is not that start's: its
 * container does not exist until after the answer that names it.
 */
export function markStartVisible(projectId: string | null): void {
  if (timing === null || timing.projectId !== projectId || timing.start === null) return
  const { id, clickedAt } = timing.start
  timing.start = null
  void beacon('/api/observations/start-visible', { startId: id, durationMs: Date.now() - clickedAt })
}

/**
 * The person is no longer waiting on a start of any project but `onScreen` — they moved to another
 * project, or out of the workspace (`null`). What was timed for any other project reports nothing,
 * and an answer still in flight for it binds nothing.
 */
export function markStartAbandoned(onScreen: string | null = null): void {
  if (timing !== null && timing.projectId !== onScreen) timing = null
}
