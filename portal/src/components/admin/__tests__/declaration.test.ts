/**
 * The stored declaration is read by one of two readers, chosen by its `version` field. Both read
 * stored data rather than a wire schema, so a malformed field must narrow to nothing, never throw.
 */
import { describe, it, expect } from 'vitest'
import { agentFinding, readDeclaration } from '../declaration'
import type { ClassDeclaration } from '../declaration'

const SHA = '3f2a9c1' + '9'.repeat(33)

const CLASSES = [
  { key: 'credentials_keys', title: 'Credentials & keys', kind: 'scored', weight: 20 },
  { key: 'pii', title: 'PII', kind: 'hard_block', weight: null },
  { key: 'integrations', title: 'Integrations', kind: 'scored', weight: 20 },
  { key: 'financial_data', title: 'Financial data', kind: 'hard_block', weight: null },
]

const ALL_NO = Object.fromEntries(CLASSES.map((entry) => [entry.key, false]))

const stored = (overrides: Record<string, unknown> = {}): Record<string, unknown> => ({
  version: 2,
  commit: SHA,
  savedAt: '2026-09-26T09:05:00+00:00',
  decidedAt: '2026-09-26T09:30:00+00:00',
  policy: { threshold: 50, ownersCanChangeAnswers: true },
  classes: CLASSES,
  review: { current: true, status: 'complete', failureCode: null, checkedAt: '2026-09-26T09:06:00+00:00' },
  reviewerAnswers: ALL_NO,
  reviewerReasons: {},
  ownerAnswers: {},
  reviewerScore: 0,
  score: 0,
  outcome: 'published',
  reason: null,
  note: null,
  ...overrides,
})

function readClasses(overrides: Record<string, unknown> = {}): ClassDeclaration {
  const read = readDeclaration(stored(overrides))
  if (read.version !== 2) throw new Error('expected the class reader')
  return read
}

const byKey = (read: ClassDeclaration, key: string) => {
  const found = read.classes.find((entry) => entry.key === key)
  if (found === undefined) throw new Error(`no class ${key}`)
  return found
}

