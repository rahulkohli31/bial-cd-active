/**
 * STARTING A PROJECT'S APP — one implementation, two callers.
 *
 * WHY THIS EXISTS
 * The app starts two ways now: because somebody opened the project, and because somebody pressed
 * the control a failed start leaves behind. They are the same request with the same ways to fail,
 * and the classification below is the part that must never differ between them.
 *
 * It is a plain async function rather than a hook because the auto-start calls it from an effect
 * and the control calls it from a press; neither needs React state from here. The caller owns its
 * own in-flight guard, because the two have different ones: a button collapses two presses in a
 * tick, and a mount effect must not fire twice for one arrival.
 */
import { ApiError } from '../../utils/apiError'
import { BuildSessionAlreadyActiveError, relaunchPreview } from '../../utils/buildSessionApi'
import type { StartOutcome } from './workspaceState'
import type { WorkspaceReport } from './workspaceChannel'

export const BUILD_ALREADY_RUNNING = 'A build is already running in this application.'

/** What the server itself said, or `null` when it said nothing a person could read. */
export function serverMessage(err: unknown): string | null {
  return err instanceof ApiError && err.message ? err.message : null
}

/**
 * Anything the server named, carried verbatim; anything it did not, called a timeout.
 *
 * A start that does not end in a running app says WHICH WAY it ended: "we waited and nothing came
 * back" is a different sentence from "the server said why".
 */
export function outcomeFor(err: unknown): StartOutcome {
  const reason = serverMessage(err)
  return reason === null ? { kind: 'timed-out' } : { kind: 'failed', reason }
}

/**
 * Start this project's app and route the answer to the surface.
 *
 * NOTHING IS REPORTED THROUGH A MOUNTED GUARD. Every handler here writes into the SURFACE — the
 * workspace read, the address, the outcome slot — all of which outlive whatever called this, and
 * all of which need the answer. The start control in particular unmounts routinely mid-flight: the
 * press makes the state `starting`, which offers no action, so the button that fired the request
 * is gone before the request comes back.
 *
 * NO REFUSAL OPENS A QUESTION HERE, AND `sandbox_reclaim_blocked` IS NOT AN EXCEPTION. The server
 * takes the one workspace for whichever project was asked for and tears the outgoing one down
 * behind it, so a citizen's own other project can no longer refuse this call. What still can is a
 * colleague's shared view sitting in the slot, and pressing start cannot move that — the server
 * refuses it whatever the citizen answers. So it lands where every other named refusal lands: the
 * server's own sentence, carried verbatim as a start failure with nothing to press. A refusal
 * arriving with `isSharedView` false would be the server contradicting its own switch, which is a
 * fault to fix behind the wire rather than a state to draw a screen for.
 */
export async function startApp(report: WorkspaceReport): Promise<void> {
  const projectId = report.projectId
  if (!projectId) return
  // THE SURFACE HEARS THE PRESS IMMEDIATELY, not on the next poll tick. The server's own
  // `starting` is the authority and it arrives later; this is what stops the sentence above the
  // pane saying nothing happened for up to forty-five seconds.
  report.onStartPending(true)
  try {
    const res = await relaunchPreview({ projectId })
    // THE URL FIRST, and before the outcome. It is what the surface frames, and reporting it
    // second would leave one commit in which the state says "running" and the pane has no address
    // to show for it. Handed over even when `ready` is false: the container is up and the document
    // is what has not arrived, so the frame's own load-gated reveal is the right thing to be
    // waiting on rather than a sentence in front of it.
    if (res.previewUrl) report.onStarted(res.previewUrl)
    // `ready === false` is "started but not painted yet", NOT "dead" — and an ABSENT `ready` reads
    // `true` by the wire's recorded contract, which is exactly why liveness can never hang off
    // this boolean. Safe here only because both sides of the read are non-destructive.
    report.onStartOutcome(res.ready ? null : { kind: 'not-painted' })
  } catch (err) {
    if (err instanceof BuildSessionAlreadyActiveError) {
      // Your own other chat is building. The server names it in wire terms, so this one refusal
      // is re-said in the citizen's — the remedy is to finish or stop that build.
      report.onStartOutcome({ kind: 'failed', reason: BUILD_ALREADY_RUNNING })
      return
    }
    report.onStartOutcome(outcomeFor(err))
  } finally {
    report.onStartPending(false)
  }
}
