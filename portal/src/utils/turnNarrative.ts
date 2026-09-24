/**
 * What the surface asks ABOUT a turn.
 *
 * The two questions the surface asks of a turn — `turnPhase` (app pane) and `atLimitSendState`
 * (composer) — both live here so neither caller has to learn this module's vocabulary.
 */
import type { DiagnosticFrame, StepItem } from './turnStreamApi'
import type { BuildSessionStatus } from './buildSessionTypes'

export interface TurnNarrative {
  /** Live steps, keyed by tool-call id so the `finished` frame REPLACES its `started` one. */
  steps: Record<string, StepItem>
  diagnostics: DiagnosticFrame[]
  workspace: { state: 'preparing' | 'ready' | 'unavailable'; message: string | null } | null
  preview: { url: string | null; state: 'ready' | 'reconnecting' | null }
}

/**
 * The phase this turn is in, in the status vocabulary the app pane reads. `null` means nothing
 * to say — the pane keeps whatever it already had. Ordering is deliberate: an unavailable
 * workspace is terminal no matter what else arrived, and a live preview outranks "still
 * provisioning" because the user can SEE it. Takes no chat-kind parameter — the distinction
 * between app work and a read-only answer is read off the frames themselves (see
 * `touchedTheApp` below), not declared by a caller.
 */
export function turnPhase(
  narrative: TurnNarrative,
  {
    running,
    terminal,
  }: {
    running: boolean
    terminal: 'completed' | 'failed' | 'stopped' | null
  }
): BuildSessionStatus | null {
  if (narrative.workspace === null) return null
  if (narrative.workspace.state === 'unavailable') return 'failed'
  // DID THIS TURN TOUCH THE APP? Generous on purpose — every one of these is a frame only a turn
  // doing app work emits, and under-reading it would leave the pane uncovered over a real build,
  // which is the louder wrong of the two. A question about a heading produces none of them.
  const touchedTheApp =
    Object.keys(narrative.steps).length > 0 ||
    narrative.diagnostics.length > 0 ||
    narrative.preview.url !== null ||
    narrative.preview.state !== null
  if (!touchedTheApp) {
    // A read turn has exactly one thing worth narrating: the 30-60s wait for its container,
    // while it is still happening. Everything after it belongs to the answer.
    return running && narrative.workspace.state === 'preparing' ? 'provisioning' : null
  }
  // A TURN THAT FAILED IS THE ONLY ONE THAT FAILED. This read
  // `terminal === 'failed' || terminal === 'stopped'`, and that one line was the whole of the bug:
  // a citizen who pressed Stop watched their running app collapse to "The preview is no longer
  // running" over a container the backend had deliberately kept up. The backend does not make this
  // distinction on the container at all — `finish_turn_sandbox` pardons it with no branch on how
  // the turn ended ("THE CONTAINER IS ALWAYS PARDONED", `manager.py`) — so a stop and a completion
  // leave exactly the same thing serving, and only the phase said otherwise.
  //
  // STOPPED IS ITS OWN PHASE, and `ended` is what that phase is: the turn is over. `ended` is not a
  // claim that the build SUCCEEDED — nothing in this vocabulary makes that claim, and the pane no
  // longer draws a completion chip off it. What the pane may say about a half-written app comes
  // from the compile state, which reports on what is actually in the container rather than on how
  // its turn finished. Reading `ended` as "it worked" is the conflation this whole group removes.
  if (terminal === 'failed') return 'failed'
  if (terminal === 'completed' || terminal === 'stopped') return 'ended'
  if (!running) return null
  if (narrative.preview.state === 'ready') return 'ready'
  return narrative.workspace.state === 'preparing' ? 'provisioning' : 'building'
}

/** What the composer's SEND control does while the citizen is out of budget. */
export interface AtLimitSendState {
  /** Always true — the value exists so the call site reads as what it sets, not as a bare flag. */
  disabled: true
  /** The `title`, naming when sending starts working again. A control that will not act and does
   *  not say why is the single most frustrating state a UI can be in: it looks broken, and the
   *  reader has no way to tell whether waiting would help. */
  title: string
}

/**
 * The SEND control's state while today's budget is spent — `null` when it is not. Describes the
 * SEND control only, never the composer: a citizen refused mid-thought usually has a draft worth
 * keeping, so the textarea stays live to select/copy/paste — take the composer down too and the
 * draft is hostage until midnight.
 */
export function atLimitSendState(quota: { resetsAt: string } | null): AtLimitSendState | null {
  if (!quota) return null
  const when = formatResetTime(quota.resetsAt)
  return {
    disabled: true,
    title: when ? `You can send again after ${when}` : 'You can send again after midnight',
  }
}

/**
 * `resetsAt` as a time a person can read, or `null` when it is not a usable instant. FALLS
 * BACK rather than throwing: an unparseable wire value has already reached a renderer in the
 * existing tests — `new Date('x').toLocaleTimeString()` renders the literal "Invalid Date" into
 * the citizen's banner, worse than saying nothing. The caller's "after midnight" fallback is
 * true regardless, since the reset IS the next IST midnight.
 */
export function formatResetTime(isoUtc: string): string | null {
  const at = new Date(isoUtc)
  if (Number.isNaN(at.getTime())) return null
  return at.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })
}
