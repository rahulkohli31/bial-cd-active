/**
 * WHAT A WAIT LOOKS LIKE WHEN IT IS NOT ALLOWED TO MOVE.
 *
 * THE BUG THIS EXISTS TO CLOSE. `index.css`'s reduce-motion block sets `animation: none` on
 * `.animate-spin`, which is correct and stays. What it leaves behind is the problem: a `Loader2`
 * is a circular arrow with a gap in it — the universal "loading" glyph — and a STATIONARY one
 * does not read as "motion was suppressed", it reads as "this hung". So the portal's accommodation
 * turned every wait into a picture of a crash. Three components already branched on the
 * preference (`ToolActivityLine`, `OfferStrip`, `StopTurnControl`) and all three branched the
 * wrong way: they dropped `animate-spin` and kept the same frozen arrow.
 *
 * Reported from two Windows VMs, where animations are commonly switched off at the OS: a 40-second
 * save and a 76-second hand-over both presented as a dead modal. The citizen's own words were that
 * the application was stuck.
 *
 * THE FIX IS NOT MORE MOTION. Under the preference this renders no spinner at all — a glyph that
 * never claims to rotate cannot look stalled — and carries the wait on the one signal that needs
 * no animation to prove liveness: A NUMBER THAT GOES UP. Elapsed seconds is honest under both
 * registers, which is why it appears under motion too once a wait outlives `ELAPSED_AFTER_MS`.
 * A spinner says "working"; it cannot say "still working, and here is how long", and at seventy
 * seconds that is the only question the person in front of it has.
 *
 * WHY A SHARED PRIMITIVE RATHER THAN TWENTY EDITS. Twenty-three components render `animate-spin`
 * and each would have needed the same three lines, which is how the three that already had them
 * ended up disagreeing with the other twenty. One component, one behaviour, one place to correct.
 */
import { useEffect, useRef, useState } from 'react'
import { Loader2, Clock } from 'lucide-react'

function readReducedMotion(): boolean {
  return (
    typeof window !== 'undefined' &&
    typeof window.matchMedia === 'function' &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches
  )
}

/** Tracks `prefers-reduced-motion`; SSR/jsdom-safe (no matchMedia → false, i.e. animate). */
export function usePrefersReducedMotion(): boolean {
  const [reduced, setReduced] = useState(readReducedMotion)
  useEffect(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return undefined
    const mq = window.matchMedia('(prefers-reduced-motion: reduce)')
    const onChange = () => setReduced(mq.matches)
    onChange()
    mq.addEventListener?.('change', onChange)
    return () => mq.removeEventListener?.('change', onChange)
  }, [])
  return reduced
}

/** How long a wait must run before its elapsed time is worth showing. Below this the number is
 *  noise on an interaction that already feels instant; above it, it is the whole answer. */
export const ELAPSED_AFTER_MS = 5_000

/**
 * Seconds since the wait began, ticking once a second; `0` whenever `active` is false.
 *
 * THE TIMER IS KEYED ON THE TRANSITION, not on mount: these live inside dialogs that stay mounted
 * across several steps of a hand-over, and a counter that kept climbing through all of them would
 * report the dialog's age rather than the step's. Cleared on the way down so a second press starts
 * from zero rather than resuming someone else's clock.
 *
 * `since` IS FOR THE OPPOSITE SHAPE — a wait whose ELEMENT comes and goes while the wait itself
 * runs on. A caller that is torn down and rebuilt mid-wait has no transition to key on: it mounts
 * fresh each time and, without an anchor it did not choose, would restart from zero and report a
 * number SMALLER than the one already on screen. Given a timestamp, the count is derived from it
 * on every mount, so remounting is invisible and the number only ever goes up.
 */
export function useElapsedSeconds(active: boolean, since?: number | null): number {
  const [seconds, setSeconds] = useState(0)
  const startedAt = useRef<number | null>(null)

  useEffect(() => {
    if (!active) {
      startedAt.current = null
      setSeconds(0)
      return undefined
    }
    // Clamp: a clock skewed ahead of the anchor would otherwise count backwards from a negative.
    const from = since ?? Date.now()
    const read = () => Math.max(0, Math.floor((Date.now() - from) / 1000))
    startedAt.current = from
    // Read immediately rather than seeding 0 — on a remount the wait is already underway, and the
    // first tick is a second away.
    setSeconds(read())
    const id = setInterval(() => setSeconds(read()), 1000)
    return () => clearInterval(id)
  }, [active, since])

  return seconds
}

