/**
 * THE SHARED COMPOSER BOX (plan 002, U5) — one control, on both screens.
 *
 * ═══ WHAT THIS SUITE IS FOR ═══
 *
 * The owner settled Decision 1 on 2026-09-02: adopt the library's composer. What that costs is
 * three properties this codebase held by hand, each of which had to be re-established inside the
 * library's flow rather than assumed to survive it. Each is a scenario here, and each is written
 * against a specific way the library gets it wrong on its own:
 *
 *   1. THE BOX CLEARS ONLY ONCE THE SERVER HAS ACCEPTED. `composer.send()` sets `_text = ""`
 *      before it awaits anything and restores it only if the ATTACHMENT tasks throw — never if the
 *      append does. That is the defect that destroyed a citizen's typed message and their staged
 *      files one day before this plan was written, in the library's own code.
 *   2. NO INTERACTIVE CONTROL RENDERS A REAL `disabled`. `ComposerPrimitive.Send` is built by
 *      `createActionButton`, which renders `<button disabled={…}>`, and `useComposerSend` returns
 *      no callback for the whole of every turn. `disabled` on the focused element blurs it to
 *      `document.body`.
 *   3. THE ATTACHMENT PIPELINE STAYS OURS. The library renders a chip; it does not decide which
 *      content is re-sent, which binaries are inlined, or what a refusal says.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, cleanup, fireEvent, waitFor } from '@testing-library/react'
import { ComposerHarness } from './_composerHarness'
import ComposerBox from '../ComposerBox'
import type { ComposerSubmission } from '../ComposerBox'

afterEach(cleanup)

/** A promise a test holds open, so "still there while the request is out" is observable. */
function deferred(): { promise: Promise<void>; resolve: () => void } {
  let resolve: () => void = () => {}
  const promise = new Promise<void>((r) => {
    resolve = r
  })
  return { promise, resolve }
}

interface DrawOptions {
  onSubmit?: (submission: ComposerSubmission) => Promise<void>
  unavailableReason?: string | null
  onUrgent?: (message: string) => void
}

function draw({ onSubmit, unavailableReason = null, onUrgent = vi.fn() }: DrawOptions = {}) {
  const submit = onSubmit ?? vi.fn().mockResolvedValue(undefined)
  const view = render(
    <ComposerHarness>
      <ComposerBox
        conversationId="chat-1"
        placeholder="Describe the change you need…"
        onSubmit={submit}
        unavailableReason={unavailableReason}
        onUrgent={onUrgent}
      />
    </ComposerHarness>,
  )
  return { ...view, submit, onUrgent }
}

const box = () => screen.getByTestId('composer-input') as HTMLTextAreaElement
const send = () => screen.getByTestId('composer-send')
const type = (value: string) => fireEvent.change(box(), { target: { value } })
const drop = (file: File) =>
  fireEvent.drop(screen.getByTestId('composer-dropzone'), {
    dataTransfer: { types: ['Files'], files: [file] },
  })

describe('the board\'s shape: one box, both controls inside it', () => {
  it('puts the attachment control and the send control INSIDE the box, not beside it', () => {
    draw()
    const form = screen.getByTestId('composer')
    expect(form.contains(screen.getByTestId('composer-attach'))).toBe(true)
    expect(form.contains(send())).toBe(true)
    expect(form.contains(box())).toBe(true)
  })

  it('shows the placeholder it is handed', () => {
    draw()
    expect(box().getAttribute('placeholder')).toBe('Describe the change you need…')
  })
})