describe('a class declaration', () => {
  it('reads when the version was saved and when the agent checked it', () => {
    expect(readClasses()).toMatchObject({
      savedAt: '2026-09-26T09:05:00+00:00',
      checkedAt: '2026-09-26T09:06:00+00:00',
    })
  })

  it('reads a declaration stored without either time as having neither', () => {
    const read = readClasses({ savedAt: undefined, review: { current: true, status: 'complete', failureCode: null } })

    expect(read).toMatchObject({ savedAt: null, checkedAt: null })
  })

  it('reads the policy, the scores, the reason and the note it was decided with', () => {
    const read = readClasses({
      reviewerScore: 60,
      score: 40,
      outcome: 'routed',
      reason: 'over_threshold',
      note: 'The rates are public on the vendor portal.',
    })

    expect(read).toMatchObject({
      commit: SHA,
      decidedAt: '2026-09-26T09:30:00+00:00',
      threshold: 50,
      ownersCanChangeAnswers: true,
      reviewerScore: 60,
      score: 40,
      reason: 'over_threshold',
      note: 'The rates are public on the vendor portal.',
    })
  })

  it('lists hard blocks first, then scored classes, each in the order it was stored', () => {
    expect(readClasses().classes.map((entry) => entry.key)).toEqual([
      'pii',
      'financial_data',
      'credentials_keys',
      'integrations',
    ])
  })

  it('takes each title from its own snapshot, so a class renamed later reads as it was judged', () => {
    const read = readClasses({
      classes: [{ key: 'pii', title: 'Personal data (as judged)', kind: 'hard_block', weight: null }],
    })

    expect(read.classes.map((entry) => entry.title)).toEqual(['Personal data (as judged)'])
  })

  it('names the hard blocks the agent answered Yes, with its reasons', () => {
    const read = readClasses({
      reviewerAnswers: { ...ALL_NO, pii: true },
      reviewerReasons: { pii: 'Stores a photo of each visitor’s ID.', financial_data: 'No payments.' },
      reason: 'hard_block',
    })

    expect(read.found.map((entry) => [entry.title, entry.reason])).toEqual([['PII', 'Stores a photo of each visitor’s ID.']])
    expect(agentFinding(read)).toBe('PII found (hard block)')
  })

  it('gives a hard block no owner answer, whatever the owners could change', () => {
    const read = readClasses({ reviewerAnswers: { ...ALL_NO, pii: true }, ownerAnswers: { pii: false } })

    expect(byKey(read, 'pii')).toMatchObject({ agent: true, owner: null, changed: false })
  })

  it('marks a scored answer the owner changed, and reads an unsent one as the agent’s', () => {
    const read = readClasses({
      reviewerAnswers: { ...ALL_NO, integrations: true, credentials_keys: true },
      ownerAnswers: { integrations: false },
    })

    expect(byKey(read, 'integrations')).toMatchObject({ agent: true, owner: false, changed: true })
    expect(byKey(read, 'credentials_keys')).toMatchObject({ agent: true, owner: true, changed: false })
  })

  it('gives every class no owner answer when owners could not change answers', () => {
    const read = readClasses({
      policy: { threshold: 50, ownersCanChangeAnswers: false },
      reviewerAnswers: { ...ALL_NO, integrations: true },
      ownerAnswers: null,
    })

    expect(read.classes.map((entry) => entry.owner)).toEqual([null, null, null, null])
    expect(byKey(read, 'integrations').agent).toBe(true)
  })

  it('reads an unfinished review as no answers and no score', () => {
    const read = readClasses({
      review: { current: false, status: 'failed', failureCode: 'timeout' },
      reviewerAnswers: null,
      ownerAnswers: null,
      reviewerScore: null,
      score: null,
      outcome: 'routed',
      reason: 'review_unfinished',
    })

    expect(read.classes.map((entry) => [entry.agent, entry.owner])).toEqual([
      [null, null],
      [null, null],
      [null, null],
      [null, null],
    ])
    expect(read.reviewerScore).toBeNull()
    expect(agentFinding(read)).toBe('Review did not finish')
  })

  it('describes a send with no hard block by the agent’s own score', () => {
    const read = readClasses({ reviewerAnswers: { ...ALL_NO, integrations: true }, reviewerScore: 20, score: 0 })

    expect(agentFinding(read)).toBe('No hard block · score 20/100')
  })

  it('narrows malformed fields to nothing instead of throwing', () => {
    const read = readClasses({
      policy: 'strict',
      savedAt: 'yesterday',
      decidedAt: 42,
      review: { current: true, checkedAt: 'soon' },
      classes: [
        null,
        { key: 'ghost', kind: 'scored', weight: 20 },
        { key: 'odd', title: 'Odd', kind: 'mandatory', weight: 20 },
        { key: 'integrations', title: 'Integrations', kind: 'scored', weight: '20' },
      ],
      reviewerAnswers: { integrations: 'yes' },
      reviewerReasons: ['not', 'a', 'record'],
      ownerAnswers: 'none',
      reviewerScore: '60',
      score: Number.NaN,
      reason: 'dispute',
      note: '   ',
    })

    expect(read.classes).toEqual([
      {
        key: 'integrations',
        title: 'Integrations',
        kind: 'scored',
        weight: null,
        agent: null,
        reason: null,
        owner: null,
        changed: false,
      },
    ])
    expect(read).toMatchObject({
      savedAt: null,
      checkedAt: null,
      decidedAt: null,
      threshold: null,
      ownersCanChangeAnswers: null,
      reviewerScore: null,
      score: null,
      reason: null,
      note: null,
    })
  })

  it('reads a snapshot that is not a list as no classes', () => {
    expect(readClasses({ classes: { pii: 'PII' } }).classes).toEqual([])
  })
})

describe('a declaration without a version', () => {
  it('is read as the six-question shape, as it was sent', () => {
    const read = readDeclaration({
      commits: { shipping: SHA, reviewed: SHA },
      citizen: { answers: { personal_information: true, public_data: false }, explanation: 'Staff names only.' },
    })

    expect(read.version).toBe(1)
    if (read.version !== 1) return
    expect(read.present).toBe(true)
    expect(read.citizenAnswers).toEqual([
      { key: 'personal_information', label: 'Personal Information (PII)', yes: true },
      { key: 'public_data', label: 'Public Data', yes: false },
    ])
    expect(read.explanation).toBe('Staff names only.')
  })

  it('reads no declaration at all as absent', () => {
    const read = readDeclaration(null)

    expect(read).toMatchObject({ version: 1, present: false })
  })
})
