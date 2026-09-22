/**
 * HAS THIS WAIT OUTLIVED THE PLATFORM'S OWN BUDGET — one boolean, from one timer.
 *
 * WHY A TIMER AND NOT A TICK. The boundary is crossed once per wait, so learning it by re-deriving
 * the whole workspace state every second would re-render the shell a hundred and twenty times to
 * discover one thing. `AppPane` already counts seconds for the number it draws; that counter is
 * local to the pane and nothing above it re-renders for it. This is the other half — the FACT,
 * which belongs in the state beside the sentence it changes rather than being re-derived at each
 * surface (see `WorkspaceState.busy` for why a second derivation is a second author).
 *
 * WHY NOT THE POLL. A reading that has not changed is not published — `samePreviewState` returns
 * the previous object, so the surfaces are deliberately not woken — and a wait that is stuck is
 * precisely a reading that does not change. Anything hung off the poll would therefore never fire
 * on the one case it exists for.
 *
 * IT ARMS FROM THE SERVER'S INSTANT, so a tab that reloads five minutes into a start crosses the
 * boundary immediately rather than starting its patience over. An undated wait gets the full
 * budget from now, which is the honest fallback: nothing knows better.
 */
import { useEffect, useState } from 'react'
import { START_PATIENCE_MS, msSpentSince, waitBeganAt } from './workspaceState'
import type { PreviewState } from '../../utils/buildSessionApi'

export function useTheWaitHasGoneOnTooLong(preview: PreviewState | null): boolean {
  const startedAt = waitBeganAt(preview)
  const waiting = preview?.state === 'starting'
  const [tooLong, setTooLong] = useState(false)
  useEffect(() => {
    if (!waiting) {
      setTooLong(false)
      return
    }
    // A WALL CLOCK HERE ONLY, and the asymmetry with the pane's counter is deliberate: this
    // compares against an instant another machine stamped, which a monotonic clock has no common
    // origin with. The cost of a system-clock jump is that the second sentence arrives early or
    // late once — the counter's cost would be a number that runs backwards, which is why that one
    // reads the wall clock exactly once and counts monotonically from there.
    const spent = msSpentSince(startedAt, Date.now())
    if (spent >= START_PATIENCE_MS) {
      setTooLong(true)
      return
    }
    setTooLong(false)
    const patience = setTimeout(() => setTooLong(true), START_PATIENCE_MS - spent)
    return () => clearTimeout(patience)
  }, [waiting, startedAt])
  return tooLong
}
