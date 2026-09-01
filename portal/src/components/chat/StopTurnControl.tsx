/**
 * STOP, MOVED TO WHERE THE COMPOSER IS (R55).
 *
 * Today stop lives inside `BuildProgress` — the pinned card this work exists to delete. This
 * component is the same ability in a component of its own, mounted on the composer's chrome, and
 * it ships BEFORE anything is removed so there is never a commit in which a build can be started
 * and not stopped. U10 gives it its permanent home on the new composer; U17 asserts a running
 * turn is still stoppable after `BuildProgress.tsx` is gone.
 *
 * RELOCATED, NOT REDESIGNED. The better version of stop is its own work.
 *
 * ── THE TWO ARMS ARE NOT A MODE BRANCH ──
 *
 * They discriminate on whether a TURN ID EXISTS, which is a transport fact, not a kind of chat.
 * A turn build is stopped through the turn endpoint with its conversation and turn ids; a legacy
 * build session has no turn id and is stopped through the session. Nothing here asks what kind of
 * chat this is, and after this plan nothing on the surface does.
 *
 * FORCE-END DELIBERATELY DOES NOT MOVE. `BuildProgress` records that a turn build has no
 * force-end equivalent, and a kill switch that confirms "this kills in-progress work" and then
 * does nothing is worse than no kill switch. It dies with the card.
 *
 * ── `aria-disabled`, NEVER `disabled` (R64) ──
 *
 * The shipped button in `BuildProgress` uses a real `disabled={stopping}`. That is the bug R64
 * forbids: `disabled` on the currently-focused element blurs it to `document.body`, so a keyboard
 * user who has just pressed Stop loses their place at the exact moment they were promised
 * feedback. This codebase records the mechanism twice already (`BuilderPage`'s textarea and its
 * Send button) and it is not a style preference. Enforcement lives in the handler; the attribute
 * is affordance only.
 *
 * THE ACCESSIBLE NAME IS STABLE. The old button's label flips "Stop" → "Stopping…", which renames
 * the control mid-interaction — the same defect U15 avoids on the copy button. The word stays
 * "Stop" in every state; the in-flight state is carried by the glyph and by `title`, which is the
 * `aria-disabled`-plus-reason shape the composer's Send already uses.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { Loader2, Square } from 'lucide-react'
import { usePrefersReducedMotion } from './ToolActivityLine'

/** The live turn to stop: which conversation, and which turn within it. */
export interface StopTarget {
  conversationId: string
  turnId: string
}

export interface StopTurnControlProps {
  /** A turn — or a legacy build session — is running. No run, no control: this renders `null`. */
  running: boolean
  /**
   * Resolve the live turn AT PRESS TIME, returning `null` when there is no turn id — which means
   * a legacy build session, not an error.
   *
   * A getter rather than a plain `turnId` prop, and that is not ceremony. `BuilderPage` holds the
   * live turn id in a REF, with a comment saying why: the stop handler is created once and would
   * otherwise close over whichever turn was live at its first render. A prop read during render
   * reintroduces exactly that staleness one layer up, and it would do so silently — stopping the
   * previous turn, or falling to the session arm because the render happened to precede the frame
   * that set the id. Reading at the moment of the press is the only version that cannot be stale.
   */
  resolveTarget: () => StopTarget | null
  /**
   * Stop the live turn, with the conversation id and turn id `resolveTarget` returned.
   *
   * The resolved value is deliberately `unknown`: `stopTurn` answers `"stopping"` or
   * `"already_settled"` and `session.stop()` answers a boolean. This control cares only that the
   * request SETTLED — a rejection is the failure, and "already settled" is a perfectly good
   * outcome for someone who pressed Stop as the turn was finishing anyway.
   */
  onStopTurn: (conversationId: string, turnId: string) => Promise<unknown>
  /** Stop a legacy build session, which has no turn id. */
  onStopSession: () => Promise<unknown>
  /**
   * A stop request failed. The caller decides where the sentence lands — U9 consolidates that
   * onto the assertive slot. What this component guarantees is that a failure is never silent
   * and never leaves a dead button.
   */
  onStopFailed: (message: string) => void
}

const STOP_FAILED = 'Could not stop this. Try again.'

export default function StopTurnControl({
  running,
  resolveTarget,
  onStopTurn,
  onStopSession,
  onStopFailed,
}: StopTurnControlProps) {
  const [stopping, setStopping] = useState(false)
  // Same gate the step rows use, for the same reason: a spinner is the one piece of chrome on
  // this surface that moves continuously, and `prefers-reduced-motion` means it.
  const reducedMotion = usePrefersReducedMotion()

  // A stop request outlives the control: the turn ends, `running` flips false, this unmounts, and
  // the promise then settles. Without this the state update lands on a dead component.
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  const handleStop = useCallback(async () => {
    // The enforcement, and the whole of it. A second press while one is in flight is a no-op —
    // `aria-disabled` on the button is what SAYS so, and says nothing else.
    if (stopping) return
    setStopping(true)
    try {
      const target = resolveTarget()
      if (target) await onStopTurn(target.conversationId, target.turnId)
      else await onStopSession()
    } catch {
      onStopFailed(STOP_FAILED)
    } finally {
      if (mounted.current) setStopping(false)
    }
  }, [stopping, resolveTarget, onStopTurn, onStopSession, onStopFailed])

  if (!running) return null

  return (
    <button
      type="button"
      onClick={handleStop}
      aria-disabled={stopping}
      title={stopping ? 'Stopping — this can take a moment.' : undefined}
      data-testid="stop-turn"
      className={`inline-flex flex-shrink-0 items-center gap-1.5 rounded-lg border border-bial-border bg-white px-2.5 py-1 text-xs font-semibold text-tertiary transition ${
        stopping ? 'opacity-50 cursor-default' : 'hover:border-primary hover:text-primary'
      }`}
    >
      {stopping ? (
        <Loader2 size={12} className={reducedMotion ? undefined : 'animate-spin'} />
      ) : (
        <Square size={12} />
      )}
      Stop
    </button>
  )
}