/**
 * The glyph half, for the places that have room for an icon and nothing else — buttons, mostly.
 *
 * `aria-hidden` in BOTH registers. It is decoration either way; every caller already pairs it with
 * a label, and the label is what a screen reader should read.
 */
export function BusyGlyph({
  size = 15,
  className = '',
  testId,
  icon: Icon = Loader2,
  durationMs,
}: {
  size?: number
  className?: string
  /** Forwarded as `data-testid`. Carried through BOTH registers on purpose: a suite that could
   *  only find the glyph while it span would go green on the very bug this module closes. */
  testId?: string
  /**
   * The glyph to spin, when the caller's own carries meaning the default does not — the preview's
   * RotateCcw says "reconnecting", not merely "waiting". Ignored under the preference, where the
   * whole point is that nothing circular sits still.
   */
  icon?: typeof Loader2
  /**
   * A slower revolution, in milliseconds, for a wait the surface wants to read as unhurried. The
   * preview's stall card used 1.8s deliberately — "this is taking longer than usual" — and a
   * mechanical sweep dropped it once already.
   */
  durationMs?: number
}): React.ReactElement {
  const reduced = usePrefersReducedMotion()
  // NOT the caller's icon without its animation — that is precisely the frozen arrow this module
  // exists to stop rendering, and RotateCcw frozen reads exactly as badly as Loader2 frozen. A
  // clock face is static BY NATURE, so nothing about it is waiting to move.
  if (reduced)
    return <Clock size={size} aria-hidden="true" data-testid={testId} className={`flex-shrink-0 ${className}`} />
  return (
    <Icon
      size={size}
      aria-hidden="true"
      data-testid={testId}
      className={`flex-shrink-0 animate-spin ${className}`}
      {...(durationMs ? { style: { animationDuration: `${durationMs}ms` } } : {})}
    />
  )
}

export interface WaitingLineProps {
  /** What is happening, in the caller's own words — "Saving it first…", "Putting it away…". */
  label: string
  /** Whether the wait is running. Drives the elapsed clock; the caller still decides to render. */
  active?: boolean
  /** When this wait outlives its own element, the moment it began (`Date.now()`), so the count
   *  survives a remount. Omit it and the clock starts when this component does. */
  since?: number | null
  className?: string
}

/**
 * Glyph, sentence and — once the wait has earned it — a live elapsed count.
 *
 * NO `role="status"` OF ITS OWN, and the reason is now two reasons. Two callers sit inside a
 * polite region they own (`ReclaimWorkspaceDialog`'s step line, `SaveControl`'s wait box), where
 * nesting a second region is how a sentence gets announced twice. The third —
 * `ChatThread`'s `ReasoningGroup` — sits inside NO region at all, deliberately: the transcript's
 * announcing is `Announcer`'s job, driven off the turn-level running flag, so a region here would
 * announce the same turn a second time and once more per working window. Either way the rule
 * holds: this component draws, something else speaks.
 *
 * The seconds are `tabular-nums` so the line does not reflow on every tick — a sentence that
 * jitters once a second is its own kind of broken.
 */
export function WaitingLine({
  label,
  active = true,
  since,
  className = '',
}: WaitingLineProps): React.ReactElement {
  const seconds = useElapsedSeconds(active, since)
  const show = seconds * 1000 >= ELAPSED_AFTER_MS
  return (
    <span className={`inline-flex items-center gap-2 ${className}`}>
      <BusyGlyph size={14} className="text-primary" />
      <span>{label}</span>
      {show && (
        // `aria-hidden`, and this is not an oversight. Callers place this line inside a polite live
        // region so the WAIT is announced; a number that changes every second inside that region is
        // announced every second, which turns a 76-second hand-over into 76 interruptions. The
        // sentence beside it already carries the meaning for a reader who cannot see the count.
        <span
          data-testid="waiting-elapsed"
          aria-hidden="true"
          className="tabular-nums text-neutral/70"
        >
          {seconds}s
        </span>
      )}
    </span>
  )
}
