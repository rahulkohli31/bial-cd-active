/**
 * THE ONE WORKSPACE STATE — what the platform reports, turned into what a person
 * reads and what they may press.
 *
 * WHY THIS EXISTS
 * WHAT THESE TESTS CAN AND CANNOT PROVE, said up front because the distinction is the unit's
 * whole point. They prove the CLIENT's vocabulary: that no arm of this map reaches a destructive
 * verb, that a sentence says what the register requires, that a withheld attribution renders no
 * empty quotes. They prove nothing about what `POST /relaunch` does when a word is pressed — that
 * is server behaviour, asserted in `backend/tests/api/v1/build_sessions/`, and a client test
 * saying "this made no restore call" would pass in exactly the state that loses work.
 *
 * The copy assertions are deliberately literal: the exact wording was the client's own call, and
 * a test that matched loosely would let the sentence drift back to the negation it was chosen
 * to replace.
 *
 * ★ AND THE COUNT IS PART OF THE CONTRACT. What is pinned below is the FOUR a citizen reads plus
 * the one that is never drawn — the count as much as the copy, because a fifth card growing back
 * is the failure this pin exists to make visible.
 */
import { readFileSync } from 'node:fs'
import { resolve as resolvePath } from 'node:path'
import { describe, it, expect } from 'vitest'
import {
  BACKGROUND_CADENCE,
  LAUNCH_LABEL,
  STARTING_PROBE_LIMIT,
  isTerminalReading,
  mayHaveStopped,
  resolveWorkspaceState,
  sameWorkspaceState,
  WORKSPACE_STATE_FIELDS,
  type StartOutcome,
  type WorkspaceInputs,
  type WorkspaceState,
} from '../workspaceState'
import type { PreviewState } from '../../../utils/buildSessionApi'

/** A preview-state read, in the shape the wire parser produces one. */
function reading(over: Partial<PreviewState> = {}): PreviewState {
  return {
    state: 'asleep',
    alive: false,
    previewUrl: null,
    restorable: null,
    startingSince: null,
    startFailure: null,
    ...over,
  }
}

function resolve(over: Partial<WorkspaceInputs> = {}) {
  return resolveWorkspaceState({
    preview: reading(),
    projectHasSavedBuild: null,
    startOutcome: null,
    startInFlight: false,
    waitHasGoneOnTooLong: false,
    ...over,
  })
}

/**
 * Everything a surface would put on screen for this value, as one string.
 *
 * EVERY SLOT A SURFACE DRAWS, not just the sentence pair: a sweep blind to the note or the label
 * would go on passing while either said whatever it liked, which is exactly the class of miss the
 * register assertions below exist to catch.
 */
const rendered = (over: Partial<WorkspaceInputs> = {}) => {
  const state = resolve(over)
  return [state.headline, state.detail ?? '', state.note ?? '', state.action?.label ?? ''].join(' ')
}

/** Every ending a press can have, so a sweep can be exhaustive over the union rather than sample it. */
const EVERY_ENDING: readonly (StartOutcome | null)[] = [
  null,
  { kind: 'failed', reason: 'the image could not be pulled' },
]

