/**
 * The classification policy is declared TWICE — once in Python, once here — and nothing
 * used to fail when the two drifted.
 *
 * `backend/src/services/deploy/classification.py` calls its table "ONE POLICY UNIT" that
 * changes "together, in review, in this file"; `deployApi.ts` re-declares the same six
 * rows plus the threshold by hand, and says so ("there is no codegen across the two
 * languages"). They matched by careful editing and by nothing else, so a reweight or a
 * rewording on one side shipped a UI that contradicted the server.
 *
 * The server stays authoritative — a stale table here cannot mis-publish anything — so
 * what drift costs is the citizen's trust in the screen: the modal's running total, its
 * "Send for review" vs "Publish" label, and its score line would all say one thing while
 * the server did another.
 *
 * The fixture is written by the Python table's own test, so it cannot lag it; this test
 * pins the TypeScript mirror against the fixture. No codegen, no build-time dependency
 * between the two languages, and drift is a red test in whichever PR causes it.
 */
import { describe, expect, it } from 'vitest'

import policy from '../__fixtures__/classification-policy.json'
import { AUTO_DEPLOY_MAX_SCORE, DATA_CLASSIFICATION_QUESTIONS } from '../deployApi'

describe('the classification policy matches the backend field-for-field', () => {
  it('declares the same categories, in the same order, with the same labels and weights', () => {
    // Order matters as much as content: the questionnaire renders in table order, and a
    // reordering that kept every row would still change what the citizen reads first.
    const mirrored = DATA_CLASSIFICATION_QUESTIONS.map(([, label, weight, storedKey]) => ({
      storedKey,
      label,
      weight,
    }))

    expect(mirrored).toEqual(policy.questions)
  })

  it('uses the same auto-publish threshold', () => {
    // The number that decides whether a declaration needs a human at all (issue #115) and,
    // since #117, whether it is obliged to explain itself. Two copies of that is the one
    // most worth pinning.
    expect(AUTO_DEPLOY_MAX_SCORE).toBe(policy.autoDeployMaxScore)
  })

  it('carries a camelCase key for every stored key, with no extras on either side', () => {
    // The pairing this file exists to make checkable: every row carries both spellings, so
    // a category added to one language and not the other fails here rather than surfacing
    // as a silently-unscored answer.
    expect(DATA_CLASSIFICATION_QUESTIONS).toHaveLength(policy.questions.length)
    for (const [camelKey, , , storedKey] of DATA_CLASSIFICATION_QUESTIONS) {
      expect(camelKey).toBe(
        storedKey.replace(/_([a-z])/g, (_match, letter: string) => letter.toUpperCase()),
      )
    }
  })
})
