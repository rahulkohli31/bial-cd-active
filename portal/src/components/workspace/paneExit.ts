/**
 * THE PANE'S DEPARTURE, HELD OPEN LONG ENOUGH TO BE SEEN (plan 002, U6).
 *
 * `T2Sliding` is an entire artboard of this one moment, caught halfway, with an annotation that
 * says exactly what it is: "the app card is sliding out to the right and fading as it goes … a
 * moment later the app is gone", and "nothing about the app is stopped or reloaded — it is only
 * taken off the screen." Underneath it "the conversation is already settling towards the middle of
 * the window", which is why the two overlap on the board.
 *
 * ═══ WHY A HOLD IS NEEDED AT ALL ═══
 *
 * The keyframes and their reduced-motion suppression have existed since this plan landed, and
 * `animate-pane-leave` was never applied to anything — because applying it changes nothing on its
 * own. The moment a surface stops declaring the pane, the column goes to zero size and
 * `visibility:hidden` in the same frame, and an element that is not rendered cannot be watched
 * fading. So the exit is a state of its own, briefly: the column keeps its size, plays the
 * keyframe, and only then collapses.
 *
 * ═══ ONE AUTHOR, AND WHY IT IS A TIMER ═══
 *
 * `AppPane` owns this and hands the answer down to `AppPaneHost`, so the column and the frame
 * inside it cannot disagree about whether they are still leaving. It is a timer rather than an
 * `animationend` listener for one reason that decides it: under `prefers-reduced-motion` the
 * animation is suppressed in `index.css`, so `animationend` never fires and the pane would stay
 * on screen for ever. A timer always ends. The cost is that a reader who asked for less motion
 * waits {@link PANE_EXIT_MS} for a layout change instead of getting it instantly, which is a
 * quarter of a second of stillness rather than a quarter of a second of movement.
 *
 * NOTHING HERE UNMOUNTS OR RE-KEYS ANYTHING. The pane is the same element throughout, with a class
 * change — the whole reason the movement is safe over a live iframe.
 *
 * ═══ THE ONE COST, SAID OUT LOUD ═══
 *
 * `visibility:hidden` is what takes the framed app out of the TAB ORDER (see `hiddenSubtree.ts`),
 * and it cannot be applied while the card is still being watched leave — an invisible element has
 * nothing to animate. So for {@link PANE_EXIT_MS} the departing app is announced as gone
 * (`aria-hidden` lands immediately) but is still reachable by Tab. The clean fix is the `inert`
 * attribute, which removes a subtree from the tab order while leaving it painted; React 18 has no
 * supported prop for it, so it is named here rather than smuggled in as a raw attribute, and it is
 * the first thing to revisit when this app moves to React 19.
 */
import { useEffect, useRef, useState } from 'react'

/**
 * How long the leaving column holds its size.
 *
 * IT MUST MATCH `pane-leave`'s duration in `tailwind.config.js` (0.24s). Two numbers, because a
 * keyframe's duration is not readable from JavaScript without measuring computed style — so this
 * one is written down beside the reason instead of derived.
 */
export const PANE_EXIT_MS = 240

/**
 * `true` for one animation's length after the pane stops being wanted, `false` otherwise.
 *
 * A pane that was never visible does not "leave", so a surface that mounts with no pane at all —
 * every plan chat opened cold, and the project screen before anything is built — goes straight to
 * its resting state with no animation and no delay.
 */
export function usePaneLeaving(visible: boolean): boolean {
  const [leaving, setLeaving] = useState(false)
  const was = useRef(visible)

  useEffect(() => {
    if (was.current === visible) return undefined
    was.current = visible
    if (visible) {
      // Coming back INTERRUPTS a departure: the return arm takes over from wherever the leave got
      // to, rather than waiting for a timer about a movement that is no longer happening.
      setLeaving(false)
      return undefined
    }
    setLeaving(true)
    const timer = window.setTimeout(() => setLeaving(false), PANE_EXIT_MS)
    return () => window.clearTimeout(timer)
  }, [visible])

  return leaving
}