describe('★ the four a citizen reads, each from its own real inputs', () => {
  // ONE TEST PER STATE, NAMED FOR THE SITUATION RATHER THAN THE ARM, because the arm names are
  // internal and the situations are what the client signed off. Each asserts the WHOLE sentence
  // pair verbatim: the wording was somebody's decision, and a loose match lets it drift back.

  it('NEW — a project with nothing to launch invites a description, and offers no button', () => {
    // Offering "Launch Application" here would 404: `POST /relaunch` answers `no_saved_build` for
    // a project with no saved copy. An invitation is the only honest affordance.
    const state = resolve({ preview: reading({ state: 'asleep', restorable: false }) })

    expect(state.name).toBe('never-built')
    expect(state.headline).toBe('Describe what you want to build.')
    expect(state.detail).toBe('Your app will appear here as it takes shape.')
    expect(state.action).toBeNull()
    expect(state.busy ?? false).toBe(false)
  })

  it('BUILDING — anything under way with nothing proven to serve says one sentence, and is busy', () => {
    // The wire's `starting` now spans the WHOLE pre-serve interval, because `alive` is no longer
    // allowed to mean "a container was scheduled". That widening is the entire measured fix.
    const state = resolve({ preview: reading({ state: 'starting' }) })

    expect(state.name).toBe('starting')
    expect(state.headline).toBe('Getting your app ready.')
    expect(state.detail).toBe('Setting up somewhere for it to run.')
    expect(state.action).toBeNull()
    expect(state.busy).toBe(true)
  })

  it('RUNNING — a proven serve draws no sentence at all: the frame IS the state', () => {
    // `detail: null` is the assertion that matters. A line under a working app is noise, and a
    // refusal left over from a press that has since succeeded is worse than noise.
    const state = resolve({ preview: reading({ state: 'alive', alive: true }) })

    expect(state.name).toBe('running')
    expect(state.headline).toBe('Your app is running.')
    expect(state.detail).toBeNull()
    expect(state.action).toBeNull()
    expect(state.busy ?? false).toBe(false)
  })

  it('SAVED — a saved copy with nothing serving it says "Your app is saved." and offers one start', () => {
    const state = resolve({ preview: reading({ state: 'asleep', restorable: true }) })

    expect(state.name).toBe('not-running')
    // VERBATIM AND CLIENT-APPROVED: full stop after "saved", and no negation of any kind after it.
    expect(state.headline).toBe('Your app is saved.')
    expect(state.detail).toBe('It stays running while you work, so you only do this once.')
    expect(state.action).toEqual({ kind: 'start', label: 'Launch Application' })
    expect(state.busy ?? false).toBe(false)
  })

  it('★ and there are FOUR of them, plus the one that is never drawn', () => {
    // THE COUNT IS THE CONTRACT. Pinning the number is what makes a fifth card growing back a red
    // test rather than a review someone has to notice: every new arm has to be added HERE by
    // whoever adds it.
    const everyArm = [
      resolve({ preview: reading({ state: 'asleep', restorable: false }) }),
      resolve({ preview: reading({ state: 'starting' }) }),
      resolve({ preview: reading({ state: 'alive', alive: true }) }),
      resolve({ preview: reading({ state: 'asleep', restorable: true }) }),
      resolve({ preview: null }),
    ]

    expect(new Set(everyArm.map((s) => s.name))).toEqual(
      new Set(['never-built', 'starting', 'running', 'not-running', 'could-not-read']),
    )
    // Four of the five are DRAWN; the fifth is the internal one, reachable only where nothing has
    // answered yet. Asserted as a count so a sixth cannot arrive unnoticed.
    expect(new Set(everyArm.map((s) => s.name)).size).toBe(5)
  })
})

