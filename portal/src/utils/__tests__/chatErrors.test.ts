/**
 * How a chat reads a failed conversation write.
 *
 * WHY THIS FILE EXISTS NOW. It didn't, and that is exactly how the 409's meaning inverted under
 * it without a single test going red: the append route grew a SECOND 409 that means the opposite
 * of the first, and this module kept confidently rendering the first one's copy. The result was
 * the worst possible sentence — "That message was already saved. Reload to see the latest
 * transcript." — about a message that was NOT saved, sending the user to do the one thing that
 * destroys the text their retry needed.
 *
 * So the load-bearing assertions here are the two 409s, and the ORDER they are checked in.
 */
import { describe, it, expect } from 'vitest'
import * as chatErrors from '../chatErrors'
import { describeSaveFailure, describeModeSwitchFailure } from '../chatErrors'
import { ApiError } from '../apiError'

/** The server's transient conflict: nothing was written, retrying is the right move. */
const seqConflict = () =>
  new ApiError('Could not store this message — please try again.', 409, 'message_seq_conflict')

/** The permanent one: this exact turn is already stored, retrying can never help. */
const duplicate = () => new ApiError('message._id is already in use.', 409)

describe('describeSaveFailure', () => {
  describe('the two 409s', () => {
    it('tells the user to resend when the write was LOST', () => {
      const copy = describeSaveFailure(seqConflict())

      expect(copy).toMatch(/send it again/i)
      // The exact lie this guards: the message did not save, so it must never say it did, and it
      // must never send the user to reload — that throws away the text the retry needs.
      expect(copy).not.toMatch(/already saved/i)
      expect(copy).not.toMatch(/reload/i)
    })

    it('tells the user to reload when the turn ALREADY landed', () => {
      const copy = describeSaveFailure(duplicate())

      expect(copy).toMatch(/already saved/i)
      expect(copy).toMatch(/reload/i)
    })

    it('separates them by the server CODE, not by the status', () => {
      // Both are 409. Only the code distinguishes them, which is why the server sends one.
      expect(describeSaveFailure(seqConflict())).not.toBe(describeSaveFailure(duplicate()))
    })

    it('reads an unknown-code 409 as permanent, not as a lost write', () => {
      // Fail-closed on copy: inventing "send it again" for a 409 we don't recognise would invite
      // a retry that can never succeed. Only the explicit transient code earns that advice.
      const copy = describeSaveFailure(new ApiError('something else', 409, 'some_other_code'))

      expect(copy).toMatch(/already saved/i)
    })
  })

  describe('the other arms still hold', () => {
    it('names a deleted project on a 404', () => {
      expect(describeSaveFailure(new ApiError('gone', 404))).toMatch(/deleted/i)
    })

    it('shows a 400 verbatim — it names a real client bug worth reading', () => {
      expect(describeSaveFailure(new ApiError('header.projectId is required', 400))).toBe(
        'header.projectId is required',
      )
    })

    it('falls back for anything else', () => {
      expect(describeSaveFailure(new Error('network'))).toMatch(/check your connection/i)
      expect(describeSaveFailure(new Error('network'), 'custom fallback')).toBe('custom fallback')
    })
  })
})

describe('describeModeSwitchFailure (N12)', () => {
  it('keeps the step copy for the ONE status it is true of — the mid-turn 409', () => {
    expect(describeModeSwitchFailure(new ApiError('conflict', 409))).toMatch(/finish the current step/i)
  })

  it('reads a 401 and a 403 as the session, not as a step', () => {
    // The trap this unit exists to dissolve: a step that does not exist cannot be finished, so
    // the old copy told the user to do the one thing that could never work.
    for (const status of [401, 403]) {
      const copy = describeModeSwitchFailure(new ApiError('nope', status))
      expect(copy).toMatch(/session expired/i)
      expect(copy).not.toMatch(/finish the current step/i)
    }
  })

  it('reads a 404 as a chat that is gone — retrying cannot help', () => {
    expect(describeModeSwitchFailure(new ApiError('gone', 404))).toMatch(/no longer exists/i)
  })

  it('falls back for a 500 and for a non-ApiError alike, never onto the step copy', () => {
    for (const err of [new ApiError('boom', 500), new TypeError('Failed to fetch'), null]) {
      const copy = describeModeSwitchFailure(err)
      expect(copy).toMatch(/could not switch modes/i)
      expect(copy).not.toMatch(/finish the current step/i)
    }
  })
})

// U27: `describeAppFailure` was the copy for a failed single-file app write — the app-registry
// save path it explained (a 409 owner conflict, a 422 create failure) died with the JSX-era
// single-file build (U5/U11), leaving it a zero-reference export. Inert guard against it
// silently returning, paired with a liveness check (same shape as the sibling in
// `buildSystemPrompt.test.js`) so the guard cannot false-green on a broken import. The cast
// (not `any`) mirrors `approvalApi.test.ts`'s retirement idiom: a namespace import types
// a removed export as a compile error on plain property access, not as `undefined`.
describe('describeAppFailure is retired (U27)', () => {
  it('no longer exports describeAppFailure, while the sibling failure-copy functions still work', () => {
    expect('describeAppFailure' in chatErrors).toBe(false)
    expect((chatErrors as Record<string, unknown>).describeAppFailure).toBeUndefined()
    expect(describeSaveFailure(new ApiError('gone', 404))).toMatch(/deleted/i)
    expect(describeModeSwitchFailure(new ApiError('conflict', 409))).toMatch(/finish the current step/i)
  })
})
