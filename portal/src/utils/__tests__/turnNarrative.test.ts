/**
 * What the surface asks ABOUT a turn — the phase the app pane reads, and whether today's budget
 * is spent.
 *
 * `turnPhase` REPLACES `narrativeStatus`, whose `isBuild` parameter had to be TOLD by a caller
 * that knew the chat's kind. One surface now serves both kinds and consults no kind anywhere,
 * so the frames answer instead. That parameter also only ever arrived as the literal
 * `true`, which made the read-turn arm unreachable in the shipped product — the arm is reachable
 * here, and asserted, for the first time.
 */
import { describe, it, expect } from 'vitest'

import { atLimitSendState, formatResetTime, turnPhase, type TurnNarrative } from '../turnNarrative'

const RESETS_AT = '2026-07-15T18:30:00.000Z'

const quota = () => ({ resetsAt: RESETS_AT })

/** An empty narrative — every field at the value it holds before any frame arrives. */
const narrative = (over: Partial<TurnNarrative> = {}): TurnNarrative => ({
  steps: {},
  diagnostics: [],
  workspace: null,
  preview: { url: null, state: null },
  ...over,
})

const step = (seq: number) => ({
  type: 'step' as const,
  seq,
  tool: 'write_file',
  label: 'Writing the page',
  state: 'ok' as const,
  hidden: false,
})

describe('turnPhase — nothing to say', () => {
  it('says nothing until a workspace frame has arrived', () => {
    // A turn that has not reported on the workspace has told us nothing about the app; inventing
    // a phase here would cover a live preview with a provisioning screen on every ordinary send.
    expect(turnPhase(narrative(), { running: true, terminal: null })).toBeNull()
    expect(turnPhase(narrative(), { running: false, terminal: 'completed' })).toBeNull()
  })

  it('an unavailable workspace is terminal, whatever else arrived', () => {
    // First in the order on purpose: a later arm claiming `building` over a workspace that could
    // not be prepared is the pane telling a citizen their app is being written when nothing is.
    expect(
      turnPhase(
        narrative({
          workspace: { state: 'unavailable', message: null },
          steps: { a: step(1) },
          preview: { url: 'https://app.example', state: 'ready' },
        }),
        { running: true, terminal: null },
      ),
    ).toBe('failed')
  })
})

describe('turnPhase — a turn that only answered a question', () => {
  it('reports the container wait while it is still happening, and nothing after it', () => {
    const preparing = narrative({ workspace: { state: 'preparing', message: null } })
    expect(turnPhase(preparing, { running: true, terminal: null })).toBe('provisioning')
    // Once the container is up, a read turn's remaining time belongs to the answer, not to a
    // progress claim about the app.
    const ready = narrative({ workspace: { state: 'ready', message: null } })
    expect(turnPhase(ready, { running: true, terminal: null })).toBeNull()
  })

  it('says nothing once it has finished, however it finished', () => {
    const preparing = narrative({ workspace: { state: 'preparing', message: null } })
    for (const terminal of ['completed', 'failed', 'stopped'] as const) {
      expect(turnPhase(preparing, { running: false, terminal })).toBeNull()
    }
  })

  it('a failed QUESTION does not paint the app pane failed', () => {
    // A question that errored says nothing about the app — reporting `failed` here would put a
    // build-failure treatment over an app that is running perfectly well.
    expect(
      turnPhase(narrative({ workspace: { state: 'ready', message: null } }), {
        running: false,
        terminal: 'failed',
      }),
    ).toBeNull()
  })
})