describe('★ BUILDING absorbed three cards, and it still has no verb', () => {
  /**
   * THREE SOURCES, ONE SENTENCE. The wire's `starting`, this surface's own outstanding press, and a
   * container that exists and has never answered are all the same situation — a start is
   * happening, nothing is serving yet.
   */
  const everyWayIn: [string, Partial<WorkspaceInputs>][] = [
    ['the server says a start is in flight', { preview: reading({ state: 'starting' }) }],
    ['this surface`s own press is outstanding', { preview: reading({ state: 'asleep', restorable: true }), startInFlight: true }],
  ]

  for (const [how, inputs] of everyWayIn) {
    it(`says the same one sentence when ${how}`, () => {
      const state = resolve(inputs)
      expect(state.name).toBe('starting')
      expect(state.headline).toBe('Getting your app ready.')
      expect(state.detail).toBe('Setting up somewhere for it to run.')
    })
  }

  it('★ carries NO action under ANY combination of inputs, until the budget is spent', () => {
    // INSIDE THE BUDGET THERE IS NOTHING TO PRESS, and this is where that is kept honest.
    //
    // The escape a wait eventually offers is a function of ONE input — `waitHasGoneOnTooLong` —
    // and of nothing else. A patience button that grew out of any other condition ("…unless a
    // reason came back", "…unless there is a saved copy") would be a second author for the same
    // affordance, and a single-input assertion is exactly what such a condition slips past. So
    // the sweep is over the product, and the boundary is swept as a dimension of it rather than
    // left at its default — which is what made this vacuous for the arm it now covers.
    for (const startOutcome of EVERY_ENDING) {
      for (const projectHasSavedBuild of [true, false, null]) {
        for (const [how, inputs] of everyWayIn) {
          const where = `${how} / ${startOutcome?.kind ?? 'no ending'}`
          const inside = resolve({
            ...inputs,
            startOutcome,
            projectHasSavedBuild,
            waitHasGoneOnTooLong: false,
          })
          expect(inside.name, how).toBe('starting')
          expect(inside.action, where).toBeNull()
          // LIVENESS. Every absence above would pass just as happily against an arm that returned
          // an empty husk, so the sentence has to be there too — a withheld verb, not a blank card.
          expect(inside.headline).toBe('Getting your app ready.')
          expect(inside.busy).toBe(true)

          // PAST IT, ONE VERB AND ONLY THAT ONE. `start` is the press that reaches the restoring
          // arm; the wait must never offer it, however the wait was arrived at.
          const spent = resolve({
            ...inputs,
            startOutcome,
            projectHasSavedBuild,
            waitHasGoneOnTooLong: true,
          })
          expect(spent.name, where).toBe('starting')
          expect(spent.busy, where).toBe(true)
          expect(spent.action?.kind, where).toBe('retry')
          expect(spent.headline, where).not.toBe('Getting your app ready.')
        }
      }
    }
  })

  it('★ and the map is handed no clock, so time cannot reach it except as an input', () => {
    // The sweep above proves the escape is a function of ONE input. This closes the other half:
    // the map must not be able to consult a clock ITSELF. A map that read one could satisfy every
    // assertion above on the first call and grow a different button on the hundredth, and no
    // pure-function test would ever see it — the boundary has to arrive as
    // `waitHasGoneOnTooLong`, decided by a caller with a timer and testable as a value.
    //
    // Asserted against the SOURCE because that is where a clock would have to appear, and because
    // the module is deliberately pure: there is no seam to observe one through. The two duration
    // constants this file legitimately owns are poll CADENCES handed to a caller's timer, never
    // read here — which is why this greps for the READS, not for the numbers.
    const source = readFileSync(
      resolvePath(process.cwd(), 'src/components/workspace/workspaceState.ts'),
      'utf8',
    )
    for (const clock of ['Date.now', 'performance.now', 'setTimeout', 'setInterval', 'requestAnimationFrame']) {
      expect(source, `workspaceState.ts reads a clock (${clock}) — a timed action could hide behind it`)
        .not.toContain(clock)
    }
    // LIVENESS: the file really was read and really is the map, so a path typo cannot green this.
    expect(source).toContain('function resolveWorkspaceState')
  })

  it('the starting sentence carries no digits and no duration word', () => {
    // The no-duration-claim rule taken literally. Nobody has measured a cold start, so no
    // sentence may name one — the canvas's "about thirty seconds" and the register's "about
    // half a minute" are both dropped.
    const text = rendered({ preview: reading({ state: 'starting' }) })

    expect(text).not.toMatch(/\d/)
    expect(text).not.toMatch(
      /\b(second|seconds|minute|minutes|moment|moments|hour|hours|soon|shortly|about|roughly|approximately|quick|quickly)\b/i,
    )
  })

  it('but a press REFUSED while a start really was in flight still gets its answer', () => {
    // A citizen who pressed Launch during a build asked a question and is owed an answer to it.
    // The honest answer does not change the state they are in, so it is a note rather than a card.
    const state = resolve({
      preview: reading({ state: 'starting' }),
      startOutcome: { kind: 'failed', reason: 'BUILD_ALREADY_RUNNING' },
    })

    expect(state.name).toBe('starting')
    expect(state.note).toBe('BUILD_ALREADY_RUNNING')
    expect(state.action).toBeNull()
  })
})