describe('★ the box clears ONLY once the server has accepted', () => {
  it('keeps the text and the staged file when the send is refused', async () => {
    // THE DEFECT THIS IS WRITTEN AGAINST, in the library's own code: `composer.send()` empties
    // the text before it awaits, and restores it only when the ATTACHMENT tasks throw. So the
    // send path here is ours, and this is the assertion that keeps it that way.
    const submit = vi.fn().mockRejectedValue(new Error('the server said no'))
    draw({ onSubmit: submit })
    type('do not lose me')
    drop(new File(['id,name'], 'payroll.csv', { type: 'text/csv' }))
    await waitFor(() => expect(screen.getByTestId('composer-chips').textContent).toContain('payroll.csv'))

    fireEvent.click(send())

    await waitFor(() => expect(submit).toHaveBeenCalledTimes(1))
    expect(box().value).toBe('do not lose me')
    expect(screen.getByTestId('composer-chips').textContent).toContain('payroll.csv')
  })

  it('clears both once it resolves, and not before', async () => {
    const gate = deferred()
    const submit = vi.fn(() => gate.promise)
    draw({ onSubmit: submit })
    type('send me')
    fireEvent.click(send())

    await waitFor(() => expect(submit).toHaveBeenCalledTimes(1))
    // STILL THERE while the request is out — the half a "clears on success" test alone misses.
    expect(box().value).toBe('send me')

    gate.resolve()
    await waitFor(() => expect(box().value).toBe(''))
  })

  it('says the refusal in the words it was given, and only when they were meant to be read', async () => {
    const refusal = Object.assign(new Error('You already have a build running in this chat.'), {
      name: 'SendRefusal',
    })
    const onUrgent = vi.fn()
    draw({ onSubmit: vi.fn().mockRejectedValue(refusal), onUrgent })
    type('again')
    fireEvent.click(send())
    await waitFor(() => expect(onUrgent).toHaveBeenCalledWith('You already have a build running in this chat.'))

    cleanup()
    // A bug's message is not for this audience.
    const other = vi.fn()
    draw({ onSubmit: vi.fn().mockRejectedValue(new TypeError('x.y is not a function')), onUrgent: other })
    type('again')
    fireEvent.click(send())
    await waitFor(() => expect(other).toHaveBeenCalledTimes(1))
    expect(other.mock.calls[0]?.[0]).toMatch(/still here/i)
  })

  it('a silent refusal says nothing and clears nothing', async () => {
    const refusal = Object.assign(new Error('swallowed'), { name: 'SendRefusal', silent: true })
    const onUrgent = vi.fn()
    draw({ onSubmit: vi.fn().mockRejectedValue(refusal), onUrgent })
    type('held')
    fireEvent.click(send())
    await waitFor(() => expect(box().value).toBe('held'))
    expect(onUrgent).not.toHaveBeenCalled()
  })

  it('a double press sends once', async () => {
    const gate = deferred()
    const submit = vi.fn(() => gate.promise)
    draw({ onSubmit: submit })
    type('once')
    fireEvent.click(send())
    fireEvent.click(send())
    await waitFor(() => expect(submit).toHaveBeenCalledTimes(1))
    gate.resolve()
  })
})

describe('★ nothing in the box renders a real `disabled`', () => {
  it('not while a reply is streaming, not with nothing to send, not while sending', async () => {
    // THE LIBRARY GETS BOTH OF THESE WRONG ON ITS OWN — its Send for the whole of every turn, and
    // its Input whenever the thread reports itself disabled. Asserted over the WHOLE subtree, so
    // a control added later is covered without anyone remembering to add a case.
    const states: (string | null)[] = [null, 'Replying — keep typing if you like; send unlocks when it is done.']
    for (const reason of states) {
      const { container } = draw({ unavailableReason: reason })
      expect(container.querySelectorAll('[disabled]'), String(reason)).toHaveLength(0)
      // …and the send control still says WHY, in its accessible name.
      if (reason) expect(send().getAttribute('aria-label')).toContain(reason)
      expect(send().getAttribute('aria-disabled')).toBe('true')
      cleanup()
    }
  })

  it('keeps accepting typing while the agent answers, which is what the board draws', () => {
    draw({ unavailableReason: 'Replying — keep typing if you like; send unlocks when it is done.' })
    type('typed mid-reply')
    expect(box().value).toBe('typed mid-reply')
    expect(box().hasAttribute('disabled')).toBe(false)
  })

  it('refuses the press rather than the control — the enforcement is in the handler', async () => {
    const submit = vi.fn().mockResolvedValue(undefined)
    draw({ onSubmit: submit, unavailableReason: 'A build is running.' })
    type('anything')
    fireEvent.click(send())
    await waitFor(() => expect(submit).not.toHaveBeenCalled())
  })

  it('declares no maxLength — the attribute that would do the cutting', () => {
    draw()
    expect(box().hasAttribute('maxlength')).toBe(false)
  })
})

