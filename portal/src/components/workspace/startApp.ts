/**
 * STARTING A PROJECT'S APP — one implementation, and the single-flight guard its three triggers
 * share.
 *
 * WHY THIS EXISTS
 *
 * The app starts three ways: because somebody opened the project, because somebody pressed the
 * control a failed start leaves behind, and because somebody sent the rail's first message. They
 * are the same request with the same ways to fail, and the classification below is the part that
 * must never differ between them.
 *
 * NONE OF THE THREE CAN SEE THE OTHERS, and all three report into one busy flag and one outcome
 * slot. A restore blocks for up to two minutes, which is exactly long enough for a citizen to type
 * a message over one already running — so `useStartApp` makes the second trigger JOIN the first
 * rather than fire a second request whose `finally` clears the first's busy state and whose answer
 * lands on top of it.
 */
import { useRef, useState } from 'react'
import { ApiError } from '../../utils/apiError'
import { BuildSessionAlreadyActiveError, relaunchPreview } from '../../utils/buildSessionApi'
import type { StartOutcome, StartResult } from './workspaceState'
import type { WorkspaceReport } from './workspaceChannel'

export const BUILD_ALREADY_RUNNING = 'A build is already running in this application.'

/**
 * EVERY PART OF THE REPORT A START WRITES INTO, and nothing else. A `WorkspaceReport` satisfies it,
 * so a caller holding one passes it straight through; naming the subset is what lets the guard
 * below wrap the sinks without also wrapping the state and the refresh, which it has no business
 * touching.
 */
export type StartSinks = Pick<
  WorkspaceReport,
  'projectId' | 'onStarted' | 'onStartPending' | 'onStartOutcome'
>

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
 * NOTHING SAVED TO BRING BACK IS NOT A FAILURE. The snapshot gate answers 404 with this code, and
 * a project that has never been built reaches it by design — the first message is what provisions
 * a workspace. The same endpoint answers an UNCODED 404 for a project that is deleted or is not
 * this citizen's, which is a real failure; the code is what separates them.
 */
function nothingToBringBack(err: unknown): boolean {
  return err instanceof ApiError && err.status === 404 && err.code === 'no_saved_build'
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
 * IT NEVER THROWS. The caller that must re-say a failure where the citizen is standing reads it
 * off the result; the two callers that must not be interrupted by one simply ignore it.
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
export async function startApp(sinks: StartSinks): Promise<StartResult> {
  const projectId = sinks.projectId
  // Nobody to ask: no request was made, so nothing failed.
  if (!projectId) return { kind: 'ok' }
  // THE SURFACE HEARS THE PRESS IMMEDIATELY, not on the next poll tick. The server's own
  // `starting` is the authority and it arrives later; this is what stops the sentence above the
  // pane saying nothing happened for up to forty-five seconds.
  sinks.onStartPending(true)
  try {
    const res = await relaunchPreview({ projectId })
    // THE URL FIRST, and before the outcome. It is what the surface frames, and reporting it
    // second would leave one commit in which the state says "running" and the pane has no address
    // to show for it. Handed over even when `ready` is false: the container is up and the document
    // is what has not arrived, so the frame's own load-gated reveal is the right thing to be
    // waiting on rather than a sentence in front of it.
    if (res.previewUrl) sinks.onStarted(res.previewUrl)
    // `ready === false` is "started but not painted yet", NOT "dead" — and an ABSENT `ready` reads
    // `true` by the wire's recorded contract, which is exactly why liveness can never hang off
    // this boolean. Safe here only because both sides of the read are non-destructive.
    sinks.onStartOutcome(res.ready ? null : { kind: 'not-painted' })
    return { kind: 'ok' }
  } catch (err) {
    // NOTHING TO RESTORE IS REPORTED AS NOTHING AT ALL — the snapshot gate's own 404, and
    // deliberately no blank-template arm. It returns BEFORE `onStartOutcome` below, which is what
    // keeps a failure sentence off the pane of a project whose app has simply never been built.
    if (nothingToBringBack(err)) return { kind: 'ok' }
    if (err instanceof BuildSessionAlreadyActiveError) {
      // Your own other chat is building. The server names it in wire terms, so this one refusal
      // is re-said in the citizen's — the remedy is to finish or stop that build.
      sinks.onStartOutcome({ kind: 'failed', reason: BUILD_ALREADY_RUNNING })
      return { kind: 'failed', error: err }
    }
    sinks.onStartOutcome(outcomeFor(err))
    return { kind: 'failed', error: err }
  } finally {
    sinks.onStartPending(false)
  }
}

/**
 * ONE START AT A TIME, AND ONE ANSWER TO IT — the claim all three triggers share.
 *
 * The returned function starts the app, or hands back the start already running for this project,
 * so a joiner waits for the real answer instead of provisioning a second container and exactly one
 * `finally` owns the busy flag.
 *
 * A GENERATION, THE SAME COUNTER THE PREVIEW POLLS USE, because the sinks belong to the SURFACE
 * while a start belongs to a PROJECT: the surface is not remounted when the screen moves to another
 * project, so a start still in the air must not report into the screen that replaced it, and its
 * claim must not be joined by a trigger for the project that arrived. `current` is read at REPORT
 * time for the same reason — the sinks of the moment are the ones that decide.
 */
export function createStarter(current: () => StartSinks): () => Promise<StartResult> {
  let running: Promise<StartResult> | null = null
  let claimedFor: string | null = null
  let generation = 0
  return () => {
    const claimingFor = current().projectId
    if (running !== null && claimedFor === claimingFor) return running
    const mine = ++generation
    claimedFor = claimingFor
    // OVERTAKEN MEANS SILENT, NOT CANCELLED. The request is made and the server will do what it
    // does; what must not happen is its answer landing on a screen showing something else — a
    // preview URL, a busy flag or a failure sentence belonging to the project that left.
    const stillOurs = () => mine === generation && claimingFor === current().projectId
    const guarded: StartSinks = {
      projectId: claimingFor,
      onStarted: (previewUrl) => {
        if (stillOurs()) current().onStarted(previewUrl)
      },
      onStartPending: (pending) => {
        if (stillOurs()) current().onStartPending(pending)
      },
      onStartOutcome: (outcome) => {
        if (stillOurs()) current().onStartOutcome(outcome)
      },
    }
    running = startApp(guarded).finally(() => {
      if (mine === generation) running = null
    })
    return running
  }
}

/**
 * The surface's own starter, created once and never replaced, reading whichever sinks the surface
 * holds at the moment it is called. Published on the workspace report, which is how the pane's
 * control and the rail's composer reach the same claim from two different columns.
 */
export function useStartApp(sinks: StartSinks): () => Promise<StartResult> {
  const latest = useRef(sinks)
  latest.current = sinks
  const [starter] = useState(() => createStarter(() => latest.current))
  return starter
}
