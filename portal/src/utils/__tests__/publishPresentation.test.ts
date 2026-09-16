/**
 * ONE DECISION, TWO SURFACES — the invariant that makes the panel and the chip agree.
 *
 * The boards draw the app's status twice: an always-visible APP STATUS panel in the project rail
 * and a chip beside the title in the toolbar row. They are different shapes with different
 * lifetimes, so they are not one component — which means "they can never say different things"
 * has to be something other than a shared render.
 *
 * It is this module: every word, every colour, every action and every row is decided here, from
 * the ONE server-computed `publishState`, and both surfaces read the answer. So the tests that
 * matter are about totality (a state the server adds cannot reach a surface unlabelled) and about
 * authorship (neither surface may spell a second copy of any of it).
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import path from 'node:path'
import {
  ACTION_LABEL,
  lookFor,
  presentationFor,
  provenanceRows,
  savedRow,
} from '../publishPresentation'
import type { ApprovalState, DeploymentView, PublishState } from '../deployApi'

/**
 * Every value the union can hold — as a RECORD KEYED BY THE UNION, which is what makes the claim
 * true. It was an array annotated `readonly PublishState[]`, and TypeScript satisfies that with
 * ANY subset: a state added to `PublishState` and not written here compiled cleanly and simply
 * dropped out of the totality loop, the drift loop and the empty-rows loop below, with every test
 * still green. A record literal missing a key is an error on the literal itself.
 *
 * `Object.keys` is the one place the key type is lost, and the assertion below restates exactly
 * what this record's own annotation already guarantees.
 */
const ALL_STATES: Record<PublishState, null> = {
  nothing_built: null,
  draft: null,
  in_review: null,
  changes_requested: null,
  approved_ready_to_publish: null,
  approved_needs_review_again: null,
  starting_up: null,
  live_current: null,
  live_newer_work: null,
  live_drift_unknown: null,
  taken_offline: null,
  switched_off: null,
  did_not_start: null,
}
const EVERY_STATE = Object.keys(ALL_STATES) as readonly PublishState[]

const view = (over: Partial<DeploymentView> = {}): DeploymentView =>
  ({ publishState: 'draft', savedHead: null, savedAt: null, ...over }) as DeploymentView
const approval = (over: Partial<ApprovalState> = {}): ApprovalState => ({ status: 'draft', ...over }) as ApprovalState

describe('the decision is total over every state the server can send', () => {
  it('gives each state words, a colour and rows without throwing', () => {
    for (const state of EVERY_STATE) {
      const presentation = presentationFor(state)
      expect(presentation.label.length, state).toBeGreaterThan(0)
      expect(presentation.sentence.length, state).toBeGreaterThan(0)
      if (presentation.action !== null) expect(ACTION_LABEL[presentation.action], state).toBeTruthy()

      const look = lookFor(state)
      expect(look.pill, state).toMatch(/^text-status-[a-z]+-fg bg-status-[a-z]+-bg$/)
      expect(look.dot, state).toMatch(/^bg-status-[a-z]+-dot$/)

      expect(() => provenanceRows(state, view(), approval()), state).not.toThrow()
    }
  })

  it('paints the pill and its dot from the SAME family, never a mismatched pair', () => {
    for (const state of EVERY_STATE) {
      const { pill, dot } = lookFor(state)
      const family = /bg-status-([a-z]+)-bg/.exec(pill)?.[1]
      expect(dot, state).toBe(`bg-status-${family}-dot`)
    }
  })

  it('★ only the state that KNOWS work has drifted prints the amber date', () => {
    // #B45309 is the only amber TEXT on the canvas and the colour is the whole signal.
    // `live_drift_unknown` is the trap: the server could not make the comparison, so "yours is
    // newer" would be as much of a claim there as "nothing of yours is waiting".
    const drifted = EVERY_STATE.filter((state) =>
      provenanceRows(state, view(), approval()).some((row) => row.tone === 'drift'),
    )
    expect(drifted).toEqual(['live_newer_work'])
  })

  it('★ claims no approval for a live app that no administrator ever approved', () => {
    // Ladder rule 7 publishes unattended, and `approved_commit_sha` is NULL for every one of
    // those — the common case, not the exotic one. The row was printed regardless, so the panel
    // headed it APPROVED and filled it with "We could not tell", which reads as an approval that
    // happened and was then lost. An approval that never happened gets no row.
    for (const state of ['live_current', 'live_newer_work', 'live_drift_unknown'] as const) {
      const keys = provenanceRows(state, view({ publishState: state }), approval()).map((row) => row.key)
      expect(keys, state).toEqual(['published', 'saved'])
    }

    // Liveness, and the other half of the rule: a version an administrator DID approve still
    // says so, and either half of the stamp is enough to know that it happened.
    const withBoth = approval({ approvedAt: '2026-08-19T00:00:00Z', approvedCommitSha: 'abc' })
    expect(provenanceRows('live_current', view(), withBoth).map((row) => row.key)).toEqual([
      'published',
      'approved',
      'saved',
    ])
    const dateOnly = approval({ approvedAt: '2026-08-19T00:00:00Z', approvedCommitSha: null })
    expect(provenanceRows('live_current', view(), dateOnly).map((row) => row.key)).toContain('approved')
  })

  it('★ still says "we could not tell" where the state itself asserts an approval', () => {
    // The counterweight to the rule above. `approved_ready_to_publish` MEANS an administrator
    // approved this version, so a missing stamp there is a genuine gap in the record and the row
    // has to stay and say so — dropping it would hide the one case the "cannot tell" rendering
    // exists for.
    for (const state of ['approved_ready_to_publish', 'approved_needs_review_again'] as const) {
      const row = provenanceRows(state, view(), approval()).find((r) => r.key === 'approved')
      expect(row, state).toBeTruthy()
      expect(row?.stamp ?? null, state).toBeNull()
    }
  })

  it('★ shows no version row for a state that has no version to date', () => {
    for (const state of ['nothing_built', 'starting_up', 'switched_off'] as const) {
      expect(provenanceRows(state, view(), approval()), state).toEqual([])
    }
  })
})

