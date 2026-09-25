/**
 * THE ONE CONTROL THAT STARTS THE APP. Two action members exist — start and retry — and this
 * renders whichever it is handed; there is no third, so no unreadable signal reaches a teardown or
 * a restore from here. What the press does once it lands is the server's, and has its own tests.
 *
 * WHY THIS EXISTS
 *
 * `relaunchPreview` reaches a two-armed endpoint. ATTACH is safe: it reuses the live container and
 * keeps it when the app shows no page. RESTORE tears the container down before pulling the last saved
 * bundle, so a guard keeps an unreadable attach — the recorded data-loss path — out of it. A stale
 * `asleep` read stays reachable, the registry hash having no TTL, and this control answers it with
 * one start and whatever comes back, refusals included, never a retry or an invented recovery verb.
 *
 * `aria-disabled`, never `disabled`: disabling a focused control blurs it to `document.body` and
 * takes its name and reason with it. The VISIBLE label carries the state, where once only the
 * `aria-label` did over words reading "Launch Application" either way; that override is gone
 * rather than kept beside them, because a second name for one control is what WCAG's label-in-name
 * rule forbids. No live region: `LivePreview` owns one polite region for every pane state.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { PlayCircle, RotateCcw } from 'lucide-react'
import { BusyGlyph } from '../ui/Waiting'
import { assertNever } from '../../utils/assertNever'
import type { WorkspaceAction } from './workspaceState'
import type { WorkspaceReport } from './workspaceChannel'

export interface StartAppControlProps {
  action: WorkspaceAction
  report: WorkspaceReport
}

export default function StartAppControl({ action, report }: StartAppControlProps) {
  const [pending, setPending] = useState(false)
  // THE ONLY GUARD HERE IS ABOUT THIS COMPONENT. Two presses in one tick collapse to one request
  // because `report.start` is single-flight — the second press joins the first, which is also what
  // keeps this control from racing the project opening and the rail's send. `mounted` is a
  // different question: it keeps the `await` below from writing into a component the citizen has
  // already navigated away from.
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  const start = useCallback(async () => {
    if (!report.projectId) return
    setPending(true)
    try {
      await report.start()
    } finally {
      // `mounted` guards THIS control's own spinner and nothing else — everything the surface
      // needs was already reported inside the start, which unmounting must not skip.
      if (mounted.current) setPending(false)
    }
  }, [report])

  switch (action.kind) {
    case 'start':
      return (
        <Control
          label={action.label}
          pending={pending}
          pendingLabel="Starting your app"
          icon={<PlayCircle size={15} />}
          onPress={() => void start()}
        />
      )
    case 'retry':
      return (
        <Control
          label={action.label}
          pending={pending}
          pendingLabel="Trying again"
          icon={<RotateCcw size={15} />}
          onPress={() => {
            // A retry clears the last outcome and asks again, then starts. Clearing first matters:
            // otherwise a second failure of the same kind would leave the sentence unchanged and
            // the press would look like it did nothing.
            report.onStartOutcome(null)
            void start()
          }}
        />
      )
    default:
      return assertNever(action)
  }
}

interface ControlProps {
  label: string
  /** THIS control is the one working: it renames itself and spins. */
  pending: boolean
  pendingLabel: string
  icon: React.ReactNode
  onPress: () => void
}

function Control({ label, pending, pendingLabel, icon, onPress }: ControlProps) {
  return (
    <button
      type="button"
      // `aria-disabled`, NEVER `disabled` — see the docblock. The click handler checks the same
      // flag, so the control is inert without being unfocusable.
      aria-disabled={pending}
      // A property, not a speech: it marks the control as working without announcing anything,
      // which is what keeps this off the pane's live region.
      aria-busy={pending}
      onClick={() => {
        if (!pending) onPress()
      }}
      className={`inline-flex items-center gap-2 rounded-xl px-5 py-2.5 text-sm font-bold transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40 bg-primary text-white shadow-sm shadow-primary/30 hover:bg-primary-600 ${
        pending ? 'opacity-60' : ''
      }`}
    >
      {pending ? <BusyGlyph size={15} /> : icon}
      {/* THE WORDS ARE WHAT CHANGES. With no `aria-label` over the top, this is also the
          accessible name — so the button renames itself from "Launch Application" to "Starting
          your app…" as it goes, and a reader on the control hears the change. */}
      {pending ? `${pendingLabel}…` : label}
    </button>
  )
}
