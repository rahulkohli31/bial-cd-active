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
 * ★ AND THE COUNT IS NOW PART OF THE CONTRACT. This file used to pin TEN textually distinct arms.
 * Five of them were hedges against a lie on the wire — `alive` meant a container had been
 * SCHEDULED, never that anything had watched the app ANSWER a request — so the map kept a second
 * opinion of its own ("up but not painted", "did not answer in time", "we could not start your
 * app", a second held arm, a sentence about our own plumbing). The backend proves the serve before
 * it says `alive`, the hedges have nothing left to hedge, and the arms they carried collapse into
 * the wait that was always the honest answer. What is pinned below is the FIVE a citizen reads
 * plus the one that is never drawn — and the pin is the count as much as the copy, because a
 * sixth card growing back is the failure this collapse exists to make visible.
 */
import { readFileSync } from 'node:fs'
import { resolve as resolvePath } from 'node:path'
import { describe, it, expect } from 'vitest'
import {
  BACKGROUND_CADENCE,
  LAUNCH_LABEL,
  STARTING_PROBE_LIMIT,
  asDecidedReading,
  isTerminalReading,
  mayHaveStopped,
  resolveWorkspaceState,
  sameWorkspaceState,
  WORKSPACE_STATE_FIELDS,
  type DecidedPreview,
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
    occupyingProjectName: null,
    occupyingProjectId: null,
    restorable: null,
    ...over,
  }
}

/**
 * A reading the platform actually stood behind — the shape the map REMEMBERS across reads.
 *
 * Narrowed through the module's own `asDecidedReading` rather than cast, so a test cannot hand the
 * memory slot an `unknown` that the product could never put there. The non-null assertion is safe
 * by construction and would throw loudly here if it stopped being: every caller below passes a
 * decided state.
 */
function settled(over: Partial<PreviewState> = {}): DecidedPreview {
  const decided = asDecidedReading(reading(over))
  if (decided === null) throw new Error('a settled reading may not be `unknown`')
  return decided
}

function resolve(over: Partial<WorkspaceInputs> = {}) {
  return resolveWorkspaceState({
    preview: reading(),
    lastDecidedPreview: null,
    projectHasSavedBuild: null,
    startOutcome: null,
    startInFlight: false,
    ...over,
  })
}

/**
 * Everything a surface would put on screen for this value, as one string.
 *
 * ALL FOUR SLOTS, not just the two the map used to have. A sweep that read only the headline,
 * the detail and the first action would have gone on passing while the take-back's label and the
 * stopped-holder note said whatever they liked — which is exactly the class of miss the register
 * assertions below exist to catch.
 */
const rendered = (over: Partial<WorkspaceInputs> = {}) => {
  const state = resolve(over)
  return [
    state.headline,
    state.detail ?? '',
    state.note ?? '',
    state.action?.label ?? '',
    state.secondAction?.label ?? '',
  ].join(' ')
}

/** A workspace held by a named project — the arm with a second control. */
const heldBy = (name = 'Car pool apps', id = 'proj-9') =>
  reading({ state: 'slot_taken', occupyingProjectName: name, occupyingProjectId: id })

/** Every ending a press can have, so a sweep can be exhaustive over the union rather than sample it. */
const EVERY_ENDING: readonly (StartOutcome | null)[] = [
  null,
  { kind: 'not-painted' },
  { kind: 'timed-out' },
  { kind: 'failed', reason: 'the image could not be pulled' },
  { kind: 'take-back-failed', reason: 'Could not save your work', stoppedHolder: null },
  { kind: 'take-back-failed', reason: 'Could not save your work', stoppedHolder: 'Roster' },
]