describe('turnPhase — a turn that worked on the app', () => {
  const working = (over: Partial<TurnNarrative> = {}) =>
    narrative({ workspace: { state: 'ready', message: null }, steps: { a: step(1) }, ...over })

  it('is building while it runs, and provisioning while the container is still coming up', () => {
    expect(turnPhase(working(), { running: true, terminal: null })).toBe('building')
    expect(
      turnPhase(working({ workspace: { state: 'preparing', message: null } }), {
        running: true,
        terminal: null,
      }),
    ).toBe('provisioning')
  })

  it('a live preview outranks "still provisioning" — the user can SEE it', () => {
    expect(
      turnPhase(
        working({
          workspace: { state: 'preparing', message: null },
          preview: { url: 'https://app.example', state: 'ready' },
        }),
        { running: true, terminal: null },
      ),
    ).toBe('ready')
  })

  it('★ carries its terminal: a failed turn failed, and NOTHING ELSE DID', () => {
    expect(turnPhase(working(), { running: false, terminal: 'completed' })).toBe('ended')
    expect(turnPhase(working(), { running: false, terminal: 'failed' })).toBe('failed')
    // A STOPPED TURN MAPS TO `ended`, NOT `failed`. The backend does not tear the container
    // down on a stop — it pardons it with no branch on how the turn ended: `finish_turn_sandbox`
    // is reached on the stopped arm and calls `_pardon_the_container` unconditionally ("THE
    // CONTAINER IS ALWAYS PARDONED", `manager.py`). So a stop leaves exactly what a completion
    // leaves: a running app. Mapping it to `failed` would collapse the pane to "The preview is
    // no longer running" over a container the server is deliberately keeping up.
    //
    // `ended` is not "a completed treatment". It is the phase for a turn that is OVER, and
    // nothing downstream reads success into it any more — the completion chip that once did
    // is deleted. What the pane may say about a half-written app comes from the compile state,
    // which reads the container rather than the terminal reason.
    expect(turnPhase(working(), { running: false, terminal: 'stopped' })).toBe('ended')
  })

  it('★ a stopped turn and a failed one are NOT the same phase — the distinction is the fix', () => {
    // The pairwise form, because the single-value assertions above would both stay green under a
    // mutant that mapped every terminal to one phase. Asserting they DIFFER is what forbids the
    // collapse coming back in either direction.
    const stopped = turnPhase(working(), { running: false, terminal: 'stopped' })
    const failed = turnPhase(working(), { running: false, terminal: 'failed' })
    expect(stopped).not.toBe(failed)
    // …and it is the FAILED one that is terminal-for-the-app, not the stopped one.
    expect(failed).toBe('failed')
  })

  it('says nothing once it is neither running nor terminal', () => {
    expect(turnPhase(working(), { running: false, terminal: null })).toBeNull()
  })

  it('recognises app work from a diagnostic or a preview alone, not only from steps', () => {
    // GENEROUS ON PURPOSE. Under-reading "did this touch the app?" leaves the pane uncovered over
    // a real build, which is the louder wrong of the two — so each of these frames is enough on
    // its own. Mutation check: narrow `touchedTheApp` to steps alone and both halves go red.
    const viaDiagnostic = narrative({
      workspace: { state: 'ready', message: null },
      diagnostics: [
        { type: 'diagnostic', seq: 1, source: 'tsc', userMessage: 'A page did not compile.', userAction: 'Retry.' },
      ],
    })
    expect(turnPhase(viaDiagnostic, { running: true, terminal: null })).toBe('building')

    const viaPreview = narrative({
      workspace: { state: 'ready', message: null },
      preview: { url: 'https://app.example', state: null },
    })
    expect(turnPhase(viaPreview, { running: false, terminal: 'completed' })).toBe('ended')
  })
})

describe('atLimitSendState', () => {
  it('the SEND control will not act, and its title names when sending works again', () => {
    // THE COMPOSER STAYS ENABLED — disabling the textarea would take a citizen's draft hostage
    // until midnight.
    //
    // Mutation check: return `null` unconditionally from `atLimitSendState` and this goes red.
    const state = atLimitSendState(quota())
    expect(state?.disabled).toBe(true)
    expect(state?.title).toMatch(/^You can send again after /)
    expect(state?.title).toContain(formatResetTime(RESETS_AT) as string)
  })

  it('says nothing about sending while the citizen still has budget', () => {
    // The state must be ABSENT rather than a disabled-false object: a composer that spreads it
    // unconditionally would otherwise refuse every ordinary turn.
    expect(atLimitSendState(null)).toBeNull()
  })
})

describe('formatResetTime', () => {
  it('degrades rather than printing "Invalid Date" into a citizen\'s banner', () => {
    // `resetsAt` is a wire value, and a naive `new Date(iso).toLocaleTimeString()` renders the
    // literal words "Invalid Date", which is worse than the caller's vaguer fallback.
    //
    // Mutation check: drop the `Number.isNaN` guard and this goes red.
    expect(formatResetTime('x')).toBeNull()
    expect(formatResetTime(RESETS_AT)).not.toBeNull()
    expect(formatResetTime(RESETS_AT)).not.toContain('Invalid')
  })

  it('and the caller says something true either way', () => {
    // The liveness half: `null` above must reach a sentence, not an empty title. The fallback is
    // true regardless of the wire value, because the reset IS the next IST midnight.
    const unusable = atLimitSendState({ ...quota(), resetsAt: 'x' })
    expect(unusable?.title).toBe('You can send again after midnight')
  })
})