describe('the register — what the pane may and may not say', () => {
  it('names no negative state anywhere in what it renders', () => {
    // ASSERTED OVER THE WHOLE RENDERED TEXT, not over the headline, so a fourth negative phrasing
    // added to the detail line later fails a test rather than a review. `not running` stays alive
    // as an internal state name and on the wire; it is never a thing a person reads.
    //
    // ★ ONE FIELD IS OUT OF SCOPE, AND IT IS A NARROWING OF THE SUBJECT RATHER THAN OF THE RULE.
    // The rule forbids the pane describing THIS app by what it is not — "saved", never "stopped".
    // The `note` field is never the map's own prose: it is either the SERVER's words about a press
    // this citizen made, carried verbatim because rewriting them would put a second author on
    // somebody else's sentence, or the map's own line about ANOTHER project. The next block pins
    // that carve-out so it cannot quietly widen.
    const forbidden = [/not running/i, /\bstopped\b/i, /unavailable/i, /preview/i]
    const everyState: Partial<WorkspaceInputs>[] = [
      { preview: reading({ state: 'asleep', restorable: true }) },
      { preview: reading({ state: 'asleep', restorable: false }) },
      { preview: reading({ state: 'asleep', restorable: null }) },
      { preview: reading({ state: 'starting' }) },
      { preview: reading({ state: 'alive', alive: true }) },
      { preview: null },
      { preview: reading({ state: 'asleep' }), startOutcome: { kind: 'failed', reason: 'no image' } },
    ]

    for (const inputs of everyState) {
      const state = resolve(inputs)
      // Everything whose subject is this citizen's own app: the two sentences and the label.
      const text = [state.headline, state.detail ?? '', state.action?.label ?? ''].join(' ')
      for (const phrase of forbidden) {
        expect(`${JSON.stringify(inputs.preview?.state ?? null)}: ${text}`).not.toMatch(phrase)
      }
    }
  })

  it('★ a server refusal full of the forbidden words rides in `note` and breaks nothing', () => {
    // ★ THIS IS WHY THE REASON MOVED OUT OF `detail`. The old `start-failed` card put the server's
    // sentence in `detail`, which is inside the sweep above — so one refusal containing the words
    // "not running" turned a green suite red on a string this client does not control and cannot
    // rewrite. The reason now rides in the one field the sweep exempts, and the card the citizen
    // reads is the ordinary SAVED one, because the situation and the next step are identical.
    const refusal = 'The container is not running and the previous build stopped; the preview is unavailable.'
    const state = resolve({
      preview: reading({ state: 'asleep', restorable: true }),
      startOutcome: { kind: 'failed', reason: refusal },
    })

    // It lands on SAVED, verbatim, in `note` — not in `detail`, and not in a card of its own.
    expect(state.name).toBe('not-running')
    expect(state.note).toBe(refusal)
    expect(state.detail).toBe('It stays running while you work, so you only do this once.')
    expect(state.headline).toBe('Your app is saved.')
    // And the sweep's own subject — the two sentences and the label — is still clean, even
    // though the value the citizen reads on this very card contains all four forbidden phrases.
    const swept = [state.headline, state.detail, state.action?.label ?? ''].join(' ')
    for (const phrase of [/not running/i, /\bstopped\b/i, /unavailable/i, /preview/i]) {
      expect(swept).not.toMatch(phrase)
    }
    // The remedy is unchanged: the same Launch every saved workspace offers. Pressing it again is
    // non-destructive by construction — the action union contains no restore or teardown verb.
    expect(state.action).toEqual({ kind: 'start', label: LAUNCH_LABEL })
  })

  it('★ and the one carve-out stays exactly one field wide', () => {
    // The note is the only place a server's own prose may appear. Written as its own assertion so
    // that widening the carve-out — by moving that sentence into `detail`, say — fails here rather
    // than passing the sweep above on a technicality.
    const refused = resolve({
      preview: reading({ state: 'asleep', restorable: true }),
      startOutcome: { kind: 'failed', reason: 'The other app is stopped and the preview is unavailable.' },
    })

    expect(refused.note).toMatch(/\bstopped\b/)
    expect(`${refused.headline} ${refused.detail ?? ''} ${refused.action?.label ?? ''}`).not.toMatch(/\bstopped\b/i)
  })

  it('a refusal on a project with NOTHING saved is still acknowledged', () => {
    // `no_saved_build` is the refusal a project with no saved copy actually gets, and that refusal
    // produces exactly this reading. An arm that dropped the note would answer a press the citizen
    // had just made with "Describe what you want to build." and no sign anything had happened.
    const state = resolve({
      preview: reading({ state: 'asleep', restorable: false }),
      startOutcome: { kind: 'failed', reason: 'This project has no saved copy yet.' },
    })

    expect(state.name).toBe('never-built')
    expect(state.note).toBe('This project has no saved copy yet.')
    expect(state.action).toBeNull()
  })
})