describe('a switched-off app is told what it cannot do', () => {
  it('★ names the consequence the owner is actually hitting, and never mentions publishing', () => {
    // THE KILL SWITCH NOW REACHES DRAFTS. When it reached approved apps only, "nothing
    // can be published" was the whole of what it meant. To the owner of an app that has never
    // been published — the ordinary case, since one-click deploy never writes a status — that
    // sentence named a consequence they were not pursuing and left the one they are hitting
    // unsaid: their workspace will not start and no message they send will run.
    const { sentence } = presentationFor('switched_off')

    expect(sentence).toContain('An administrator switched this app off')
    expect(sentence).toContain('cannot make changes')
    expect(sentence.toLowerCase()).not.toContain('publish')
  })

  it('★ offers nothing to do, because the owner has nothing they can do', () => {
    // Only an administrator can undo this. An action here would send the citizen round a loop
    // that cannot end — which is why `taken_offline`, whose remedy IS the owner's to take,
    // keeps its action while this one does not.
    expect(presentationFor('switched_off').action).toBeNull()
    expect(presentationFor('taken_offline').action).not.toBeNull()
  })
})

describe('the saved row keeps its two halves independent', () => {
  it('carries a date with no id, an id with no date, and neither', () => {
    expect(savedRow(view({ savedAt: '2026-08-25T14:20:00Z', savedHead: null }), 'LAST SAVED', 'ink')).toMatchObject({
      stamp: '2026-08-25T14:20:00Z',
      sha: null,
    })
    expect(savedRow(view({ savedAt: null, savedHead: 'abc' }), 'LAST SAVED', 'ink')).toMatchObject({
      stamp: null,
      sha: 'abc',
    })
    expect(savedRow(view(), 'LAST SAVED', 'ink')).toMatchObject({ stamp: null, sha: null })
  })

  it('★ never fills one half in from the other, and never from nothing', () => {
    // The failure this forbids: a row that reports a version because it has a date, or a date
    // because it has a version. `null` is "no claim" on each axis, independently.
    const row = savedRow(view({ savedAt: '2026-08-25T14:20:00Z', savedHead: null }), 'LAST SAVED', 'ink')
    expect(row?.sha ?? null).toBeNull()
    expect(row?.stamp ?? null).toBe('2026-08-25T14:20:00Z') // liveness: it IS a row, not an omission
    const other = savedRow(null, 'LAST SAVED', 'ink')
    expect(other).not.toBeNull()
    expect(other?.stamp ?? null).toBeNull()
    expect(other?.sha ?? null).toBeNull()
  })

  it('labels the row for what it is being compared against', () => {
    // "YOUR LATEST" where something is LIVE, because the row exists to be contrasted with what
    // is serving; "LAST SAVED" where nothing is, because there is nothing to contrast it with.
    const labelFor = (state: PublishState) =>
      provenanceRows(state, view(), approval()).find((row) => row.key === 'saved')?.label
    expect(labelFor('live_current')).toBe('YOUR LATEST')
    expect(labelFor('live_newer_work')).toBe('YOUR LATEST')
    expect(labelFor('draft')).toBe('LAST SAVED')
    expect(labelFor('changes_requested')).toBe('LAST SAVED')
  })
})

