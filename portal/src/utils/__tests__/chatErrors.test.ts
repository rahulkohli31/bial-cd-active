/**
 * How a chat tells a write it can retry from one it cannot.
 *
 * WHAT THIS FILE USED TO GUARD, AND WHY THAT IS GONE. Its load-bearing assertions were the
 * append route's two opposite-meaning 409s and the order they were checked in — written after
 * the 409's meaning inverted underneath `describeSaveFailure` without a single test going red.
 * That route is retired on both sides now: no client calls it and the backend sends
 * `message_seq_conflict` nowhere, so there is no longer a distinction to get wrong. The copy
 * builder and its two private predicates went with it, as did `describeModeSwitchFailure` —
 * written for the composer's mode selector, which the `Removals` board retired ("a chat's kind
 * is fixed when it is created, so there is nothing to switch").
 *
 * What is left is one predicate with one live caller, and the retirement guard below.
 */
import { describe, it, expect } from 'vitest'
import * as chatErrors from '../chatErrors'
import { isConversationGone } from '../chatErrors'
import { ApiError } from '../apiError'
import { TurnStartError } from '../turnStreamApi'

describe('isConversationGone', () => {
  // TWO TRANSPORTS, ASSERTED SEPARATELY. `TurnStartError` has no `ApiError` in its prototype
  // chain — they are siblings — so one arm passing says nothing about the other. Until this
  // file's copy builders were retired, both arms were only ever reached incidentally, through
  // their 404 cases; the predicate is the whole module now, so it is asserted directly.
  //
  // Mind the constructors: `ApiError(message, status)` and `TurnStartError(status, message)` take
  // their two arguments in OPPOSITE orders, and both accept a string and a number, so swapping
  // them type-checks and quietly builds a 0-status error that no arm matches.
  it('reads a REST 404 as gone', () => {
    expect(isConversationGone(new ApiError('gone', 404))).toBe(true)
  })

  it('reads a turn-start 404 as gone — the transport the live caller actually throws', () => {
    expect(isConversationGone(new TurnStartError(404, 'gone'))).toBe(true)
  })

  it('reads a recoverable failure as NOT gone, on both transports', () => {
    // The consequence of a false positive is the harshest action this predicate can take: the
    // surface navigates away, and a 500 or a lapsed session is not a deleted project.
    for (const status of [400, 401, 403, 409, 500, 503]) {
      expect(isConversationGone(new ApiError('nope', status)), `ApiError ${status}`).toBe(false)
      expect(isConversationGone(new TurnStartError(status, 'nope')), `TurnStartError ${status}`).toBe(false)
    }
  })

  it('reads a non-API throw as NOT gone', () => {
    for (const err of [new TypeError('Failed to fetch'), new Error('boom'), null, undefined]) {
      expect(isConversationGone(err)).toBe(false)
    }
  })
})

// Inert guard against any of the three retired copy builders silently returning, paired with a
// liveness check (same shape as the sibling in `buildSystemPrompt.test.js`) so it cannot
// false-green on a broken import. `describeAppFailure` went with the JSX-era single-file build
// (U27); `describeSaveFailure` with the client append route; `describeModeSwitchFailure` with
// the composer's mode selector. The cast (not `any`) mirrors `approvalApi.test.ts`'s retirement
// idiom: a namespace import types a removed export as a compile error on plain property access,
// not as `undefined`.
describe('the failure-copy builders are retired', () => {
  it('exports none of them, while the surviving predicate still works', () => {
    for (const name of ['describeAppFailure', 'describeSaveFailure', 'describeModeSwitchFailure']) {
      expect(name in chatErrors, name).toBe(false)
      expect((chatErrors as Record<string, unknown>)[name], name).toBeUndefined()
    }
    expect(isConversationGone(new ApiError('gone', 404))).toBe(true)
  })
})
