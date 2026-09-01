/**
 * THE READ BEHIND THE WORKSPACE STATE (Plan F, U2).
 *
 * `workspaceState.ts` is pure. This is the half that talks to the server: one cheap read, on a
 * cadence, turned into the one value the pane and the Plan-chat line both render.
 *
 * ═══ IT RUNS WHEN THERE IS NO FRAME, AND THAT IS THE WHOLE DIFFERENCE ═══
 *
 * The conversation surface's own preview probe returns early on `!framedPreviewUrl` — "only worth
 * asking while a frame is actually on screen claiming to be live". That is right for a pane whose
 * job is to catch a framed app being reclaimed underneath it. It is exactly wrong here: the
 * no-frame case is precisely what this hook exists to describe. A project whose app is saved and
 * not running has no address at all, and it is the state that carries the product's one start
 * control.
 *
 * ═══ WHAT IT COSTS, AND THE LINE IT WILL NOT CROSS ═══
 *
 * `fetchPreviewState` is CHEAP BY CONTRACT (C3 §8.3): one cache read, at most two rows, at most
 * two object-store HEADs, and NO container call. It is safe on a timer.
 *
 * `fetchSaveState` is not. It runs two `git` executions INSIDE the container, so it is called only
 * when the read says `alive`. Asking a stopped project whether it has unsaved work is an attach
 * against a dead workspace — a start the screen caused, which R3 forbids. The consequence is
 * stated rather than hidden: at rest, a stopped project shows no save state and no commit, and the
 * save half of the rail appears only while the app is running.
 *
 * `fetchCompileState` and `checkWorkspace` are not called from here at all. They belong to a
 * surface with a live turn behind it, and both cost a container exec.
 *
 * ═══ TWO CONSEQUENCES OF THE TIMER, WRITTEN DOWN BECAUSE FEATURES DEPEND ON THEM ═══
 *
 *  - `starting` reaches `running` WITH NO USER GESTURE. Somebody presses start, the server holds
 *    the state, and the pane arrives at the running app on its own.
 *  - THE THIRTY-MINUTE STAY LAPSING IS NOTICED. `RELAUNCH_PREVIEW_STAY_SECONDS` is granted at
 *    relaunch and extended only by a turn's own deadline writers; the start-then-read shape has no
 *    turn, so the stay can lapse under a person who is still reading. The next read returns
 *    `asleep` and the pane says "Your app is saved." with the start offered again — one press to
 *    recover, nothing lost. Renewing the stay on a read would be a new way to hold a container
 *    claimed, which is a server capability nobody has planned.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { fetchPreviewState, fetchSaveState } from '../../utils/buildSessionApi'
import type { PreviewState, SaveState } from '../../utils/buildSessionApi'
import {
  PREVIEW_PROBE_MS,
  isTerminalReading,
  resolveWorkspaceState,
  type StartOutcome,
  type WorkspaceState,
} from './workspaceState'

export interface WorkspaceReading {
  /** WHAT TO SAY. The single value the pane and the Plan-chat line both render. */
  state: WorkspaceState
  /**
   * The raw read, for the one caller that needs more than a sentence out of it: the project
   * surface builds the pane's address from this same result rather than starting a second poll.
   * The pure map's refusal to carry a URL is about the MAP's type; it is not a bar on the caller
   * that already holds the read using it.
   */
  preview: PreviewState | null
  /** The save model — non-null only while the workspace is `alive`. See the cost note above. */
  save: SaveState | null
  /** Record how the most recent start attempt ended. `null` clears it (a start that worked). */
  reportStartOutcome: (outcome: StartOutcome | null) => void
  /** Ask again NOW. A deliberate gesture: a start that just finished, or a retry press. */
  refresh: () => void
}

export interface WorkspaceReadOptions {
  /** `null` while a route is still resolving one — nothing is asked until it does. */
  projectId: string | null
  /** The project row's own restore answer, for a cold load before the first read lands. */
  projectHasSavedBuild: boolean | null
}

