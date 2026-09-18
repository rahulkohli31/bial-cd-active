import { describe, it, expect } from 'vitest'
import { OUTCOME_COPY, outcomeSummary, NEUTRAL_BUILD_SUMMARY } from '../messageTypes'
import type { EndReason } from '../messageTypes'

/**
 * The turn engine's own bounded endings, in the transcript. Production, 2026-09-11: a build that
 * hit its request ceiling ended with the platform's banner saying "Your app is working — have a
 * look" under a transcript row saying "The build failed." — because `request_limit` had no arm
 * here and fell through to the status. Two sentences, one screen, opposite claims.
 */
describe('outcomeSummary — the bounded endings the turn engine names', () => {
  it('a build that hit its request ceiling is not announced as failed', () => {
    const text = outcomeSummary({ status: 'failed', reason: 'request_limit' })
    expect(text).toBe(OUTCOME_COPY.request_limit)
    expect(text).not.toMatch(/fail/i)
  })

  it('the wall clock ends with the same sentence — which bound fired is not something a citizen acts on', () => {
    expect(outcomeSummary({ status: 'failed', reason: 'wall_clock_deadline_exceeded' })).toBe(
      OUTCOME_COPY.request_limit,
    )
  })

  it('a model service that would not answer says so, and names no status, provider or token', () => {
    const text = outcomeSummary({ status: 'failed', reason: 'model_unavailable' })
    expect(text).toMatch(/could not get an answer/i)
    for (const leak of ['429', 'http', 'api', 'token', 'retry', 'foundry']) {
      expect(text.toLowerCase()).not.toContain(leak)
    }
  })
})

/**
 * ★ THE CLOSED UNION: every ending the server can store has a sentence of its own.
 *
 * "The build failed." is the sentence for an ending that recorded no reason at all, and for
 * nothing else — a NAMED ending reaching it means the copy table has a hole, which is the one
 * thing this block exists to catch.
 */
describe('every ending the server can store has a sentence of its own', () => {
  const EVERY_REASON = Object.keys(OUTCOME_COPY) as EndReason[]

  it('names every member of the union, and iterates rather than counting', () => {
    // Driven off the table itself, so a member added tomorrow is covered the day it lands. The
    // floor is a liveness guard: an empty table would satisfy every `for` below.
    expect(EVERY_REASON.length).toBeGreaterThan(15)
    for (const reason of EVERY_REASON) {
      const text = outcomeSummary({ status: 'failed', reason })
      expect(text).toBe(OUTCOME_COPY[reason])
      expect(text).not.toBe('The build failed.')
      expect(text).not.toContain(reason)
    }
  })

  it('★ a build whose last check could not answer never claims to have finished', () => {
    // The backend keeps this cause vetoing for a stated reason: a container reverted to its
    // baked image compiles, serves 200, logs no crash and shows the starter page — so six green
    // signals cannot tell it apart from a working app. The hedged live sentence is never
    // persisted, so on RELOAD this lookup is the only thing the citizen sees; rendering the
    // neutral summary here is the portal asserting a completion the backend refused to confirm,
    // which is a false success rather than the false failure this work removed.
    const text = outcomeSummary({ status: 'failed', reason: 'verdict_unanswerable' })
    expect(text).not.toBe(NEUTRAL_BUILD_SUMMARY)
    expect(text).toMatch(/couldn't confirm/i)
    // And it points somewhere: an unconfirmed change is something the person at the preview can
    // settle in a second, which is the whole reason this does not read as a failure either.
    expect(text).toMatch(/preview/i)
  })

  it('a refusal reads the same on reload as it did when it happened', () => {
    // Both refusal codes reach the citizen live on a `TurnErrorFrame`; on RELOAD the banner is
    // rebuilt from the stored reason through this lookup, so the two paths must agree.
    expect(outcomeSummary({ status: 'failed', reason: 'context_hard_limit_exceeded' })).toMatch(
      /start a new chat/i,
    )
    expect(outcomeSummary({ status: 'failed', reason: 'DOCUMENT_TOO_MANY_PAGES' })).toMatch(
      /too many pages/i,
    )
  })
})

/**
 * The generic arm, which is a deliberate member of the design rather than a leftover.
 */
describe('an ending that recorded no reason', () => {
  it('takes the status-shaped sentence, and the arm is reachable', () => {
    expect(outcomeSummary({ status: 'failed', reason: null })).toBe('The build failed.')
    expect(outcomeSummary({ status: 'stopped', reason: null })).toBe(
      'This build was stopped before it finished.',
    )
    expect(outcomeSummary({ status: 'ended', reason: null })).toBe(NEUTRAL_BUILD_SUMMARY)
  })

  it('and so does a reason from a server this bundle has never met', () => {
    // The union is closed and the wire is not: a deploy ahead of this bundle can send anything.
    // It must not be printed at a citizen, and it must not crash the transcript.
    const text = outcomeSummary({ status: 'failed', reason: 'reaped_by_the_kraken' })
    expect(text).toBe('The build failed.')
    expect(text).not.toContain('kraken')
  })
})
