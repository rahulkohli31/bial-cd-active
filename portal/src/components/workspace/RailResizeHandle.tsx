/**
 * THE BOUNDARY BETWEEN THE RAIL AND THE APP (plan 002, U7).
 *
 * ═══ WHY THIS IS HAND-BUILT, SAID AT THE POINT IT HAPPENS ═══
 *
 * The board names `react-resizable-panels` and the plan overrules it, for reasons that are about
 * this shell rather than about that library:
 *
 *   · A PANEL GROUP TAKES ITS DIRECTION AS A VALUE, not as a class, and applies sizes inline. The
 *     shell's stacking crossing is a responsive class on one container — chosen precisely so that
 *     no measured breakpoint and no resize observer exists, and so that "crossing the threshold is
 *     a layout change, not a remount" holds by construction rather than by a test.
 *   · A PLAN CHAT HAS NO PANE. A conditionally rendered second panel would remount the group's
 *     children on every move between a plan chat and a build chat — and one of those children is
 *     the iframe holding the citizen's running app.
 *
 * What the library would have given for free is keyboard resizing and the right ARIA, so both are
 * supplied here rather than skipped: a `separator` with an orientation, a value, its bounds, and
 * arrow keys that move it.
 *
 * ═══ WHAT IT DRIVES ═══
 *
 * One custom property on the rail element, consumed only above the stacking threshold. Nothing
 * here measures anything: the pointer's own `clientX` is the width, clamped to the board's stops.
 */
import { useCallback, useRef, type PointerEvent as ReactPointerEvent, type KeyboardEvent } from 'react'
import { GripVertical } from 'lucide-react'
import { RAIL_KEY_STEP, RAIL_MAX, RAIL_MIN, clampRailWidth } from './railWidth'

export interface RailResizeHandleProps {
  /** The current width, in CSS pixels. Always within the board's stops. */
  width: number
  /** Called on every pointer move and every key press, with an already-clamped width. */
  onResize: (width: number) => void
  /** Called once when a drag or a key press ends, so the preference is written once. */
  onCommit: (width: number) => void
  /** The rail this handle sizes, for `aria-controls`. */
  controls: string
}

export default function RailResizeHandle({ width, onResize, onCommit, controls }: RailResizeHandleProps) {
  // THE LAST WIDTH THIS DRAG PRODUCED, so the commit writes what the citizen actually let go of
  // rather than what the last React render happened to have. A pointer-up can arrive in the same
  // frame as the move before it.
  const latest = useRef(width)
  latest.current = width

  /**
   * THE GESTURE IN FLIGHT, AND WHETHER IT EVER MOVED — the difference between a drag and a click.
   *
   * A remembered width replaces BOTH opening widths (see `railWidth.ts`), so committing on every
   * `pointerup` meant one stray click on the 9px divider inside a chat pinned every project screen
   * at the chat's 520px, without the citizen ever having dragged anything. A gesture that produced
   * no movement expressed no preference, and writing one is inventing an answer.
   *
   * It is also what makes the commit happen ONCE. Three events land in `end` — `pointerup`,
   * `pointercancel`, `lostpointercapture` — and releasing capture inside `end` queues a
   * `lostpointercapture` that re-enters it. Clearing the gesture first makes the echo a no-op,
   * while a capture genuinely lost mid-drag still arrives with the gesture intact and still
   * commits, which is the case the release below cannot cover.
   */
  const gesture = useRef<{ moved: boolean } | null>(null)

  const onPointerDown = useCallback((event: ReactPointerEvent<HTMLDivElement>) => {
    gesture.current = { moved: false }
    // POINTER CAPTURE, so a drag that leaves the 9px strip — which every drag does — keeps
    // receiving moves. Without it the boundary stops following the pointer the instant it crosses
    // into the pane, which reads as the handle being broken rather than bounded.
    event.currentTarget.setPointerCapture(event.pointerId)
    event.preventDefault()
  }, [])

  const onPointerMove = useCallback(
    (event: ReactPointerEvent<HTMLDivElement>) => {
      if (!event.currentTarget.hasPointerCapture(event.pointerId)) return
      if (gesture.current) gesture.current.moved = true
      // THE POINTER'S OWN X IS THE WIDTH. The rail starts at the viewport's left edge, under a
      // navbar and a toolbar row that take no horizontal space from it — so there is nothing to
      // measure and nothing that can go stale. A `getBoundingClientRect` here would be a
      // measurement the shell has gone to some trouble not to need.
      onResize(clampRailWidth(event.clientX))
    },
    [onResize],
  )

  const end = useCallback(
    (event: ReactPointerEvent<HTMLDivElement>) => {
      const ended = gesture.current
      // Already ended: the `lostpointercapture` this handler's own release queued, or an event
      // arriving outside a gesture this handle started.
      if (!ended) return
      gesture.current = null
      if (event.currentTarget.hasPointerCapture(event.pointerId)) {
        event.currentTarget.releasePointerCapture(event.pointerId)
      }
      // A PRESS THAT NEVER MOVED IS NOT A PREFERENCE. Nothing is written, and the rail keeps
      // whatever width it opened at.
      if (!ended.moved) return
      // LOSING CAPTURE MID-DRAG LANDS HERE TOO, which is why the commit reads the ref: whatever
      // the last move produced is a valid width, and the rail is left at it rather than snapped
      // back to where the drag started.
      onCommit(latest.current)
    },
    [onCommit],
  )

  const onKeyDown = useCallback(
    (event: KeyboardEvent<HTMLDivElement>) => {
      const step =
        event.key === 'ArrowLeft' ? -RAIL_KEY_STEP : event.key === 'ArrowRight' ? RAIL_KEY_STEP : 0
      if (step === 0) {
        // The two ends, which a keyboard user otherwise reaches by holding a key for 28 presses.
        if (event.key === 'Home') {
          event.preventDefault()
          onResize(RAIL_MIN)
          onCommit(RAIL_MIN)
        } else if (event.key === 'End') {
          event.preventDefault()
          onResize(RAIL_MAX)
          onCommit(RAIL_MAX)
        }
        return
      }
      event.preventDefault()
      const next = clampRailWidth(latest.current + step)
      onResize(next)
      onCommit(next)
    },
    [onCommit, onResize],
  )

  return (
    <div
      data-testid="rail-resize-handle"
      // A SEPARATOR WITH A VALUE, which is what makes this announceable at all. The library would
      // have supplied these; hand-building the handle means hand-building them too.
      role="separator"
      aria-orientation="vertical"
      aria-label="Resize the details column"
      aria-controls={controls}
      aria-valuenow={width}
      aria-valuemin={RAIL_MIN}
      aria-valuemax={RAIL_MAX}
      tabIndex={0}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={end}
      onPointerCancel={end}
      onLostPointerCapture={end}
      onKeyDown={onKeyDown}
      // HIDDEN BELOW THE STACKING THRESHOLD, where the columns are stacked and there is no
      // boundary to move — the board's own rule: it "disappears rather than becoming a control
      // that cannot help". `hidden` rather than a width of zero, so it leaves the tab order too.
      className="hidden wide:flex w-[9px] flex-shrink-0 cursor-col-resize touch-none select-none items-center justify-center border-x border-bial-border bg-canvas-track focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-primary/40"
    >
      <span className="flex h-9 w-[11px] items-center justify-center rounded-md border border-canvas-grip bg-white shadow-sm">
        <GripVertical size={13} className="text-canvas-placeholder" aria-hidden />
      </span>
    </div>
  )
}