/**
 * The three reasons the saved pair can be absent, and the ONE of them that removes the row.
 *
 * `savedHead`/`savedAt` both read null when the citizen has never saved, when no object store
 * is bound, and when the store would not answer. The panel spoke all three as "LAST SAVED — We
 * could not tell", which on the first is false in the frightening direction: it tells somebody
 * who has never saved that the platform lost their work, on the panel they open precisely when
 * they are unsure it is safe.
 */
describe('a project that has never been saved gets no saved row at all', () => {
  it('★ omits the row for `never_saved`, and keeps it for every other absence', () => {
    // Mutation receipt: delete the `never_saved` guard in `savedRow` and the first assertion
    // goes red while the three below stay green — they are what stops the guard being widened
    // into "no row whenever the pair is null", which would delete the honest gap as well.
    expect(savedRow(view({ savedState: 'never_saved' }), 'LAST SAVED', 'ink')).toBeNull()
    for (const state of ['store_unconfigured', 'storage_error', 'saved'] as const) {
      expect(savedRow(view({ savedState: state }), 'LAST SAVED', 'ink'), state).not.toBeNull()
    }
    // A server that said nothing is not a claim that nothing was saved.
    expect(savedRow(view({ savedState: null }), 'LAST SAVED', 'ink')).not.toBeNull()
  })

  it('★ drops it from every state that draws one, and keeps that state\'s other rows', () => {
    // The liveness half matters more than the absence half: a `provenanceRows` that threw, or
    // returned nothing at all, would satisfy "no saved row" perfectly.
    const never = view({ savedState: 'never_saved' })
    const withApproval = approval({ approvedAt: '2026-08-19T00:00:00Z', approvedCommitSha: 'abc' })
    for (const state of ['draft', 'did_not_start', 'in_review', 'live_current', 'taken_offline'] as const) {
      const keys = provenanceRows(state, never, withApproval).map((row) => row.key)
      expect(keys, state).not.toContain('saved')
    }
    expect(provenanceRows('live_current', never, withApproval).map((row) => row.key)).toEqual([
      'published',
      'approved',
    ])
    expect(provenanceRows('in_review', never, withApproval).map((row) => row.key)).toEqual(['submitted'])
  })

  it('★ a save the platform could not READ still says "we could not tell"', () => {
    // THE ROW STAYS, with that wording. A storage blip is a gap in the record about work that
    // exists; blanking the row there would hide the one case that rendering was written for.
    for (const state of ['store_unconfigured', 'storage_error'] as const) {
      const row = provenanceRows('draft', view({ savedState: state }), approval()).find(
        (r) => r.key === 'saved',
      )
      expect(row, state).toBeTruthy()
      expect(row?.stamp ?? null, state).toBeNull()
      expect(row?.sha ?? null, state).toBeNull()
    }
  })

  it('a project that HAS saved still reports its date and its build id', () => {
    const row = provenanceRows(
      'draft',
      view({ savedState: 'saved', savedAt: '2026-08-25T14:20:00Z', savedHead: 'abc' }),
      approval(),
    ).find((r) => r.key === 'saved')
    expect(row?.stamp).toBe('2026-08-25T14:20:00Z')
    expect(row?.sha).toBe('abc')
  })
})

/**
 * The reviewer's reason, as a row, above the state's action.
 */
describe('the rejection note reaches the rail', () => {
  const NOTE = 'Move the hardcoded database URL out of lib/db.ts, then send it again.'

  it('★ rides on the changes-requested state, FIRST, so the action below stays reachable', () => {
    const rows = provenanceRows(
      'changes_requested',
      view(),
      approval({ status: 'rejected', rejectionNote: NOTE }),
    )
    expect(rows.map((row) => row.key)).toEqual(['rejection', 'saved'])
    expect(rows[0].note).toBe(NOTE)
    // The whole note, never a prefix — truncating it here would put the clipping in the
    // accessible tree, which is the defect the CSS-bounded block exists to avoid.
    expect(rows[0].note?.length).toBe(NOTE.length)
  })

  it('★ writes no note row when the administrator wrote no note', () => {
    // Same rule as the absent APPROVED row: a row headed WHY reading "we could not tell"
    // would invent a note nobody wrote.
    for (const note of [null, '   ']) {
      const rows = provenanceRows('changes_requested', view(), approval({ status: 'rejected', rejectionNote: note }))
      expect(rows.map((row) => row.key), String(note)).toEqual(['saved']) // liveness: the state still draws its rows
    }
  })

  it('★ puts it on THAT state and no other', () => {
    // A note present on the approval block must not leak onto a state whose words are about
    // something else — an app resubmitted after a rejection is `in_review`, and its rail must
    // not still be showing the old complaint as though it were current.
    const rejected = approval({ status: 'rejected', rejectionNote: NOTE })
    for (const state of EVERY_STATE.filter((s) => s !== 'changes_requested')) {
      const keys = provenanceRows(state, view(), rejected).map((row) => row.key)
      expect(keys, state).not.toContain('rejection')
    }
    expect(provenanceRows('changes_requested', view(), rejected).map((r) => r.key)).toContain('rejection')
  })
})

