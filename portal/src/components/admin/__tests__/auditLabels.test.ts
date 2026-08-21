/**
 * The audit trail is the record an administrator reads to answer "what happened to this app".
 * It used to render the stored token verbatim — `publish_gate`, `classification_review` — which
 * names a column, not an event. These pin the two properties that matter: the actions this
 * feature actually writes read as English, and an action nobody has written copy for still
 * never renders as jargon.
 */
import { describe, expect, it } from 'vitest'

import { auditLabel } from '../auditLabels'

/** Every action the pre-publish classification path writes, in the order it happens. */
const THE_PATH = [
  'classification_review',
  'publish_gate',
  'submit',
  'approve',
  'reject',
  'withdraw',
] as const

describe('audit actions read as English', () => {
  it('gives every step of the publish path a title and an explanation', () => {
    for (const action of THE_PATH) {
      const label = auditLabel(action)
      // Not the raw token, and not a prettified version of it either — these are the six
      // an administrator sees most, so each is written, not derived.
      expect(label.title).not.toContain('_')
      expect(label.title.toLowerCase()).not.toBe(action.replace(/_/g, ' '))
      expect(label.description, `${action} needs a sentence`).toBeTruthy()
    }
  })

  it('says who the decision belongs to on the two that are a human choice', () => {
    expect(auditLabel('approve').description).toMatch(/administrator/i)
    expect(auditLabel('reject').description).toMatch(/administrator/i)
    // And the developer is told what happens next, since approval is not publication.
    expect(auditLabel('approve').description).toMatch(/developer publishes/i)
  })

  it('distinguishes an administrator approving their OWN app', () => {
    // Recorded under a separate action on the server for a reason; if the screen collapsed
    // the two, the one case worth noticing would be the one that disappeared.
    expect(auditLabel('approve:self').title).not.toBe(auditLabel('approve').title)
  })

  it('makes an unknown action readable instead of leaking the token', () => {
    // The fallback exists so a new server-side action is never WORSE than today's raw
    // rendering. It should not look like a machine token.
    const label = auditLabel('some_new:action.happened')
    expect(label.title).toBe('Some new action happened')
    expect(label.title).not.toMatch(/[_:.]/)
    expect(label.description).toBeUndefined()
  })

  it('does not invent a label for the empty string', () => {
    // Defensive: the server never writes one, but a blank heading is worse than a visible
    // oddity, and this is the branch that would produce it.
    expect(auditLabel('').title).toBe('')
  })
})
