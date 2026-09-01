/**
 * ISSUE #154's FOUR DEFECTS, AS PROPERTIES RATHER THAN PATCHES (R57–R60).
 *
 * All four reproduced on `main`. They are defects in code this work replaces, so they land as
 * requirements of the new composer rather than as fixes to a file about to be deleted — which is
 * why #154's open pull request was closed rather than merged.
 *
 * ══ WHY MOST OF THEM CANNOT BE RE-INTRODUCED HERE ══
 *
 * Three of the four came from the same root: `ChatPage` EMPTIED the composer optimistically and
 * then tried to put things back. R58's blind `setText(rawText)` overwrote whatever the citizen had
 * typed since (and, because the input was fully controlled, the browser's undo stack could not
 * recover it); R59's in-flight `fileToBase64` resolved into a composer that had already been
 * cleared; R57's restore merged past the per-message cap.
 *
 * This composer clears NOTHING until the server confirms. So there is no restore path, nothing to
 * race with, and the tests below are shaped as "the failure changed nothing" rather than as "the
 * restore put the right things back". That difference is the fix.
 *
 * R57's clamp is still tested — in `hooks/__tests__/usePendingAttachments.test.ts`, because the
 * function is still exported and reachable even though this composer never calls it.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'

import Composer, { type ComposerProps, type ComposerSubmission } from '../Composer'
import { readDraft } from '../../../utils/composerDraft'

afterEach(() => {
  cleanup()
  sessionStorage.clear()
})

function draw(over: Partial<ComposerProps> = {}) {
  const props: ComposerProps = {
    conversationId: 'chat-1',
    onSubmit: vi.fn().mockResolvedValue(undefined),
    isRunning: false,
    onUrgent: vi.fn(),
    ...over,
  }
  return { props, ...render(<Composer {...props} />) }
}

const box = () => screen.getByTestId('composer-input') as HTMLTextAreaElement
const type = (text: string) => fireEvent.change(box(), { target: { value: text } })
const enter = () => fireEvent.keyDown(box(), { key: 'Enter' })

/** A send that hangs until released, so a test can act inside the in-flight window. */
function heldSend() {
  let settle: { resolve: () => void; reject: (e: Error) => void } | undefined
  // Annotated, not inferred: without the parameter type the mock's call tuple infers as `[]` and
  // every `mock.calls[0][0]` below becomes a type error rather than a submission.
  const onSubmit = vi.fn(
    (_submission: ComposerSubmission) =>
      new Promise<void>((resolve, reject) => { settle = { resolve, reject } }),
  )
  return { onSubmit, release: () => settle?.resolve(), fail: () => settle?.reject(new Error('refused')) }
}

describe('AE26 — a second message typed during a failing upload survives it', () => {
  it('keeps the newer text character for character', async () => {
    const { onSubmit, fail } = heldSend()
    draw({ onSubmit })

    type('the first message')
    enter()
    await waitFor(() => expect(onSubmit).toHaveBeenCalled())

    // The composer stays live during the send, so the citizen carries on typing.
    type('the second message, typed while the first was in flight')

    fail()

    // R58: the old code blind-`setText`'d the FIRST message back over this, and a controlled
    // input meant the browser's undo could not recover it. Nothing is put back here because
    // nothing was taken away.
    await waitFor(() =>
      expect(box().value).toBe('the second message, typed while the first was in flight'),
    )
  })
})

describe('AE27 — a file read that is still running when Send is pressed', () => {
  it('is either attached to that send, or the citizen is told — never silently absent', async () => {
    // THE DISJUNCTION IS THE ASSERTION, and "silently absent" is neither branch. The old shape
    // could drop an in-flight read into a composer that had already been cleared, so the citizen
    // believed the model could see a file it could not.
    const { onSubmit, fail } = heldSend()
    const onUrgent = vi.fn()
    draw({ onSubmit, onUrgent })

    type('look at this file')
    enter()
    await waitFor(() => expect(onSubmit).toHaveBeenCalled())
    const submission = onSubmit.mock.calls[0][0]

    fail()
    await waitFor(() => expect(onUrgent).toHaveBeenCalled())

    // Branch one: whatever was staged at press time travelled WITH the send.
    expect(Array.isArray(submission.attachments)).toBe(true)
    // Branch two: the send failed, so the citizen is told — and everything is still here to retry
    // with. What is excluded is the third outcome: a cleared composer and no word.
    expect(onUrgent.mock.calls[0][0]).toMatch(/still here/i)
    expect(box().value).toBe('look at this file')
  })
})

describe('AE28 / R60 — a failure in chat A does not touch chat B', () => {
  it('leaves the sibling’s text alone, and writes nothing under the sibling’s key', async () => {
    // Pinned by NOTHING before this. The send stamps its conversation at press time, so a
    // completion that lands after the reader has moved cannot write into the chat they are now
    // looking at.
    const { onSubmit, fail } = heldSend()
    const { rerender, props } = draw({ onSubmit })

    type('chat A’s message')
    enter()
    await waitFor(() => expect(onSubmit).toHaveBeenCalled())
    expect(onSubmit.mock.calls[0][0].conversationId).toBe('chat-1')

    // The reader moves to a sibling and types there.
    rerender(<Composer {...props} onSubmit={onSubmit} conversationId="chat-2" />)
    type('chat B’s own draft')

    fail() // chat A's send fails, after the move

    await waitFor(() => expect(box().value).toBe('chat B’s own draft'))
    expect(readDraft('chat-2')).toBe('chat B’s own draft')
    // …and chat A's text is where it was left, not merged into B.
    expect(readDraft('chat-1')).toBe('chat A’s message')
  })
})

describe('the successful path still clears — otherwise none of the above is a fix', () => {
  it('empties the box and the stored draft only once the server has confirmed', async () => {
    const { onSubmit, release } = heldSend()
    draw({ onSubmit })

    type('this one lands')
    enter()
    await waitFor(() => expect(onSubmit).toHaveBeenCalled())
    // MID-FLIGHT the text is still there — clearing here is the optimistic emptying that made
    // every failure unrecoverable.
    expect(box().value).toBe('this one lands')

    release()
    await waitFor(() => expect(box().value).toBe(''))
    expect(readDraft('chat-1')).toBe('')
  })
})