describe('★ the five a citizen reads, each from its own real inputs', () => {
  // ONE TEST PER STATE, NAMED FOR THE SITUATION RATHER THAN THE ARM, because the arm names are
  // internal and the situations are what the client signed off. Each asserts the WHOLE sentence
  // pair verbatim: the wording was somebody's decision, and a loose match lets it drift back.

  it('NEW — a project with nothing to launch invites a description, and offers no button', () => {
    // Offering "Launch Application" here would 404: `POST /relaunch` answers `no_saved_build` for
    // a project with no saved copy. An invitation is the only honest affordance.
    const state = resolve({ preview: reading({ state: 'never_built', restorable: false }) })

    expect(state.name).toBe('never-built')
    expect(state.headline).toBe('Describe what you want to build.')
    expect(state.detail).toBe('Your app will appear here as it takes shape.')
    expect(state.action).toBeNull()
    expect(state.secondAction ?? null).toBeNull()
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
    expect(state.secondAction ?? null).toBeNull()
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

  it('HELD — another of this citizen`s projects holding the one workspace names it', () => {
    const state = resolve({ preview: heldBy('Car pool', 'proj-9') })

    expect(state.name).toBe('held-by-another-project')
    expect(state.headline).toBe('“Car pool” is using your workspace.')
    expect(state.detail).toBe(
      'You have one workspace at a time. Open that project to pick up where you left off.',
    )
    expect(state.busy ?? false).toBe(false)
  })

  it('★ and there are FIVE of them, plus the one that is never drawn — not ten', () => {
    // THE COUNT IS THE CONTRACT. This assertion used to read `.toBe(10)`, and five of those ten
    // were second opinions about a wire value nobody could trust. Pinning the number is what makes
    // a sixth card growing back a red test rather than a review someone has to notice: every new
    // arm has to be added HERE, beside the reason the collapse happened, by whoever adds it.
    const everyArm = [
      resolve({ preview: reading({ state: 'never_built', restorable: false }) }),
      resolve({ preview: reading({ state: 'starting' }) }),
      resolve({ preview: reading({ state: 'alive', alive: true }) }),
      resolve({ preview: reading({ state: 'asleep', restorable: true }) }),
      resolve({ preview: heldBy() }),
      resolve({ preview: reading({ state: 'unknown' }) }),
    ]

    expect(new Set(everyArm.map((s) => s.name))).toEqual(
      new Set(['never-built', 'starting', 'running', 'not-running', 'held-by-another-project', 'could-not-read']),
    )
    // Five of the six are DRAWN; the sixth is the internal one, reachable only where nothing has
    // ever been decided. Asserted as a count so a seventh cannot arrive unnoticed.
    expect(new Set(everyArm.map((s) => s.name)).size).toBe(6)
  })
})

describe('★ BUILDING absorbed three cards, and it still has no verb', () => {
  /**
   * FOUR SOURCES, ONE SENTENCE. The wire's `starting`, this surface's own outstanding press, a
   * relaunch that came back `ready:false`, and a container that exists and has never answered are
   * all the same situation — a start is happening, nothing is serving yet.
   */
  const everyWayIn: [string, Partial<WorkspaceInputs>][] = [
    ['the server says a start is in flight', { preview: reading({ state: 'starting' }) }],
    ['this surface`s own press is outstanding', { preview: reading({ state: 'asleep', restorable: true }), startInFlight: true }],
    ['a relaunch answered ready:false', { preview: reading({ state: 'starting' }), startOutcome: { kind: 'not-painted' } }],
    ['the press got no answer inside budget', { preview: reading({ state: 'starting' }), startOutcome: { kind: 'timed-out' } }],
  ]

  for (const [how, inputs] of everyWayIn) {
    it(`says the same one sentence when ${how}`, () => {
      const state = resolve(inputs)
      expect(state.name).toBe('starting')
      expect(state.headline).toBe('Getting your app ready.')
      expect(state.detail).toBe('Setting up somewhere for it to run.')
    })
  }

  it('★ carries NO action under ANY combination of inputs that reaches it — decision D2', () => {
    // THERE IS NO PATIENCE BUTTON, and this is where that decision is kept honest.
    //
    // The obvious kindness is a "Launch Application" appearing after a long enough wait so the
    // wait is never a dead end. It is not offered because of WHERE that press would land:
    // `relaunch_preview`'s cold arm tears the live container down before restoring the last saved
    // bundle, and the situation such a button exists for — a start whose observer was lost — is
    // exactly the situation that takes the cold arm. The button would be most dangerous at the
    // precise moment it appeared. The escape is server-side instead.
    //
    // ASSERTED EXHAUSTIVELY OVER THE PRODUCT rather than on one input, because a patience escape
    // would arrive as a condition — "…unless a reason came back", "…unless there is a saved copy"
    // — and a single-input assertion is exactly what such a condition slips past.
    for (const startOutcome of EVERY_ENDING) {
      for (const projectHasSavedBuild of [true, false, null]) {
        for (const [how, inputs] of everyWayIn) {
          const state = resolve({ ...inputs, startOutcome, projectHasSavedBuild })
          expect(state.name, how).toBe('starting')
          expect(state.action, `${how} / ${startOutcome?.kind ?? 'no ending'}`).toBeNull()
          expect(state.secondAction ?? null, how).toBeNull()
          // LIVENESS. Every absence above would pass just as happily against an arm that returned
          // an empty husk, so the sentence has to be there too — a withheld verb, not a blank card.
          expect(state.headline).toBe('Getting your app ready.')
          expect(state.busy).toBe(true)
        }
      }
    }
  })

  it('★ and the map is handed no clock, so a TIMED action cannot exist here at all', () => {
    // The behavioural sweep above proves no action for any INPUT. This closes the other half: a
    // patience button is a function of elapsed TIME, and time is not an input to this module. A
    // map that read a clock could satisfy every assertion above on the first call and grow a
    // button on the hundredth, and no pure-function test would ever see it.
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

  it('a press that ended with nothing to report changes nothing a person reads', () => {
    // `not-painted` and `timed-out` are the two endings with no server prose behind them, and the
    // reason they say nothing is not politeness: one describes the state the citizen is already
    // in, and the other is a fact about a FETCH, which is not evidence about a workspace.
    const bare = resolve({ preview: reading({ state: 'starting' }) })
    for (const kind of ['not-painted', 'timed-out'] as const) {
      const withEnding = resolve({ preview: reading({ state: 'starting' }), startOutcome: { kind } })
      expect(sameWorkspaceState(bare, withEnding), kind).toBe(true)
    }
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
      { preview: reading({ state: 'never_built', restorable: false }) },
      { preview: reading({ state: 'starting' }) },
      { preview: reading({ state: 'alive', alive: true }) },
      { preview: reading({ state: 'unknown' }) },
      { preview: null },
      { preview: reading({ state: 'unknown' }), lastDecidedPreview: settled({ state: 'asleep', restorable: true }) },
      { preview: heldBy() },
      { preview: reading({ state: 'slot_taken' }) },
      { preview: reading({ state: 'asleep' }), startOutcome: { kind: 'not-painted' } },
      { preview: reading({ state: 'asleep' }), startOutcome: { kind: 'timed-out' } },
      { preview: reading({ state: 'asleep' }), startOutcome: { kind: 'failed', reason: 'no image' } },
      {
        preview: heldBy(),
        startOutcome: { kind: 'take-back-failed', reason: 'no image', stoppedHolder: 'Car pool apps' },
      },
    ]

    for (const inputs of everyState) {
      const state = resolve(inputs)
      // Everything whose subject is this citizen's own app: the two sentences, and both labels.
      const text = [
        state.headline,
        state.detail ?? '',
        state.action?.label ?? '',
        state.secondAction?.label ?? '',
      ].join(' ')
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
    // And the sweep's own subject — the two sentences and both labels — is still clean, even
    // though the value the citizen reads on this very card contains all four forbidden phrases.
    const swept = [state.headline, state.detail, state.action?.label ?? '', state.secondAction?.label ?? ''].join(' ')
    for (const phrase of [/not running/i, /\bstopped\b/i, /unavailable/i, /preview/i]) {
      expect(swept).not.toMatch(phrase)
    }
    // The remedy is unchanged: the same Launch every saved workspace offers. Pressing it again is
    // non-destructive by construction — the action union contains no restore or teardown verb.
    expect(state.action).toEqual({ kind: 'start', label: LAUNCH_LABEL })
  })

  it('★ and the one carve-out stays exactly one field wide', () => {
    // The note is the only place "stopped" may appear, it appears only where a take-back stopped
    // somebody, and it names them. Written as its own assertion so that widening the carve-out —
    // by moving that sentence into `detail`, say — fails here rather than passing the sweep above
    // on a technicality.
    const stopped = resolve({
      preview: heldBy('Roster', 'p-9'),
      startOutcome: { kind: 'take-back-failed', reason: 'Could not save your work', stoppedHolder: 'Roster' },
    })

    expect(stopped.note).toMatch(/\bstopped\b/)
    expect(stopped.note).toContain('“Roster”')
    expect(`${stopped.headline} ${stopped.detail ?? ''}`).not.toMatch(/\bstopped\b/i)
  })

  it('a refusal on a project with NOTHING saved is still acknowledged', () => {
    // `no_saved_build` is the refusal a project with no saved copy actually gets, and that refusal
    // produces exactly this reading. An arm that dropped the note would answer a press the citizen
    // had just made with "Describe what you want to build." and no sign anything had happened.
    const state = resolve({
      preview: reading({ state: 'never_built', restorable: false }),
      startOutcome: { kind: 'failed', reason: 'This project has no saved copy yet.' },
    })

    expect(state.name).toBe('never-built')
    expect(state.note).toBe('This project has no saved copy yet.')
    expect(state.action).toBeNull()
  })
})

/**
 * ★ DECISION D3 — AN UNREADABLE READ NEVER CHANGES THE PANE.
 *
 * `could-not-read` was proposed for deletion outright. It survives as an INTERNAL arm, and the
 * reason is what these tests are: without it a coordination-store blip falls through to the
 * at-rest arms, where `restorable` is null (the object store was not consulted) and
 * `projectHasSavedBuild` is still null on a cold load — so a Redis hiccup printed "Describe what
 * you want to build." over a project whose app may be serving right now, with no action at all.
 */
describe('★ an unreadable read renders the LAST SETTLED reading — decision D3', () => {
  it('★ a standing frame stays framed: unknown over a remembered `alive` still says RUNNING', () => {
    const state = resolve({
      preview: reading({ state: 'unknown' }),
      lastDecidedPreview: settled({ state: 'alive', alive: true }),
    })

    expect(state.name).toBe('running')
    expect(state.headline).toBe('Your app is running.')
    // MUTATION RECEIPT: drop the `?? lastDecidedPreview` fallback and this answers `could-not-read`
    // — "We could not check on your app." over an app the citizen is looking at.
  })

  it('★ a standing card stays put: unknown over a remembered `asleep` still says SAVED', () => {
    const state = resolve({
      preview: reading({ state: 'unknown' }),
      lastDecidedPreview: settled({ state: 'asleep', restorable: true }),
    })

    expect(state.name).toBe('not-running')
    expect(state.headline).toBe('Your app is saved.')
    expect(state.action?.kind).toBe('start')
  })

  it('★ and only with NO settled reading at all does the fallback sentence appear', () => {
    // The other half, and it is what keeps the arm honest rather than merely surviving: showing
    // "we could not check" to somebody whose app is fine is the failure; showing it to somebody
    // about whom the platform has genuinely never learned anything is simply the truth.
    const state = resolve({ preview: reading({ state: 'unknown' }), lastDecidedPreview: null })

    expect(state.name).toBe('could-not-read')
    expect(state.headline).toBe('We could not check on your app.')
    expect(state.detail).toBe('Nothing has changed while we were asking.')
    expect(state.action?.kind).toBe('retry')
    // NOT BUSY. A read that failed is not work in progress, and saying it is would put a wait on
    // screen with nothing behind it.
    expect(state.busy ?? false).toBe(false)
  })

  it('answers even before the platform has said anything at all', () => {
    // Not an empty pane: the honest sentence before the first read is that we have not heard, and
    // the retry is the only thing a person can usefully do with that.
    const state = resolve({ preview: null, lastDecidedPreview: null })
    expect(state.name).toBe('could-not-read')
    expect(state.action?.kind).toBe('retry')
  })

  it('★ the memory covers every settled reading, not only the two above', () => {
    // Written as a sweep because the rule is about the READ deciding nothing, not about which
    // answer happens to be remembered — an implementation that special-cased `alive` would pass
    // the first test in this block and still move the pane on a blip over a held workspace.
    const remembered: [string, DecidedPreview][] = [
      ['running', settled({ state: 'alive', alive: true })],
      ['starting', settled({ state: 'starting' })],
      ['not-running', settled({ state: 'asleep', restorable: true })],
      ['never-built', settled({ state: 'never_built', restorable: false })],
      ['held-by-another-project', settled({ state: 'slot_taken', occupyingProjectName: 'Roster', occupyingProjectId: 'p-9' })],
    ]

    for (const [expected, lastDecidedPreview] of remembered) {
      const blipped = resolve({ preview: reading({ state: 'unknown' }), lastDecidedPreview })
      const standing = resolve({ preview: lastDecidedPreview, lastDecidedPreview })
      expect(blipped.name, expected).toBe(expected)
      // THE WHOLE VALUE, not just the name: "the pane does not move" is a claim about every
      // sentence and every button on it, and a name-only assertion would pass a card that kept
      // its arm and lost its remedy.
      expect(sameWorkspaceState(blipped, standing), expected).toBe(true)
    }
  })

  it('a read that DID decide something outranks the memory, in both directions', () => {
    // The memory is a fallback, never a ceiling. Without this, a pane that had once seen `alive`
    // would keep saying so after the container was reaped.
    const wasAlive = settled({ state: 'alive', alive: true })
    expect(resolve({ preview: reading({ state: 'asleep', restorable: true }), lastDecidedPreview: wasAlive }).name)
      .toBe('not-running')
    expect(resolve({ preview: reading({ state: 'alive', alive: true }), lastDecidedPreview: settled({ state: 'asleep' }) }).name)
      .toBe('running')
  })
})

/**
 * ★ DECISION D4 — HELD KEEPS TODAY'S BUTTON ORDER.
 *
 * The design proposed promoting the take-back to `action` and demoting the go-to. It is not taken.
 * `PlanChatWorkspaceLine` narrows on `action.kind === 'go-to-project'` and never reads
 * `secondAction`, so the swap would render the held card with NO BUTTON on the one surface whose
 * whole job is to send somebody elsewhere — and it would make the consequential verb the lead
 * control on a card nobody navigated to.
 */
describe('★ the hand-over state — one card, two sentences, and the order is decision D4', () => {
  it('★ with a name and an id: go-to LEADS, take-back is the alternative, in that order', () => {
    const state = resolve({ preview: heldBy() })

    expect(state.action).toEqual({
      kind: 'go-to-project',
      label: 'Open “Car pool apps”',
      projectId: 'proj-9',
    })
    expect(state.secondAction).toEqual({
      kind: 'take-back',
      label: 'Stop “Car pool apps” and open this app instead',
    })
    // ★ THE ORDER, ASSERTED AS AN ORDER rather than as two independent facts. Swap the two slots
    // and both assertions above could be rewritten to pass; this one cannot, because it says which
    // verb the surfaces LEAD with — and the Plan chat reads only the leader.
    expect([state.action?.kind, state.secondAction?.kind]).toEqual(['go-to-project', 'take-back'])
  })

  it('★ with the attribution withheld it names nobody, quotes nothing, and still offers a way out', () => {
    // A first-class wire state, not a bug to paper over: the server declines to attribute a
    // container it cannot map to a project this person owns, because naming the WRONG project in
    // a sentence about somebody's work is worse than naming none. The failure this arm is written
    // against is a sentence with an empty pair of quotes in it.
    //
    // IT USED TO BE A DEAD END — a second state offering `action` and `secondAction` both null: a
    // card that named the problem, named no remedy, and left nothing to press. A missing holder
    // name is a reason to say LESS, not to DO less.
    const state = resolve({ preview: reading({ state: 'slot_taken' }) })

    expect(state.name).toBe('held-by-another-project')
    expect(state.headline).toBe('Another project is using your workspace.')
    expect(state.detail).toBe('You have one workspace at a time, and we could not tell which project has it.')
    expect(rendered({ preview: reading({ state: 'slot_taken' }) })).not.toMatch(/[“"]\s*[”"]/)
    // The take-back leads because it is the ONLY one, not because it was promoted — there is no
    // go-to to lead with, and `AppPane` draws its second control INSIDE the first one's block, so
    // a lone alternative parked in `secondAction` would be a remedy nothing renders.
    expect(state.action).toEqual({
      kind: 'take-back',
      label: 'Stop the other project and open this app instead',
    })
    expect(state.secondAction ?? null).toBeNull()
  })

  it('★ THE INVARIANT: no reading of a taken slot ever leaves both slots empty', () => {
    // The dead end the merge removed, pinned so it cannot come back through a new arm. Swept over
    // every attribution shape AND every press ending, because the endings are what would introduce
    // one: a `take-back-failed` arm that decided to withhold both controls would look reasonable
    // in review and would strand the citizen with a named problem and nothing to press.
    const attributions: [string, PreviewState][] = [
      ['named and routable', heldBy('Roster', 'p-9')],
      ['name only', reading({ state: 'slot_taken', occupyingProjectName: 'Roster' })],
      ['id only', reading({ state: 'slot_taken', occupyingProjectId: 'p-9' })],
      ['neither', reading({ state: 'slot_taken' })],
    ]

    for (const [shape, preview] of attributions) {
      for (const startOutcome of EVERY_ENDING) {
        for (const projectHasSavedBuild of [true, false, null]) {
          const state = resolve({ preview, startOutcome, projectHasSavedBuild })
          expect(state.name, shape).toBe('held-by-another-project')
          const both = (state.action ?? null) === null && (state.secondAction ?? null) === null
          expect(both, `${shape} / ${startOutcome?.kind ?? 'no ending'} left nothing to press`).toBe(false)
          // AND A SECOND SLOT IS NEVER FILLED WITHOUT A FIRST, which `AppPane` depends on: it
          // draws the second control inside the first one's block.
          if (state.secondAction) expect(state.action, shape).not.toBeNull()
        }
      }
    }
  })

  it('offers no go-to when only half the attribution arrived — a button to nowhere is worse', () => {
    // THE ATTRIBUTION IS ALL OR NOTHING and the wire says so: the name and the id go missing
    // together. Half an attribution can neither label a navigation nor route one.
    const nameOnly = resolve({
      preview: reading({ state: 'slot_taken', occupyingProjectName: 'Roster' }),
    })
    const idOnly = resolve({ preview: reading({ state: 'slot_taken', occupyingProjectId: 'p-9' }) })

    for (const state of [nameOnly, idOnly]) {
      expect(state.action?.kind).not.toBe('go-to-project')
      expect(JSON.stringify(state)).not.toContain('Roster')
    }
  })

  it('a held slot outranks a start outcome — the remedy, never a retry', () => {
    // A retry against an occupied slot can only fail the same way again.
    const state = resolve({ preview: heldBy('Roster', 'p-9'), startOutcome: { kind: 'timed-out' } })

    expect(state.action?.kind).toBe('go-to-project')
  })
})

describe('★ taking the workspace back — the second control, and its five endings', () => {
  it('★ the take-back carries no id of its own — the holder comes off the refusal', () => {
    // Deliberate, and the reason is the ending where a held id would be WRONG: another tab taking
    // the freed slot mid-sequence. The reading names the old holder; the server`s refusal names
    // the new one, and carries the `dirty` tri-state the dialog`s copy arms need besides.
    const second = resolve({ preview: heldBy() }).secondAction
    expect(second).not.toBeNull()
    expect(JSON.stringify(second)).not.toContain('proj-9')
  })

  it('★ ENDING 1 — a stop that failed returns to held and carries the server`s own sentence', () => {
    // `buildSessionApi.ts` authors the two-minute ceiling sentence, it is true only on this
    // ending, and the map does not rewrite it. Nothing was stopped, so nothing is said about the
    // holder having been.
    const ceiling =
      'The other app is still saving its work. Nothing has changed — give it a moment and try again.'
    const state = resolve({
      preview: heldBy('Roster', 'p-9'),
      startOutcome: { kind: 'take-back-failed', reason: ceiling, stoppedHolder: null },
    })

    expect(state.name).toBe('held-by-another-project')
    expect(state.detail).toBe(ceiling)
    expect(state.note ?? null).toBeNull()
    // Both ways out are still offered — the ending changed what is said, not what may be pressed.
    expect(state.action?.kind).toBe('go-to-project')
    expect(state.secondAction?.kind).toBe('take-back')
  })

  for (const [ending, reason] of [
    ['ENDING 3 — the save failed', 'Could not save your work'],
    ['ENDING 4 — the save worked and the release failed', 'Could not close the other workspace'],
  ] as const) {
    it(`★ ${ending}: held, plus the line saying the holder is down`, () => {
      // `handOverWorkspace` REJECTS RATHER THAN SWALLOWS, so a failed save is never followed by a
      // release: the holder is stopped and the slot is still held. Two facts, and the headline
      // alone tells the citizen neither of them.
      const state = resolve({
        preview: heldBy('Roster', 'p-9'),
        startOutcome: { kind: 'take-back-failed', reason, stoppedHolder: 'Roster' },
      })

      expect(state.name).toBe('held-by-another-project')
      expect(state.detail).toBe(reason)
      expect(state.note).toBe('“Roster” was stopped, and it still holds your workspace.')
    })
  }

  it('★ and never reuses ending 1`s "nothing has changed" where it would be false', () => {
    // The holder is DOWN on both of these. A sentence promising nothing moved is the one thing
    // this arm must not say.
    for (const reason of ['Could not save your work', 'Could not close the other workspace']) {
      const text = rendered({
        preview: heldBy('Roster', 'p-9'),
        startOutcome: { kind: 'take-back-failed', reason, stoppedHolder: 'Roster' },
      })
      expect(text).not.toMatch(/nothing has changed/i)
    }
  })

  it('★ ENDING 2 — the slot was freed and the start failed: SAVED, plus the holder, in `note`', () => {
    // The acceptance example "Returns to the held-by-another state" is unreachable here: the
    // release succeeded, so the holder is gone and the reading is no longer `slot_taken`.
    //
    // ★ AND IT NO LONGER GETS A CARD OF ITS OWN. `start-failed` drew "We could not start your
    // app." over a workspace whose situation, honest headline and next step were all identical to
    // SAVED's — a differently-shaped screen telling the citizen something had changed that had
    // not. What is left is the ordinary saved card, with two things said in the one field the
    // negative-copy sweep exempts: what we did to the other project, then the server's own words.
    const state = resolve({
      preview: reading({ state: 'asleep', restorable: true }),
      startOutcome: { kind: 'take-back-failed', reason: 'the image could not be pulled', stoppedHolder: 'Roster' },
    })

    expect(state.name).toBe('not-running')
    expect(state.headline).toBe('Your app is saved.')
    expect(state.detail).toBe('It stays running while you work, so you only do this once.')
    // THE ORDER INSIDE THE NOTE IS LOAD-BEARING: the map's own sentence about the other project is
    // properly terminated, and server prose has no punctuation contract at all — leading with it
    // would run the two together.
    expect(state.note).toBe('“Roster” was stopped. the image could not be pulled')
    // The remedy is the same Launch a saved workspace always offers.
    expect(state.action).toEqual({ kind: 'start', label: LAUNCH_LABEL })
  })

  it('says nothing about a holder it never stopped, even on the freed arm', () => {
    const state = resolve({
      preview: reading({ state: 'asleep', restorable: true }),
      startOutcome: { kind: 'take-back-failed', reason: 'the image could not be pulled', stoppedHolder: null },
    })
    expect(state.note).toBe('the image could not be pulled')
    expect(state.note).not.toMatch(/\bstopped\b/)
    // LIVENESS: it still reports the failure it does know about, on the card it belongs to.
    expect(state.name).toBe('not-running')
  })

  it('★ every OTHER start outcome is still outranked by a held slot', () => {
    // The precedence is unchanged for the three endings that describe an ordinary start. Only the
    // take-back ending crosses it, because it describes a press made FROM this arm.
    for (const startOutcome of [
      { kind: 'timed-out' },
      { kind: 'not-painted' },
      { kind: 'failed', reason: 'no image' },
    ] as const) {
      const state = resolve({ preview: heldBy('Roster', 'p-9'), startOutcome })
      expect(state.name).toBe('held-by-another-project')
      expect(state.detail).toBe('You have one workspace at a time. Open that project to pick up where you left off.')
      expect(state.note ?? null).toBeNull()
    }
  })

  it('★ the comparator sees BOTH optional fields, and each one on its own', () => {
    // The channel skips a publish when `sameWorkspaceState` says two readings render identically,
    // and both optional fields are things a citizen reads. ISOLATED DELIBERATELY: the obvious pair
    // — two different holders — differs in the headline and in the first action's label too, so a
    // comparator that had never heard of either field still calls them different and the test
    // passes vacuously. Each assertion below moves exactly one field.

    // THE NOTE, alone: the same failing take-back, told apart only by whether it got as far as
    // stopping the holder. Same name, same headline, same server prose.
    const failed = (stoppedHolder: string | null) =>
      resolve({
        preview: heldBy('Roster', 'p-9'),
        startOutcome: { kind: 'take-back-failed', reason: 'Could not save your work', stoppedHolder },
      })
    expect(failed(null).headline).toBe(failed('Roster').headline)
    expect(failed(null).detail).toBe(failed('Roster').detail)
    expect(sameWorkspaceState(failed(null), failed('Roster'))).toBe(false)

    // THE SECOND SLOT, alone. Hand-built, because the map ties the take-back's label to the holder
    // name that is also in the headline — and the comparator's contract is over the TYPE, not over
    // whichever combinations one arm happens to produce today.
    const held = resolve({ preview: heldBy('Roster', 'p-9') })
    expect(sameWorkspaceState(held, held)).toBe(true)
    expect(sameWorkspaceState(held, { ...held, secondAction: null })).toBe(false)
    expect(
      sameWorkspaceState(held, { ...held, secondAction: { kind: 'take-back', label: 'Stop it' } }),
    ).toBe(false)
    // An omitted optional and an explicit `null` are the same claim, and must compare equal.
    const { secondAction: _s, note: _n, ...bare } = held
    expect(sameWorkspaceState({ ...bare, secondAction: null, note: null }, bare)).toBe(true)

    // ★ AND `busy`, which is the field a wait turns on and nothing else moves. Isolated the same
    // way: hand-built, because the only arm that sets it also changes every other field.
    const wait = resolve({ preview: reading({ state: 'starting' }) })
    expect(sameWorkspaceState(wait, { ...wait, busy: false })).toBe(false)
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
      ['never-built', reading({ state: 'never_built', restorable: false })],
      ['starting', reading({ state: 'starting' })],
      ['held-by-another-project', heldBy('Roster', 'p-9')],
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
      startOutcome: { kind: 'timed-out' },
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
    expect(resolve({ preview: reading({ state: 'slot_taken' }), startInFlight: true }).name)
      .toBe('starting')
    expect(resolve({ preview: reading({ state: 'alive', alive: true }), startInFlight: true }).name)
      .toBe('running')
    // AND THE MEMORY COUNTS AS THE READING for that one exception, so a blip mid-press does not
    // pull a framed app back into the wait.
    expect(
      resolve({
        preview: reading({ state: 'unknown' }),
        lastDecidedPreview: settled({ state: 'alive', alive: true }),
        startInFlight: true,
      }).name,
    ).toBe('running')
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
    // The type is the real enforcement — the union has four members and none of them is a
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
    const memories: (DecidedPreview | null)[] = [
      null,
      settled({ state: 'alive', alive: true }),
      settled({ state: 'asleep', restorable: true }),
      settled({ state: 'slot_taken', occupyingProjectName: 'A', occupyingProjectId: 'p' }),
    ]

    for (const preview of states) {
      for (const lastDecidedPreview of memories) {
        for (const startOutcome of EVERY_ENDING) {
          for (const projectHasSavedBuild of [true, false, null]) {
            for (const startInFlight of [true, false]) {
              const state = resolveWorkspaceState({
                preview,
                lastDecidedPreview,
                projectHasSavedBuild,
                startOutcome,
                startInFlight,
              })
              const text = `${state.headline} ${state.detail ?? ''} ${state.note ?? ''} ${state.action?.label ?? ''} ${state.secondAction?.label ?? ''}`
              expect(`${state.name}: ${text}`).not.toMatch(destructive)
              // Every arm says something, and offers at most one thing to press plus at most one
              // alternative — never a third.
              expect(state.headline.length).toBeGreaterThan(0)
              expect(['start', 'retry', 'go-to-project', 'take-back', undefined]).toContain(state.action?.kind)
              expect(['take-back', undefined]).toContain(state.secondAction?.kind)
              // ONLY THE HELD ARM HAS EVER FILLED THE SECOND SLOT, and only ever beside a first.
              if (state.secondAction) {
                expect(state.name).toBe('held-by-another-project')
                expect(state.action).not.toBeNull()
              }
              // AND A TAKE-BACK IS ONLY EVER REACHABLE FROM A HELD READING, in either slot. It is
              // the one verb that acts on somebody else's app, so the arm that offers it is worth
              // pinning rather than leaving to the union's shape.
              if (state.action?.kind === 'take-back') expect(state.name).toBe('held-by-another-project')
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

  it('★ EVERY arm carries the whole key set, so the map stays total over the optional three', () => {
    // `secondAction`, `note` and `busy` are OPTIONAL in the type, so the suites that hand-build a
    // state need not restate values they have no opinion about. That optionality is exactly why
    // the map has to be pinned here instead: TypeScript will not notice an arm that forgets one.
    //
    // ★ READ FROM THE COMPARATOR, not hand-kept beside it. This used to be a second literal list,
    // so adding a field meant editing four places and only three of them were forced. The
    // comparator is now an exhaustive keyed record — omitting a field is a COMPILE error there —
    // and this reads its keys, so the two lists cannot drift apart at all.
    const KEYS = WORKSPACE_STATE_FIELDS

    const arms: Array<[string, WorkspaceState]> = [
      ['running', resolve({ preview: reading({ state: 'alive', alive: true }) })],
      ['starting', resolve({ preview: reading({ state: 'starting' }) })],
      ['never-built', resolve({ preview: reading({ state: 'never_built', restorable: false }) })],
      ['not-running', resolve({ preview: reading({ state: 'asleep', restorable: true }) })],
      ['could-not-read', resolve({ preview: null, lastDecidedPreview: null })],
      ['held-by-another-project', resolve({ preview: heldBy('Roster', 'p-9') })],
    ]

    // Liveness first: the inputs really do reach six DISTINCT arms. Without this the loop below
    // could pass while every entry resolved to the same fallback.
    expect(new Set(arms.map(([, s]) => s.name)).size).toBe(6)
    for (const [expectedName, armState] of arms) {
      expect(armState.name).toBe(expectedName)
      expect(Object.keys(armState).sort()).toEqual(KEYS)
    }
    // AND THE UNATTRIBUTED HELD READING IS THE SAME ARM, which is the merge. Asserted beside the
    // totality pin rather than as a seventh entry, so the count above stays the count of arms.
    expect(resolve({ preview: reading({ state: 'slot_taken' }) }).name).toBe('held-by-another-project')
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

describe('asDecidedReading — the one narrowing, so no caller gets the polarity wrong', () => {
  it('refuses an unreadable answer as the thing to fall back to when we cannot read', () => {
    // The circularity the memory slot exists to break: storing "we could not check" as the value
    // to render when we cannot check.
    expect(asDecidedReading(reading({ state: 'unknown' }))).toBeNull()
    expect(asDecidedReading(null)).toBeNull()
  })

  it('passes every answer the platform was willing to stand behind, settled or not', () => {
    // `starting` and `alive` are DECIDED and are the opposite of SETTLED — their successors
    // arrive with no gesture from anybody — and conflating the two words is how a poll that
    // should keep asking stops.
    for (const state of ['alive', 'starting', 'asleep', 'slot_taken', 'never_built'] as const) {
      expect(asDecidedReading(reading({ state }))?.state, state).toBe(state)
    }
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

  it('never asks about a settled or an unreadable answer, stalled or not', () => {
    for (const reading of ['asleep', 'slot_taken', 'never_built', 'unknown'] as const) {
      expect(mayHaveStopped(reading, true, windowSpent)).toBe(false)
    }
  })
})
