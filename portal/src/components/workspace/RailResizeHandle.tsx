/**
 * THE BOUNDARY BETWEEN THE RAIL AND THE APP.
 *
 * WHY THIS EXISTS: the board names `react-resizable-panels`, but the plan overrules it for
 * reasons specific to this shell — a panel group takes its direction as a VALUE, not a class,
 * and applies sizes inline, while the shell's stacking crossing is a responsive class on one
 * container (so no measured breakpoint or resize observer exists, and crossing the threshold
 * is a layout change, not a remount, by construction); and a plan chat has no pane, so a
 * conditionally rendered second panel would remount the group's children — including the
 * iframe holding the citizen's running app — on every move between a plan and a build chat.
 * What the library would give for free (keyboard resizing, the right ARIA) is supplied here
 * instead: a `separator` with an orientation, a value, its bounds, and arrow keys.
 *
 * WHAT IT DRIVES: one custom property on the rail element, consumed only above the stacking
 * threshold. Nothing here measures anything — the pointer's own `clientX` is the width,
 * clamped to the board's stops.
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
   * THE GESTURE IN FLIGHT, and whether it moved — a drag vs. a click. Committing on every
   * `pointerup` pinned a screen at 520px on one stray divider click, nothing dragged: no
   * movement, no preference. Also fires the commit ONCE — `end` gets three events
   * (`pointerup`/`pointercancel`/`lostpointercapture`), and releasing capture inside it queues a
   * re-entrant `lostpointercapture`; clearing the gesture first makes that echo a no-op, while a
   * capture genuinely lost mid-drag still commits.
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
      aria-label="Resize the chat column"
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
