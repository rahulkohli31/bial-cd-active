/**
 * The dialog's score must be the server's score: the fixture is shared with the backend gate's own
 * test, so a rule changed on one side alone turns one of the two suites red.
 */
import { describe, expect, it } from 'vitest'

import fixture from '../__fixtures__/classification-score-cases.json'
import { classificationScore, countedAnswers } from '../classificationScore'
import type { ClassKind } from '../classificationApi'

function kindOf(value: string): ClassKind {
  if (value === 'hard_block' || value === 'scored') return value
  throw new Error(`the fixture names an unknown kind: ${value}`)
}

function answersOf(value: Record<string, boolean | undefined>): Record<string, boolean> {
  return Object.fromEntries(
    Object.entries(value).filter((entry): entry is [string, boolean] => typeof entry[1] === 'boolean'),
  )
}

const cases = fixture.cases.map((entry) => ({
  ...entry,
  classes: entry.classes.map((c) => ({ key: c.key, kind: kindOf(c.kind), weight: c.weight })),
  reviewerAnswers: answersOf(entry.reviewerAnswers),
  ownerAnswers: answersOf(entry.ownerAnswers),
}))

describe('the publish score matches the shared fixture', () => {
  it('covers more than a handful of cases', () => {
    expect(cases.length).toBeGreaterThanOrEqual(10)
  })

  it.each(cases.map((entry) => [entry.name, entry] as const))(
    'scores the reviewer answers: %s',
    (_name, entry) => {
      expect(classificationScore(entry.reviewerAnswers, entry.classes)).toBe(entry.reviewerScore)
    },
  )

  it.each(cases.map((entry) => [entry.name, entry] as const))(
    'scores the answers that count: %s',
    (_name, entry) => {
      const counted = countedAnswers({
        reviewer: entry.reviewerAnswers,
        owner: entry.ownerAnswers,
        ownersCanChangeAnswers: entry.ownersCanChangeAnswers,
      })
      expect(classificationScore(counted, entry.classes)).toBe(entry.score)
    },
  )
})
