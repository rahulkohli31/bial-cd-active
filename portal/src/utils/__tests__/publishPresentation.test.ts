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

/** Every value the union can hold. Adding one to `PublishState` and not here is a type error. */
const EVERY_STATE: readonly PublishState[] = [
  'nothing_built',
  'draft',
  'in_review',
  'changes_requested',
  'approved_ready_to_publish',
  'approved_needs_review_again',
  'starting_up',
  'live_current',
  'live_newer_work',
  'live_drift_unknown',
  'taken_offline',
  'switched_off',
  'did_not_start',
]

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

  it('★ shows no version row for a state that has no version to date', () => {
    for (const state of ['nothing_built', 'starting_up', 'switched_off'] as const) {
      expect(provenanceRows(state, view(), approval()), state).toEqual([])
    }
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
    expect(row.sha).toBeNull()
    const other = savedRow(null, 'LAST SAVED', 'ink')
    expect(other.stamp).toBeNull()
    expect(other.sha).toBeNull()
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
    for (const file of ['components/PublishStatusChip.tsx', 'components/workspace/AppStatusPanel.tsx']) {
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
