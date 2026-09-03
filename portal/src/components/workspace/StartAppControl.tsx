/**
 * THE ONE CONTROL THAT STARTS THE APP (Plan F, U3).
 *
 * ═══ IT RENDERS THE MAP'S ACTION, AND THE MAP HAS NO DESTRUCTIVE VERB ═══
 *
 * Three action members exist — start, retry, and go to the project that holds the workspace — and
 * this component renders whichever one it is handed. There is no fourth, so no unreadable signal
 * can reach a teardown or a restore FROM HERE. That is a fact about the client's vocabulary and
 * this file will not claim more: what `POST /relaunch` does when the word is pressed is the
 * server's, proved in `backend/tests/api/v1/build_sessions/test_preview_state.py`.
 *
 * ═══ KNOW WHAT IS ON THE OTHER END OF THIS BUTTON ═══
 *
 * `relaunchPreview` → `POST /v1/build-sessions/relaunch` → `relaunch_preview`, which has two arms.
 * The ATTACH arm is safe: it reuses the live container, and since the SL-20 fix it fails open on a
 * readiness timeout rather than marking the registry `ending`. The RESTORE arm is not: it tears the
 * live container down before pulling the last saved bundle. This plan added the guard that keeps an
 * unreadable attach OUT of the restore arm, because that is the arm this control enters and it was
 * the recorded data-loss path with the guard missing.
 *
 * A stale-registry read of `asleep` against a container that is in fact live is still reachable —
 * the registry hash has no TTL, so an API restart orphans live containers. This control's job there
 * is to issue one ordinary start and surface whatever the server answers, INCLUDING a refusal. It
 * does not retry on its own, escalate, or offer a recovery verb: the container's survival in that
 * case is the server's to guarantee, and a client that invented a remedy would be guessing.
 *
 * ═══ MARKED UNAVAILABLE, NEVER DISABLED ═══
 *
 * `aria-disabled`, not `disabled`. Disabling a control that currently has focus blurs it to
 * `document.body`, which drops a keyboard user out of the interface at the exact moment something
 * is happening. The name and the reason stay on it throughout.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Loader2, PlayCircle, RotateCcw, ArrowRight } from 'lucide-react'
import {
  BuildSessionAlreadyActiveError,
  asReclaimBlocked,
  relaunchPreview,
} from '../../utils/buildSessionApi'
import { ApiError } from '../../utils/apiError'
import { assertNever } from '../../utils/assertNever'
import type { StartOutcome, WorkspaceAction } from './workspaceState'
import type { WorkspaceReport } from './workspaceChannel'

export interface StartAppControlProps {
  action: WorkspaceAction
  report: WorkspaceReport
}

export default function StartAppControl({ action, report }: StartAppControlProps) {
  const navigate = useNavigate()
  const [pending, setPending] = useState(false)
  // TWO GUARDS, AND THEY ARE NOT THE SAME GUARD. The ref is synchronous, so two presses in one
  // tick collapse to one request — state would not have committed between them. `mounted` is what
  // keeps every `await` below from writing into a component the citizen has already navigated away
  // from, which is L6's rule for a start sequence.
  const inFlight = useRef(false)
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  const start = useCallback(async () => {
    const projectId = report.projectId
    if (!projectId || inFlight.current) return
    inFlight.current = true
    setPending(true)
    // THE PANE HEARS THE PRESS IMMEDIATELY, not on the next poll tick. The server's own `starting`
    // is the authority and it arrives later; this is what stops the sentence above this button
    // saying nothing happened for up to forty-five seconds.
    report.onStartPending(true)
    try {
      const res = await relaunchPreview({ projectId })
      // NO MOUNTED GUARD BEFORE THE REPORT, and the distinction is the bug it was written as.
      //
      // `mounted` protects THIS component's own state. The report's handlers write into the
      // SURFACE — the workspace read, the address, the outcome slot — all of which outlive this
      // button and all of which need the answer. And this control unmounts routinely mid-flight:
      // the moment the press reaches the map, the state becomes `starting`, which offers no
      // action, so the button that fired the request is gone before the request comes back.
      //
      // Guarding here meant a start that SUCCEEDED reported nothing: no URL for the pane to frame,
      // no outcome to clear the wait. The screen sat on "Getting your app ready." forever while a
      // perfectly good container served underneath it.
      // THE URL FIRST, and before the outcome. It is what the surface frames, and reporting it
      // second would leave one commit in which the state says "running" and the pane has no
      // address to show for it. Handed over even when `ready` is false: the container is up and
      // the document is what has not arrived, so the frame's own load-gated reveal is the right
      // thing to be waiting on rather than a sentence in front of it.
      if (res.previewUrl) report.onStarted(res.previewUrl)
      // `ready === false` is "started but not painted yet", NOT "dead" — and an ABSENT `ready`
      // reads `true` by the wire's recorded contract, which is exactly why liveness can never hang
      // off this boolean. Safe here only because both sides of the read are non-destructive.
      report.onStartOutcome(res.ready ? null : { kind: 'not-painted' })
    } catch (err) {
      // Same reasoning as the success path above: a refusal has to reach the surface whether or
      // not the button that provoked it is still on screen.
      // DISCRIMINATED ON THE CODE BEFORE ANYTHING ELSE. A bare 409 is not self-describing: it
      // fires for a same-project reattach and for a cross-project block, and the two have
      // different remedies.
      const blocked = asReclaimBlocked(err)
      if (blocked) {
        // Another project holds the one workspace. Routed to the ONE dialog rather than shown as
        // a retry — retrying against an occupied slot can only fail the same way again.
        report.onReclaimRefusal(blocked, start)
        return
      }
      if (err instanceof BuildSessionAlreadyActiveError) {
        // Your own other chat is building. A different cause with a different remedy — finish or
        // stop it — so it must not be merged into the reclaim dialog, which would offer a Save
        // button that cannot help.
        report.onStartOutcome({ kind: 'failed', reason: 'A build is already running in this project.' })
        return
      }
      report.onStartOutcome(outcomeFor(err))
    } finally {
      inFlight.current = false
      report.onStartPending(false)
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
    case 'go-to-project':
      return (
        <Control
          label={action.label}
          pending={false}
          pendingLabel=""
          icon={<ArrowRight size={15} />}
          onPress={() => navigate(`/projects/${action.projectId}`)}
        />
      )
    default:
      return assertNever(action)
  }
}

/**
 * Anything the server named, carried verbatim; anything it did not, called a timeout.
 *
 * The distinction is R4b's: a start that does not end in a running app says WHICH WAY it ended, and
 * "we waited and nothing came back" is a different sentence from "the server said why".
 */
function outcomeFor(err: unknown): StartOutcome {
  // An `ApiError` means the server answered and said something. Anything else — an aborted fetch,
  // a network failure, a body that would not parse — means nothing came back, which is a different
  // sentence and a different thing to have happened.
  if (err instanceof ApiError && err.message) return { kind: 'failed', reason: err.message }
  return { kind: 'timed-out' }
}

interface ControlProps {
  label: string
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
      aria-label={pending ? `${label} — ${pendingLabel}` : label}
      onClick={() => {
        if (!pending) onPress()
      }}
      className={`inline-flex items-center gap-2 rounded-xl bg-primary px-5 py-2.5 text-sm font-bold text-white shadow-sm shadow-primary/30 transition hover:bg-primary-600 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40 ${
        pending ? 'opacity-60' : ''
      }`}
    >
      {pending ? <Loader2 size={15} className="animate-spin" /> : icon}
      {label}
    </button>
  )
}