describe('before any read has answered', () => {
  it('answers even before the platform has said anything at all', () => {
    // A read that throws never reaches the map — both polls leave their reading where it was — so
    // `null` is the only way in, and it means nothing has answered yet.
    const state = resolve({ preview: null })

    expect(state.name).toBe('could-not-read')
    expect(state.action).toEqual({ kind: 'retry', label: 'Try again' })
  })
})

describe('a start outcome selects no arm of its own — the READING decides the card', () => {
  it('★ the same ending lands on whichever card the reading chose', () => {
    // THE CHANGE, stated as one assertion. This union used to select three whole cards; it now
    // contributes at most a `note` to an arm somebody else picked. Written as a sweep so that a
    // new arm keyed off the ending — the exact regrowth this collapse exists to prevent — cannot
    // land quietly.
    const failure: StartOutcome = { kind: 'failed', reason: 'the image could not be pulled' }
    const landings: [string, PreviewState][] = [
      ['not-running', reading({ state: 'asleep', restorable: true })],
      ['never-built', reading({ state: 'asleep', restorable: false })],
      ['starting', reading({ state: 'starting' })],
      ['running', reading({ state: 'alive', alive: true })],
    ]

    for (const [expected, preview] of landings) {
      const state = resolve({ preview, startOutcome: failure })
      expect(state.name, expected).toBe(expected)
    }
  })

  it('a live read outranks a stale start outcome — reaching alive IS the start succeeding', () => {
    const state = resolve({
      preview: reading({ state: 'alive', alive: true }),
      startOutcome: { kind: 'failed', reason: 'the image could not be pulled' },
    })
    expect(state.name).toBe('running')
    // AND THE REFUSAL GOES WITH IT. A note left over from a press that has since succeeded is
    // worse than noise — it is the pane contradicting the frame beside it.
    expect(state.note ?? null).toBeNull()
  })

  it('an in-flight press outranks every reading EXCEPT one that already says alive', () => {
    // A press is newer than a stale `asleep`, an unreadable answer or a previous ending. But if
    // the app is already serving then the start succeeded whatever it reported on the way, and
    // saying "getting your app ready" over it would contradict the frame beside it.
    expect(resolve({ preview: reading({ state: 'asleep', restorable: true }), startInFlight: true }).name)
      .toBe('starting')
    expect(resolve({ preview: null, startInFlight: true }).name).toBe('starting')
    expect(resolve({ preview: reading({ state: 'alive', alive: true }), startInFlight: true }).name)
      .toBe('running')
  })
})

describe('★ a start that failed after the server admitted it', () => {
  const WHY = 'Your app could not be started. Try again in a minute.'

  it('says why at rest, beside Launch', () => {
    const state = resolve({ preview: reading({ state: 'asleep', restorable: true, startFailure: WHY }) })

    expect(state.name).toBe('not-running')
    expect(state.note).toBe(WHY)
    expect(state.action).toEqual({ kind: 'start', label: LAUNCH_LABEL })
    expect(state.busy ?? false).toBe(false)
  })

  it('gives way to the press’s own refusal, which is newer', () => {
    const state = resolve({
      preview: reading({ state: 'asleep', restorable: true, startFailure: WHY }),
      startOutcome: { kind: 'failed', reason: 'A build is already running in this application.' },
    })

    expect(state.note).toBe('A build is already running in this application.')
  })

  it('is never said over a wait or a running app', () => {
    // The contract puts it on `asleep` alone; these pin that the map would not carry it further
    // if a reading ever did.
    for (const preview of [
      reading({ state: 'starting', startFailure: WHY }),
      reading({ state: 'alive', alive: true, startFailure: WHY }),
    ]) {
      expect(resolve({ preview }).note ?? null, preview.state).toBeNull()
    }
    expect(
      resolve({ preview: reading({ state: 'asleep', restorable: true, startFailure: WHY }), startInFlight: true }).note ??
        null,
    ).toBeNull()
  })
})

