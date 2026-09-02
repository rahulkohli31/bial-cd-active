/**
 * THE ONE WORKSPACE STATE (Plan F, U2) — what the platform reports, turned into what a person
 * reads and what they may press.
 *
 * WHAT THESE TESTS CAN AND CANNOT PROVE, said up front because the distinction is the unit's
 * whole point. They prove the CLIENT's vocabulary: that no arm of this map reaches a destructive
 * verb, that a sentence says what the register requires, that a withheld attribution renders no
 * empty quotes. They prove nothing about what `POST /relaunch` does when a word is pressed — that
 * is server behaviour, asserted in `backend/tests/api/v1/build_sessions/`, and a client test
 * saying "this made no restore call" would pass in exactly the state that loses work.
 *
 * The copy assertions are deliberately literal. R-16 was a client call on exact wording, and a
 * test that matched loosely would let the sentence drift back to the negation it was chosen to
 * replace.
 */
import { describe, it, expect } from 'vitest'
import {
  LAUNCH_LABEL,
  isTerminalReading,
  resolveWorkspaceState,
  type WorkspaceInputs,
} from '../workspaceState'
import type { PreviewState } from '../../../utils/buildSessionApi'

/** A preview-state read, in the shape the wire parser produces one. */
function reading(over: Partial<PreviewState> = {}): PreviewState {
  return {
    state: 'asleep',
    alive: false,
    previewUrl: null,
    occupyingProjectName: null,
    occupyingProjectId: null,
    restorable: null,
    ...over,
  }
}

function resolve(over: Partial<WorkspaceInputs> = {}) {
  return resolveWorkspaceState({
    preview: reading(),
    projectHasSavedBuild: null,
    startOutcome: null,
    startInFlight: false,
    ...over,
  })
}

/** Everything a surface would put on screen for this value, as one string. */
const rendered = (over: Partial<WorkspaceInputs> = {}) => {
  const state = resolve(over)
  return [state.headline, state.detail ?? '', state.action?.label ?? ''].join(' ')
}

describe('the register — what the pane may and may not say', () => {
  it('AE1: a saved, not-running project says "Your app is saved." and offers exactly one start', () => {
    const state = resolve({ preview: reading({ state: 'asleep', restorable: true }) })

    expect(state.name).toBe('not-running')
    // VERBATIM. R-16 was a client call on this exact sentence — full stop after "saved", and no
    // negation of any kind after it.
    expect(state.headline).toBe('Your app is saved.')
    expect(state.action).toEqual({ kind: 'start', label: 'Launch Application' })
  })

  it('names no negative state anywhere in what it renders', () => {
    // ASSERTED OVER THE WHOLE RENDERED TEXT, not over the headline, so a fourth negative phrasing
    // added to the detail line later fails a test rather than a review. `not running` stays alive
    // as an internal state name and on the wire; it is never a thing a person reads.
    const forbidden = [/not running/i, /\bstopped\b/i, /unavailable/i, /preview/i]
    const everyState: Partial<WorkspaceInputs>[] = [
      { preview: reading({ state: 'asleep', restorable: true }) },
      { preview: reading({ state: 'asleep', restorable: false }) },
      { preview: reading({ state: 'never_built', restorable: false }) },
      { preview: reading({ state: 'starting' }) },
      { preview: reading({ state: 'alive', alive: true }) },
      { preview: reading({ state: 'unknown' }) },
      { preview: null },
      {
        preview: reading({
          state: 'slot_taken',
          occupyingProjectName: 'Car pool apps',
          occupyingProjectId: 'proj-9',
        }),
      },
      { preview: reading({ state: 'slot_taken' }) },
      { preview: reading({ state: 'asleep' }), startOutcome: { kind: 'not-painted' } },
      { preview: reading({ state: 'asleep' }), startOutcome: { kind: 'timed-out' } },
      { preview: reading({ state: 'asleep' }), startOutcome: { kind: 'failed', reason: 'no image' } },
    ]

    for (const inputs of everyState) {
      const text = rendered(inputs)
      for (const phrase of forbidden) {
        expect(`${JSON.stringify(inputs.preview?.state ?? null)}: ${text}`).not.toMatch(phrase)
      }
    }
  })

  it('AE2: nothing built invites a description and offers NO action', () => {
    const state = resolve({ preview: reading({ state: 'never_built', restorable: false }) })

    expect(state.name).toBe('never-built')
    expect(state.action).toBeNull()
    // An invitation, not a report of an absence.
    expect(state.headline).toMatch(/describe what you want to build/i)
  })

  it('AE36: the starting sentence carries no digits and no duration word', () => {
    // R4a taken literally. Nobody has measured a cold start, so no sentence may name one — the
    // canvas's "about thirty seconds" and the register's "about half a minute" are both dropped.
    const text = rendered({ preview: reading({ state: 'starting' }) })

    expect(text).not.toMatch(/\d/)
    expect(text).not.toMatch(
      /\b(second|seconds|minute|minutes|moment|moments|hour|hours|soon|shortly|about|roughly|approximately|quick|quickly)\b/i,
    )
    expect(resolve({ preview: reading({ state: 'starting' }) }).action).toBeNull()
  })
})