describe('neither surface holds a second copy of the decision', () => {
  /**
   * A SOURCE SCAN, because this is the invariant a render test cannot reach. Both surfaces
   * agreeing today proves nothing about the next person adding a state: what has to hold is that
   * there is only ever ONE place to add it. A label spelled inside either component is a second
   * author, and the two would drift the first time only one of them was edited.
   */
  const read = (rel: string) => readFileSync(path.resolve(__dirname, '..', '..', rel), 'utf8')
  const stripComments = (text: string) =>
    text.replace(/\/\*[\s\S]*?\*\//g, ' ').replace(/(^|[^:])\/\/[^\n]*/g, '$1')

  const LABELS = EVERY_STATE.map((state) => presentationFor(state).label)

  it('the chip and the panel spell no state label of their own', () => {
    for (const file of ['components/PublishStatusChip.tsx', 'components/projects/AppStatusPanel.tsx']) {
      const source = stripComments(read(file))
      for (const label of LABELS) {
        expect(source.includes(`'${label}'`), `${file} spells "${label}"`).toBe(false)
        expect(source.includes(`"${label}"`), `${file} spells "${label}"`).toBe(false)
      }
    }
  })

  it('…and the rule actually fires on text that violates it', () => {
    // Without this, a scan whose needle never appears passes for ever and protects nothing.
    const bad = stripComments(`const label = 'Nothing built yet'`)
    expect(LABELS.some((label) => bad.includes(`'${label}'`))).toBe(true)
  })
})

/**
 * ★ A FAILED RESTART IS NOT A FAILED FIRST DEPLOY, and the correction lives HERE rather than on a
 * surface — which is the whole point. The chip and the panel both read this module, so a rename
 * applied at one of them would have produced two descriptions of one failure.
 *
 * The server reports `did_not_start` for both events. They are not the same thing to the person
 * reading them: a first deploy has never had a working version, a restart had one a minute ago.
 */
describe('the failure code corrects exactly one state', () => {
  it.each(['restart_failed', 'restart_not_ready'])('renames did_not_start under %s', (code) => {
    // Mutation receipt: drop the `failureCode` arm and this goes red on the label — the state
    // word is "Didn't start", which is true of a first deploy and false here.
    const { label, action } = presentationFor('did_not_start', code)
    expect(label).toBe('Could not restart')
    // AND IT OFFERS NOTHING TO PRESS. A restart runs the SAME version, so a restart that keeps
    // failing is an application whose own code is the fault; "Try again" would be a control that
    // is guaranteed to do nothing.
    expect(action).toBeNull()
  })

  it('points at the remedy that can actually work', () => {
    expect(presentationFor('did_not_start', 'restart_failed').sentence).toMatch(/send it for review/i)
  })

  it('★ leaves an ordinary failed first deploy saying exactly what it always said', () => {
    // The paired negative: the rename must not swallow the state it is distinguishing itself from.
    const plain = presentationFor('did_not_start')
    expect(plain.label).toBe("Didn't start")
    expect(plain.action).toBe('try_again')
    expect(presentationFor('did_not_start', 'build_failed')).toEqual(plain)
  })

  it('★ never shouts over a state that outranks the deployment row', () => {
    // An administrator's lockout and a pending submission are the more current fact server-side,
    // and `failureCode` outlives the attempt that wrote it. A correction keyed on the code alone
    // would answer "Could not restart" to an owner whose application is with a reviewer.
    for (const state of ['in_review', 'switched_off', 'live_current', 'taken_offline'] as const) {
      expect(presentationFor(state, 'restart_failed')).toEqual(presentationFor(state))
    }
  })

  it('every other state is untouched by every code', () => {
    // The blunt version of the rule above, over the whole table rather than four samples.
    for (const state of EVERY_STATE) {
      if (state === 'did_not_start') continue
      for (const code of ['restart_failed', 'restart_not_ready', 'build_failed', null]) {
        expect(presentationFor(state, code), state).toEqual(presentationFor(state))
      }
    }
  })
})