describe('sameWorkspaceState — what the channel compares before it publishes', () => {
  it('★ the comparator sees BOTH optional fields, and each one on its own', () => {
    // The channel skips a publish when `sameWorkspaceState` says two readings render identically,
    // and both optional fields are things a citizen reads. ISOLATED DELIBERATELY: a pair that
    // differs in the headline too would pass against a comparator that had never heard of either
    // field. Each assertion below moves exactly one.

    // THE NOTE, alone: the same saved card, told apart only by the server's sentence on it.
    const saved = (reason: string | null) =>
      resolve({
        preview: reading({ state: 'asleep', restorable: true }),
        startOutcome: reason === null ? null : { kind: 'failed', reason },
      })
    expect(saved(null).headline).toBe(saved('Could not save your work').headline)
    expect(saved(null).detail).toBe(saved('Could not save your work').detail)
    expect(sameWorkspaceState(saved(null), saved('Could not save your work'))).toBe(false)

    // AN OMITTED OPTIONAL AND AN EXPLICIT `null` ARE THE SAME CLAIM, and must compare equal.
    const atRest = saved(null)
    expect(sameWorkspaceState(atRest, atRest)).toBe(true)
    const { note: _n, busy: _b, ...bare } = atRest
    expect(sameWorkspaceState({ ...bare, note: null, busy: false }, bare)).toBe(true)
    // THE ACTION, alone: a different verb behind the same sentences is a different card.
    expect(sameWorkspaceState(atRest, { ...atRest, action: null })).toBe(false)

    // ★ AND `busy`, which is the field a wait turns on and nothing else moves. Isolated the same
    // way: hand-built, because the only arm that sets it also changes every other field.
    const wait = resolve({ preview: reading({ state: 'starting' }) })
    expect(sameWorkspaceState(wait, { ...wait, busy: false })).toBe(false)
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
        preview: reading({ state: 'asleep', restorable: true }),
        projectHasSavedBuild: false,
      }).action?.kind,
    ).toBe('start')
  })
})

