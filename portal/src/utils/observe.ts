/**
 * The three marks only the browser can make (R104, R105).
 *
 * The server can see a start begin and a container answer. It cannot see a citizen OPEN a
 * project, cannot see them open a chat from it, and above all cannot see the moment they are
 * finally looking at their own app. Those are facts about a screen, so the screen has to say —
 * and this module is the whole of what it says. One file, so the measurement is legible in one
 * place rather than scattered through pages other work is about to rewrite.
 *
 * WHAT A "VISIT" IS, stated because R105's denominator turns on it: ONE PROJECT ID PER PAGE LOAD.
 * The guards below live in module state, are never reset within a load, and a reload starts a new
 * visit. So a citizen who opens a project, opens a chat, comes back and opens another scores one
 * and one — and a citizen who leaves a tab open all day and returns to the same project at four
 * o'clock is still one visit. That under-counts repeat visits in a long-lived tab; it is written
 * down beside the number rather than engineered around.
 *
 * THE SAME GUARD COVERS STRICTMODE. React double-invokes every effect in development
 * (`src/main.tsx`), so a mount-fired beacon would double-count without one.
 *
 * `project_opened_chat` FIRES ONLY FOR A PROJECT ALREADY MARKED OPEN IN THIS LOAD. A deep link
 * straight to `/chat/{id}` — a bookmark, a shared link, a browser restore — resolves a project
 * that was never opened on screen, and counting it would push R105's ratio above 1. A denominator
 * smaller than its numerator is not a bias, it is a broken number.
 *
 * THE R104 CLOCK IS GATED ON THE PROJECT ALREADY HAVING AN APP. A project with nothing built has
 * no app to first-see, and emitting for it would make today's number and the sandbox-first
 * number answer different questions.
 *
 * THE GUARDS GROW, AND THAT IS THE POINT. They hold one entry per distinct project id visited in
 * this page load, and nothing evicts them — eviction would BE the double-count, since forgetting a
 * project id is exactly what makes its second visit look like a first. A reload clears them. The
 * ceiling is therefore "projects one person opened without reloading", which is a handful.
 *
 * IT LIVES IN MEMORY, and the biases that follow are recorded rather than hidden. A reload
 * mid-journey abandons the measurement, so the sample tilts toward smooth journeys. A backgrounded
 * tab inflates one — and where that inflation crosses the server's ceiling the row is REFUSED
 * outright, so the effect is a lost reading, not a capped one: the R104 mean is biased toward the
 * fast journeys twice over, once by reloads and once by the ceiling. Same for the long-lived tab
 * that returns to a project hours later: its first visit's clock is still open, so the reveal it
 * eventually gets is measured from the wrong start and refused. Those are lost rows, not wrong
 * ones, which is the right way round — but it means R104 is a floor on a healthy journey rather
 * than an average over all of them.
 *
 * There is deliberately NO visibility listener — that would be a second mechanism doing work the
 * ceiling already does at the only place it can be enforced, and it would discard a slice of
 * exactly the slow journeys R104 exists to see.
 *
 * AND WHAT THE STOP-CLOCK DOES NOT PROMISE. `markAppVisible` fires when the preview pane is
 * SHOWING the app uncovered, which is the honest end of the wait — but a cross-origin frame's
 * `load` fires for a 500 as readily as a 200, and the pane deliberately reveals a frame whose
 * compile verdict never arrived rather than leave older containers permanently blank. So this
 * measures how long until the citizen was looking at their app, not until the app was known good;
 * a broken app ends a wait too. The one case that is NOT a real view — a confirmed workspace
 * reversion, where a cover is up over a loaded frame — is excluded at the pane
 * (`LivePreview.tsx`'s reveal effect checks `workspaceLost`).
 *
 * EVERY CALL IS FIRE-AND-FORGET. A failed observation never surfaces to the citizen and never
 * fails the thing it was observing — the same contract `count(...)` holds on the server side.
 */
import { authFetch } from './api'

/**
 * The three names the beacon route allows. It refuses anything else; this is NOT the gate — the
 * gate is `_CEILING_BY_NAME` in `backend/src/api/v1/observations/router.py`, and a name that only
 * exists here is a beacon that 400s forever in silence, because this whole path is
 * fire-and-forget by design and nothing would ever surface the refusal.
 *
 * A runtime array rather than a bare type union, so that silence is testable: the type is derived
 * from the values, and `observationContract.test.ts` reconciles the values against the server's
 * allowlist. A union alone is erased at compile time and cannot be compared to anything.
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

async function beacon(name: ObservationName, value?: number): Promise<void> {
  try {
    await authFetch('/api/observations', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(value === undefined ? { name } : { name, value }),
    })
  } catch {
    // Recovered by DROPPING the observation, which is the whole contract: a measurement must
    // never fail the thing it is measuring, and there is nothing a citizen could do about a
    // beacon that did not land. The server-side counter swallows for the same reason.
  }
}

function send(name: ObservationName, value?: number): void {
  void beacon(name, value)
}

/**
 * A project page mounted. At most one beacon per project id per page load.
 *
 * `hasApp` starts the R104 clock — a project with nothing built has nothing to first-see.
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
 * The citizen is looking at their own app: the preview frame loaded AND the cover is down.
 *
 * Sends nothing when no clock was started for this project — the project had no app, or this load
 * never opened its page (a deep link straight into a chat). Defaulting a missing mark to page-load
 * time would measure a different journey and pollute the only R104 number there is.
 */
export function markAppVisible(projectId: string | null): void {
  if (!projectId) return
  const startedAt = appVisibleClocks.get(projectId)
  if (startedAt === undefined) return
  appVisibleClocks.delete(projectId)
  send('project_to_app_visible_ms', Date.now() - startedAt)
}
