/**
 * What the surface asks ABOUT a turn — the phase the app pane reads, and whether today's budget
 * is spent (Plan D U17).
 *
 * BOTH FUNCTIONS USED TO LIVE IN COMPONENTS THAT NO LONGER EXIST, and both were pinned only
 * through those components' render output. `atLimitSendState` and `formatResetTime` were exported
 * from `BuildProgress.tsx`, and their pure-function cases moved here with them rather than dying
 * with the card — a relocated function keeps its tests, or the move quietly costs the coverage.
 *
 * `turnPhase` REPLACES `narrativeStatus`, whose `isBuild` parameter had to be TOLD by a caller
 * that knew the chat's kind. One surface now serves both kinds and consults no kind anywhere
 * (R72), so the frames answer instead. That parameter also only ever arrived as the literal
 * `true`, which made the read-turn arm unreachable in the shipped product — the arm is reachable
 * here, and asserted, for the first time.
 */
import { describe, it, expect } from 'vitest'

import { atLimitSendState, formatResetTime, turnPhase, type TurnNarrative } from '../turnNarrative'
import type { QuotaExceededEvent } from '../buildSessionTypes'

const RESETS_AT = '2026-07-15T18:30:00.000Z'

const quota = (seq = 3): QuotaExceededEvent => ({
  type: 'quota_exceeded',
  seq,
  limit: 1_000_000,
  used: 1_000_001,
  resets_at: RESETS_AT,
})

/** An empty narrative — every field at the value it holds before any frame arrives. */
const narrative = (over: Partial<TurnNarrative> = {}): TurnNarrative => ({
  steps: {},
  diagnostics: [],
  quota: null,
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
    // The pane keeps whatever it already had. A turn that has not reported on the workspace has
    // told us nothing about the app, and inventing a phase here would cover a live preview with a
    // provisioning screen on every ordinary send.
    expect(turnPhase(narrative(), { running: true, terminal: null })).toBeNull()
    expect(turnPhase(narrative(), { running: false, terminal: 'completed' })).toBeNull()
  })

  it('an unavailable workspace is terminal, whatever else arrived', () => {
    // First in the order on purpose: there is no phase after this one worth reporting, and a
    // later arm claiming `building` over a workspace that could not be prepared is the pane
    // telling a citizen their app is being written when nothing is.
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
  // THE ARM THAT WAS UNREACHABLE. `narrativeStatus` took `isBuild`, and its one caller passed the
  // literal `true`, so nothing in the shipped product could ever reach this. It is reachable now
  // because the FRAMES decide, and these are the cases that prove the decision is made on
  // evidence rather than on a chat's kind.

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
    // The distinction the old `isBuild` existed to make, now made by evidence: a question that
    // errored says nothing about the app, and reporting `failed` here would put a build-failure
    // treatment over an app that is running perfectly well.
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

  it('carries its terminal: completed ends, failed and stopped both fail', () => {
    expect(turnPhase(working(), { running: false, terminal: 'completed' })).toBe('ended')
    expect(turnPhase(working(), { running: false, terminal: 'failed' })).toBe('failed')
    // STOPPED IS A FAILURE TO THE PANE, deliberately: a build the citizen interrupted did not
    // finish, and an `ended` here would put a completed treatment over a half-written app.
    expect(turnPhase(working(), { running: false, terminal: 'stopped' })).toBe('failed')
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
      diagnostics: [{ source: 'tsc', userMessage: 'A page did not compile.', userAction: 'Retry.' }],
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
    // THE COMPOSER STAYS ENABLED — this describes the send control only. A citizen who is
    // refused mid-thought has usually just typed something worth keeping, and disabling the
    // textarea takes their draft hostage until midnight (and, per KTD-3, blurs focus to the
    // document body).
    //
    // Mutation check: return `null` unconditionally from `atLimitSendState` and this goes red.
    const state = atLimitSendState([quota()])
    expect(state?.disabled).toBe(true)
    expect(state?.title).toMatch(/^You can send again after /)
    expect(state?.title).toContain(formatResetTime(RESETS_AT) as string)
  })

  it('says nothing about sending while the citizen still has budget', () => {
    // The state must be ABSENT rather than a disabled-false object: a composer that spreads it
    // unconditionally would otherwise refuse every ordinary turn.
    expect(atLimitSendState([])).toBeNull()
    expect(
      atLimitSendState([{ type: 'step', seq: 1, name: 's', label: 'x', state: 'ok' }]),
    ).toBeNull()
  })

  it('takes the NEWEST reset time by seq, not the last envelope that happened to arrive', () => {
    // A reconnect replays the stream, so envelopes arrive out of order. Reading the last ARRIVED
    // envelope hands the citizen a stale reset time from a replayed frame — and "when can I send
    // again" is the only question this answers.
    //
    // The two instants differ in TIME OF DAY, not merely in date. `formatResetTime` renders a
    // clock time, so two different DATES at the same hour render identically and the assertion
    // would pass against either implementation — which is exactly what an earlier version of
    // this test did.
    //
    // Mutation check: pick the last array element instead of sorting by seq and this goes red.
    const stale: QuotaExceededEvent = { ...quota(9), resets_at: '2026-07-15T06:15:00.000Z' }
    const newest: QuotaExceededEvent = { ...quota(12), resets_at: '2026-07-15T18:30:00.000Z' }

    // Deliberately out of array order: newest first, stale last.
    const state = atLimitSendState([newest, stale])

    expect(state?.title).toContain(formatResetTime(newest.resets_at) as string)
    expect(state?.title).not.toContain(formatResetTime(stale.resets_at) as string)
  })
})

describe('formatResetTime', () => {
  it('degrades rather than printing "Invalid Date" into a citizen\'s banner', () => {
    // `resets_at` is a wire value, and a naive `new Date(iso).toLocaleTimeString()` renders the
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
    const unusable = atLimitSendState([{ ...quota(), resets_at: 'x' }])
    expect(unusable?.title).toBe('You can send again after midnight')
  })
})