describe('the hand-over states — two arms, and neither is an error', () => {
  it('AE31: with a name and an id, it names that project and offers the way to it', () => {
    const state = resolve({
      preview: reading({
        state: 'slot_taken',
        occupyingProjectName: 'Car pool apps',
        occupyingProjectId: 'proj-9',
      }),
    })

    expect(state.name).toBe('held-by-another-project')
    expect(state.headline).toContain('Car pool apps')
    expect(state.action).toEqual({
      kind: 'go-to-project',
      label: 'Open “Car pool apps”',
      projectId: 'proj-9',
    })
  })

  it('AE31: with the attribution withheld, it names none, quotes nothing and offers no action', () => {
    // A first-class wire state, not a bug to paper over: the server declines to attribute a
    // container it cannot map to a project this person owns. The failure this is written against
    // is a sentence with an empty pair of quotes in it.
    const state = resolve({ preview: reading({ state: 'slot_taken' }) })

    expect(state.name).toBe('held-unattributed')
    expect(state.action).toBeNull()
    expect(rendered({ preview: reading({ state: 'slot_taken' }) })).not.toMatch(/[“"]\s*[”"]/)
    expect(state.headline).toMatch(/another project is using your workspace/i)
  })

  it('offers no go-to when only half the attribution arrived — a button to nowhere is worse', () => {
    const nameOnly = resolve({
      preview: reading({ state: 'slot_taken', occupyingProjectName: 'Roster' }),
    })
    const idOnly = resolve({ preview: reading({ state: 'slot_taken', occupyingProjectId: 'p-9' }) })

    expect(nameOnly.action).toBeNull()
    expect(idOnly.action).toBeNull()
  })

  it('a held slot outranks a start outcome — the remedy, never a retry (R4b)', () => {
    // A retry against an occupied slot can only fail the same way again.
    const state = resolve({
      preview: reading({
        state: 'slot_taken',
        occupyingProjectName: 'Roster',
        occupyingProjectId: 'p-9',
      }),
      startOutcome: { kind: 'timed-out' },
    })

    expect(state.action?.kind).toBe('go-to-project')
  })
})

describe('R4b — a start that did not end in a running app says which way it ended', () => {
  it('AE3: an unreadable state answers "we could not check" and offers the retry member', () => {
    const state = resolve({ preview: reading({ state: 'unknown' }) })

    expect(state.name).toBe('could-not-read')
    expect(state.action?.kind).toBe('retry')
  })

  it('gives the three endings three distinct sentences, all landing on retry', () => {
    const notPainted = resolve({ startOutcome: { kind: 'not-painted' } })
    const timedOut = resolve({ startOutcome: { kind: 'timed-out' } })
    const failed = resolve({ startOutcome: { kind: 'failed', reason: 'the image could not be pulled' } })

    expect([notPainted.name, timedOut.name, failed.name]).toEqual([
      'not-painted',
      'timed-out',
      'start-failed',
    ])
    for (const state of [notPainted, timedOut, failed]) expect(state.action?.kind).toBe('retry')
    // Three sentences, not one shrug wearing three names.
    expect(new Set([notPainted.headline, timedOut.headline, failed.headline]).size).toBe(3)
  })

  it("carries the server's named reason verbatim rather than rewriting it", () => {
    const state = resolve({ startOutcome: { kind: 'failed', reason: 'the image could not be pulled' } })
    expect(state.detail).toBe('the image could not be pulled')
  })

  it('a live read outranks a stale start outcome — reaching alive IS the start succeeding', () => {
    const state = resolve({
      preview: reading({ state: 'alive', alive: true }),
      startOutcome: { kind: 'timed-out' },
    })
    expect(state.name).toBe('running')
  })
})