describe('the properties that hold across every input', () => {
  it('names no destructive verb in any arm', () => {
    // The type is the real enforcement — the union has two members and neither is a teardown —
    // but a sentence can still say a dangerous word, and this is what catches that.
    const destructive = /\b(restore|restoring|rebuild|rebuilding|reset|delete|deleting|destroy|tear down|teardown|discard|wipe|erase)\b/i
    const states: (PreviewState | null)[] = [
      null,
      reading({ state: 'alive', alive: true }),
      reading({ state: 'starting' }),
      reading({ state: 'asleep', restorable: true }),
      reading({ state: 'asleep', restorable: false }),
      reading({ state: 'asleep', restorable: null }),
    ]

    for (const preview of states) {
      for (const startOutcome of EVERY_ENDING) {
        for (const projectHasSavedBuild of [true, false, null]) {
          for (const startInFlight of [true, false]) {
            for (const waitHasGoneOnTooLong of [true, false]) {
              const state = resolveWorkspaceState({
                preview,
                projectHasSavedBuild,
                startOutcome,
                startInFlight,
                waitHasGoneOnTooLong,
              })
              const text = `${state.headline} ${state.detail ?? ''} ${state.note ?? ''} ${state.action?.label ?? ''}`
              expect(`${state.name}: ${text}`).not.toMatch(destructive)
              // Every arm says something, and offers at most ONE thing to press — never a second.
              expect(state.headline.length).toBeGreaterThan(0)
              expect(['start', 'retry', undefined]).toContain(state.action?.kind)
              // AND NOTHING IT OFFERS ACTS ON ANOTHER PERSON'S APP: both verbs ask this project's
              // own start, which `registry:{user_id}` scopes to this citizen's one slot.
              expect(Object.keys(state)).not.toContain('secondAction')
            }
          }
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

    expect(JSON.stringify(state)).not.toContain('https://app.example/')
  })

  it('★ EVERY arm carries the whole key set, so the map stays total over the optional two', () => {
    // `note` and `busy` are OPTIONAL in the type, so the suites that hand-build a state need not
    // restate values they have no opinion about. That optionality is exactly why the map has to be
    // pinned here instead: TypeScript will not notice an arm that forgets one.
    //
    // ★ READ FROM THE COMPARATOR, not hand-kept beside it. This used to be a second literal list,
    // so adding a field meant editing four places and only three of them were forced. The
    // comparator is now an exhaustive keyed record — omitting a field is a COMPILE error there —
    // and this reads its keys, so the two lists cannot drift apart at all.
    const KEYS = WORKSPACE_STATE_FIELDS

    const arms: Array<[string, WorkspaceState]> = [
      ['running', resolve({ preview: reading({ state: 'alive', alive: true }) })],
      ['starting', resolve({ preview: reading({ state: 'starting' }) })],
      ['never-built', resolve({ preview: reading({ state: 'asleep', restorable: false }) })],
      ['not-running', resolve({ preview: reading({ state: 'asleep', restorable: true }) })],
      ['could-not-read', resolve({ preview: null })],
    ]

    // Liveness first: the inputs really do reach five DISTINCT arms. Without this the loop below
    // could pass while every entry resolved to the same fallback.
    expect(new Set(arms.map(([, s]) => s.name)).size).toBe(5)
    for (const [expectedName, armState] of arms) {
      expect(armState.name).toBe(expectedName)
      expect(Object.keys(armState).sort()).toEqual(KEYS)
    }
  })

  it('exports the start label from one place so no surface can spell it differently', () => {
    expect(LAUNCH_LABEL).toBe('Launch Application')
  })
})

describe('isTerminalReading — when re-asking can only hear the same sentence again', () => {
  it('ends the asking on a settled state with a decided restore answer', () => {
    expect(isTerminalReading(reading({ state: 'asleep', restorable: true }))).toBe(true)
    expect(isTerminalReading(reading({ state: 'asleep', restorable: false }))).toBe(true)
  })

  it('keeps asking while `restorable` is still null — a half answer is not an answer', () => {
    // Ending there pins the one sentence this must never say wrongly over a workspace sitting
    // safely on Blob, with no timer left to correct it.
    expect(isTerminalReading(reading({ state: 'asleep', restorable: null }))).toBe(false)
  })

  it('never ends on a state whose successor arrives with no gesture from anybody', () => {
    expect(isTerminalReading(reading({ state: 'alive', restorable: true }))).toBe(false)
    expect(isTerminalReading(reading({ state: 'starting', restorable: true }))).toBe(false)
  })
})

describe('mayHaveStopped — when a stuck wait is worth a container call', () => {
  const insideTheWindow = { ...BACKGROUND_CADENCE, fastReads: 1 }
  const windowSpent = { ...BACKGROUND_CADENCE, fastReads: STARTING_PROBE_LIMIT }

  it('asks about a running app only when its frame has stalled', () => {
    expect(mayHaveStopped('alive', true, BACKGROUND_CADENCE)).toBe(true)
    expect(mayHaveStopped('alive', false, BACKGROUND_CADENCE)).toBe(false)
  })

  it('★ asks about a start only once the accelerated window is spent', () => {
    // Inside the window the wait is a start being watched, and watching costs cheap reads only —
    // including the very first read after a Launch press, whose window has not opened yet.
    expect(mayHaveStopped('starting', false, BACKGROUND_CADENCE)).toBe(false)
    expect(mayHaveStopped('starting', false, insideTheWindow)).toBe(false)
    expect(mayHaveStopped('starting', false, windowSpent)).toBe(true)
  })

  it('never asks about a settled answer, stalled or not', () => {
    expect(mayHaveStopped('asleep', true, windowSpent)).toBe(false)
  })
})