describe('★ the attachment pipeline stays ours', () => {
  it('stages a dropped file through our own decode, and names it on a chip', async () => {
    draw()
    drop(new File(['id,name\n1,Priya'], 'payroll.csv', { type: 'text/csv' }))
    await waitFor(() => expect(screen.getByTestId('composer-chips').textContent).toContain('payroll.csv'))
  })

  it('hands the send OUR payload — base64 and media type, not the library\'s chip', async () => {
    const submit = vi.fn().mockResolvedValue(undefined)
    draw({ onSubmit: submit })
    drop(new File(['id,name\n1,Priya'], 'payroll.csv', { type: 'text/csv' }))
    await waitFor(() => expect(screen.getByTestId('composer-chips')).toBeTruthy())

    fireEvent.click(send())
    await waitFor(() => expect(submit).toHaveBeenCalledTimes(1))
    const submission = submit.mock.calls[0]?.[0] as ComposerSubmission
    expect(submission.attachments).toHaveLength(1)
    expect(submission.attachments[0]).toMatchObject({ name: 'payroll.csv', mediaType: 'text/csv' })
    expect(typeof submission.attachments[0]?.base64).toBe('string')
    expect(submission.conversationId).toBe('chat-1')
  })

  it('★ says a refused file out loud, which the library would swallow', async () => {
    // Both the dropzone and the paste handler wrap `addAttachment` in `try { … } catch {}`. Without
    // the adapter reporting, a file over the cap is dropped in silence and the citizen believes
    // the model can see it. Mutation receipt: drop `onRefused` from the adapter and this goes red.
    const onUrgent = vi.fn()
    draw({ onUrgent })
    drop(new File([new Uint8Array(4 * 1024 * 1024 + 1)], 'huge.png', { type: 'image/png' }))
    await waitFor(() => expect(onUrgent).toHaveBeenCalledTimes(1))
    expect(onUrgent.mock.calls[0]?.[0]).toMatch(/too large|4 MB|smaller/i)
    // …and nothing was staged.
    expect(screen.queryByTestId('composer-chips')).toBeNull()
  })

  it('refuses a format this platform does not accept, in our words', async () => {
    const onUrgent = vi.fn()
    draw({ onUrgent })
    drop(new File(['x'], 'slides.pptx', { type: 'application/vnd.openxmlformats-officedocument.presentationml.presentation' }))
    await waitFor(() => expect(onUrgent).toHaveBeenCalledTimes(1))
    expect(onUrgent.mock.calls[0]?.[0]).toMatch(/isn't supported|is not supported/i)
  })

  it('★ counts against the per-message cap across a batch, not one file at a time', async () => {
    // The cap bypass R57 records: a check that sees only the arriving file lets a sixth through.
    // The adapter reads the LIVE staged list rather than a closure, which is what makes the sixth
    // drop see five already there. Mutation receipt: pass `staged: () => []` and this goes red.
    const onUrgent = vi.fn()
    draw({ onUrgent })
    for (let i = 0; i < 5; i += 1) drop(new File(['x'], `f${i}.png`, { type: 'image/png' }))
    await waitFor(() => expect(screen.getByTestId('composer-chips').textContent).toContain('f4.png'))

    drop(new File(['x'], 'sixth.png', { type: 'image/png' }))

    await waitFor(() => expect(onUrgent).toHaveBeenCalled())
    expect(onUrgent.mock.calls.at(-1)?.[0]).toMatch(/at most 5 files/i)
    expect(screen.getByTestId('composer-chips').textContent).not.toContain('sixth.png')
  })
})