describe('the restore question, and the one answer that suppresses the start control', () => {
  it('falls through a null `restorable` to the project row rather than retracting its claim', () => {
    // `??`, never `||`: the tri-state's null is "no claim" — the object store was unreachable —
    // and treating it as "no" would retract an answer the project row already gave confidently.
    const state = resolve({
      preview: reading({ state: 'asleep', restorable: null }),
      projectHasSavedBuild: true,
    })
    expect(state.name).toBe('not-running')
    expect(state.action?.kind).toBe('start')
  })

  it('a definite `false` suppresses the start — the endpoint would only 404 there', () => {
    // The server holds neither a recovery copy nor a saved bundle, so "Launch Application" is a
    // button whose only outcome is an error. What is left is the same affordance as a project with
    // nothing built: ask for the app.
    const state = resolve({ preview: reading({ state: 'asleep', restorable: false }) })

    expect(state.name).toBe('never-built')
    expect(state.action).toBeNull()
  })

  it('a fresher `restorable` outranks a stale project row in both directions', () => {
    expect(
      resolve({
        preview: reading({ state: 'asleep', restorable: false }),
        projectHasSavedBuild: true,
      }).action,
    ).toBeNull()
    expect(
      resolve({
        preview: reading({ state: 'never_built', restorable: true }),
        projectHasSavedBuild: false,
      }).action?.kind,
    ).toBe('start')
  })
})

describe('the properties that hold across every input', () => {
  it('names no destructive verb in any arm', () => {
    // The type is the real enforcement — the union has three members and none of them is a
    // teardown — but a sentence can still say a dangerous word, and this is what catches that.
    const destructive = /\b(restore|restoring|rebuild|rebuilding|reset|delete|deleting|destroy|tear down|teardown|discard|wipe|erase)\b/i
    const states: (PreviewState | null)[] = [
      null,
      reading({ state: 'alive', alive: true }),
      reading({ state: 'starting' }),
      reading({ state: 'unknown' }),
      reading({ state: 'asleep', restorable: true }),
      reading({ state: 'asleep', restorable: false }),
      reading({ state: 'never_built', restorable: null }),
      reading({ state: 'slot_taken', occupyingProjectName: 'A', occupyingProjectId: 'p' }),
      reading({ state: 'slot_taken' }),
    ]
    const outcomes = [null, { kind: 'not-painted' }, { kind: 'timed-out' }, { kind: 'failed', reason: 'x' }] as const

    for (const preview of states) {
      for (const startOutcome of outcomes) {
        for (const projectHasSavedBuild of [true, false, null]) {
          const state = resolveWorkspaceState({ preview, projectHasSavedBuild, startOutcome, startInFlight: false })
          const text = `${state.headline} ${state.detail ?? ''} ${state.action?.label ?? ''}`
          expect(`${state.name}: ${text}`).not.toMatch(destructive)
          // Every arm says something, and offers at most one thing to press.
          expect(state.headline.length).toBeGreaterThan(0)
          expect(['start', 'retry', 'go-to-project', undefined]).toContain(state.action?.kind)
        }
      }
    }
  })

  it('carries no address — the map answers what to SAY, never what to frame', () => {
    // Framing `PreviewState.previewUrl` because it is conveniently in hand silently drops the top
    // of the address precedence: the live turn's preview, which is the app being built in front of
    // the person. There is no field here to put a URL in, which is the enforcement.
    const state = resolve({
      preview: reading({ state: 'alive', alive: true, previewUrl: 'https://app.example/' }),
    })

    expect(Object.keys(state).sort()).toEqual(['action', 'detail', 'headline', 'name'])
    expect(JSON.stringify(state)).not.toContain('https://app.example/')
  })

  it('answers even before the platform has said anything', () => {
    // Not an empty pane: the honest sentence before the first read is that we have not asked yet,
    // and the retry is the only thing a person can usefully do with that.
    const state = resolve({ preview: null })
    expect(state.name).toBe('could-not-read')
    expect(state.action?.kind).toBe('retry')
  })

  it('exports the start label from one place so no surface can spell it differently', () => {
    expect(LAUNCH_LABEL).toBe('Launch Application')
  })
})

describe('isTerminalReading — when re-asking can only hear the same sentence again', () => {
  it('ends the asking on a settled state with a decided restore answer', () => {
    for (const state of ['asleep', 'slot_taken', 'never_built'] as const) {
      expect(isTerminalReading(reading({ state, restorable: true }))).toBe(true)
      expect(isTerminalReading(reading({ state, restorable: false }))).toBe(true)
    }
  })

  it('keeps asking while `restorable` is still null — a half answer is not an answer', () => {
    // Ending there pins the one sentence this must never say wrongly over a workspace sitting
    // safely on Blob, with no timer left to correct it.
    for (const state of ['asleep', 'slot_taken', 'never_built'] as const) {
      expect(isTerminalReading(reading({ state, restorable: null }))).toBe(false)
    }
  })

  it('never ends on an answer that decided nothing', () => {
    expect(isTerminalReading(reading({ state: 'unknown', restorable: true }))).toBe(false)
    expect(isTerminalReading(reading({ state: 'alive', restorable: true }))).toBe(false)
    expect(isTerminalReading(reading({ state: 'starting', restorable: true }))).toBe(false)
  })
})