export function useWorkspaceState({
  projectId,
  projectHasSavedBuild,
}: WorkspaceReadOptions): WorkspaceReading {
  const [preview, setPreview] = useState<PreviewState | null>(null)
  const [save, setSave] = useState<SaveState | null>(null)
  const [startOutcome, setStartOutcome] = useState<StartOutcome | null>(null)
  // NOT DERIVED FROM ANYTHING, and it cannot be. A retry press is a synchronous fact whose only
  // observable state change can be collapsed into one commit by React's batching, so an
  // invalidation spelled as "something changed" is one a fast enough server erases. A counter
  // cannot be batched away: the value the effect sees is always different from the one before.
  const [epoch, setEpoch] = useState(0)

  const refresh = useCallback(() => setEpoch((n) => n + 1), [])
  const reportStartOutcome = useCallback((outcome: StartOutcome | null) => {
    setStartOutcome(outcome)
  }, [])

  // Read inside the async body without re-arming the effect. A start outcome must not restart the
  // poll — it is a fact about a press, not about the workspace — but the save read below has to
  // see the CURRENT project, which the effect's own closure already gives it.
  const projectRef = useRef(projectId)
  projectRef.current = projectId

  useEffect(() => {
    if (!projectId) {
      setPreview(null)
      setSave(null)
      return undefined
    }
    let live = true
    // A GENERATION COUNTER RATHER THAN AN IN-FLIGHT BOOLEAN. Tabbing back fires `visibilitychange`
    // and `focus` on the same gesture with the interval possibly mid-flight underneath them, so up
    // to three reads are in the air at once and settle in whatever order the network decides. A
    // boolean would DROP the later read — and the later read holds the fresher answer, so on
    // exactly the gesture where somebody is asking to be brought up to date it would answer with
    // the reading they already had.
    let latest = 0
    let timer: ReturnType<typeof setInterval> | null = null
    const stopAsking = () => {
      if (timer !== null) clearInterval(timer)
      timer = null
    }
    const keepAsking = () => {
      timer ??= setInterval(() => void read(), PREVIEW_PROBE_MS)
    }

    const read = async () => {
      if (!live || document.visibilityState !== 'visible') return
      const generation = ++latest
      try {
        const next = await fetchPreviewState(projectId)
        // Superseded: a later read started, so its answer is newer whatever order the responses
        // arrived in. Bail before touching state OR the timer — an overtaken read calling
        // `stopAsking()` would end the poll on a verdict that has already been replaced.
        if (!live || generation !== latest) return
        // AN `unknown` NEVER OVERWRITES A DECIDED VERDICT. A blip must not pull a running app off
        // screen, and it must not wipe a settled answer somebody is already reading either. It is
        // recorded only when nothing has been decided yet — because "we could not check" is a real
        // thing to say when it is the only thing we know.
        setPreview((prev) => (next.state === 'unknown' && prev ? prev : next))

        if (next.state === 'alive') {
          // THE ONLY CONTAINER CALL THIS HOOK MAKES, and it is gated on a live container for the
          // reason in the docblock. Its failure is silent on purpose: a save state we could not
          // read is `null`, which is the tri-state's "no claim", and every consumer already
          // treats that as "could not tell" rather than as "clean".
          const state = await fetchSaveState(projectId).catch(() => null)
          if (!live || generation !== latest || projectRef.current !== projectId) return
          setSave(state)
        } else {
          // Not alive, so nothing to compare and nothing that could still be true. Holding a save
          // state from a container that has since stopped would arm the unsaved-work guard against
          // work that is no longer reachable.
          setSave(null)
        }

        if (isTerminalReading(next)) stopAsking()
        else keepAsking()
      } catch {
        // A read that could not answer SAYS NOTHING. Painting "gone" on a network blip is the
        // over-claiming this whole shape exists to remove, and the timer is left running so the
        // next tick can correct it.
      }
    }

    // KEPT LIVE EVEN AFTER THE TIMER STOPS, deliberately. These fire on a deliberate human act —
    // tabbing back to the project — never on a clock, so they are bounded by the person rather
    // than by a cadence. They are also the only backstop for the one thing this effect's inputs
    // cannot see: another tab restoring, or taking, this project's workspace.
    const onVisible = () => void read()
    document.addEventListener('visibilitychange', onVisible)
    window.addEventListener('focus', onVisible)
    keepAsking()
    void read()
    return () => {
      live = false
      document.removeEventListener('visibilitychange', onVisible)
      window.removeEventListener('focus', onVisible)
      stopAsking()
    }
  }, [projectId, epoch])

  return {
    state: resolveWorkspaceState({ preview, projectHasSavedBuild, startOutcome }),
    preview,
    save,
    reportStartOutcome,
    refresh,
  }
}
