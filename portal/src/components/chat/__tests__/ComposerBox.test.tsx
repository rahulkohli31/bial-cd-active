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
import { SendRefusal } from '../sendRefusal'
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
  /** What the box reports it KEPT once an accepted send has been reconciled. */
  onAccepted?: (conversationId: string, keptText: string) => void
}

function draw({ onSubmit, unavailableReason = null, onUrgent = vi.fn(), onAccepted }: DrawOptions = {}) {
  const submit = onSubmit ?? vi.fn().mockResolvedValue(undefined)
  const view = render(
    <ComposerHarness>
      <ComposerBox
        conversationId="chat-1"
        placeholder="Describe the change you need…"
        onSubmit={submit}
        unavailableReason={unavailableReason}
        onUrgent={onUrgent}
        {...(onAccepted ? { onAccepted } : {})}
      />
    </ComposerHarness>,
  )
  return { ...view, submit, onUrgent }
}

const box = () => screen.getByTestId('composer-input') as HTMLTextAreaElement
const send = () => screen.getByTestId('composer-send')
const type = (value: string) => fireEvent.change(box(), { target: { value } })
const drop = (file: File) => dropAll(file)
/** ONE gesture carrying several files — a multi-select in the OS picker, or a handful dragged in
 *  together. The library adds every one of them concurrently, which is the shape that matters. */
const dropAll = (...files: File[]) =>
  fireEvent.drop(screen.getByTestId('composer-dropzone'), {
    dataTransfer: { types: ['Files'], files },
  })
/** The staged chips, counted by the one control each chip owns. */
const chips = () => screen.queryAllByLabelText(/^Remove /)

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

  it('★ carries the two controls TOGETHER at the right edge', () => {
    // Every board that draws a composer puts `margin-left:auto` on the paperclip and lets Send
    // follow it on the row's gap, so the pair sits in the box's bottom-right corner. With the auto
    // margin on Send instead they were pushed to opposite ends: measured 308px apart on the project
    // rail at 1440 and 932px apart at 1024, which is one control at each end of the screen.
    draw()
    const classes = (el: HTMLElement) => el.className.split(/\s+/)
    expect(classes(screen.getByTestId('composer-attach'))).toContain('ms-auto')
    expect(classes(send())).not.toContain('ms-auto')
  })
})

describe('★ the pale send circle means LOCKED, not "you have not typed yet"', () => {
  // The canvas paints the send circle #D6DDE4 (`bg-canvas-sendoff`) in exactly two boards —
  // PlanReady and PlanRevised — and in both the composer is locked by a pending offer, with the
  // greyed circle beside "Choose one of the two above". Fourteen other boards draw an EMPTY
  // composer, placeholder showing, with the circle teal. Keying the treatment off "is there text"
  // therefore put the locked look on the resting state of every screen in the product — and white
  // on #D6DDE4 is about 1.4:1, a contrast failure the boards do not have.

  it('is teal over an empty box', () => {
    draw()
    expect(send().className).toMatch(/bg-primary/)
    expect(send().className).not.toMatch(/bg-canvas-sendoff/)
  })

  it('stays teal once there is something to send', () => {
    draw()
    type('add a checkout column')
    expect(send().className).toMatch(/bg-primary/)
  })

  it('★ goes pale when something genuinely forbids sending', () => {
    draw({ unavailableReason: 'The assistant is still working.' })
    expect(send().className).toMatch(/bg-canvas-sendoff/)
    expect(send().className).not.toMatch(/bg-primary/)
  })

  it('★ still refuses an empty send, which is what `aria-disabled` is for', () => {
    // The look changed; the enforcement did not. Without this the fix above would read as
    // "the empty composer now sends", which is the opposite of what it does.
    const onSubmit = vi.fn()
    draw({ onSubmit })
    expect(send().getAttribute('aria-disabled')).toBe('true')
    fireEvent.click(send())
    expect(onSubmit).not.toHaveBeenCalled()
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
    const refusal = new SendRefusal('You already have a build running in this chat.')
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
    const refusal = new SendRefusal('swallowed', { silent: true })
    const onUrgent = vi.fn()
    draw({ onSubmit: vi.fn().mockRejectedValue(refusal), onUrgent })
    type('held')
    fireEvent.click(send())
    await waitFor(() => expect(box().value).toBe('held'))
    expect(onUrgent).not.toHaveBeenCalled()
  })

  it('★ keeps what was typed WHILE the send was in flight — it clears only what it sent', async () => {
    // THE AFFORDANCE IS THE HAZARD. The box stays typable while a send is out (by design), so an
    // unconditional clear on success deletes whatever arrived in the meantime. This is the same
    // loss the file's docblock says it exists to prevent, reached through the happy path instead
    // of the failure path.
    let release = () => {}
    const gate = new Promise<void>((r) => { release = r })
    const onSubmit = vi.fn().mockReturnValue(gate)
    draw({ onSubmit })

    type('first message')
    fireEvent.click(send())
    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1))

    // The citizen keeps typing while the request is out.
    type('first message and a second thought')
    release()

    // Only the sent words go. The rest is still there to send.
    await waitFor(() => expect(box().value).toBe(' and a second thought'))
    // The sent text was the snapshot, not what the box held when the promise settled.
    expect(onSubmit.mock.calls[0]?.[0]?.text).toBe('first message')
  })

  it('★ leaves a REWRITTEN box alone — an edit it cannot reconcile is not a licence to cut', async () => {
    // THE THIRD OUTCOME OF THE RECONCILIATION, and the one nothing drove. An exact match clears
    // the box and an appended tail is kept (both above); anything else — the citizen selected all
    // and started again while the request was out — is an edit that cannot be reconciled, and the
    // code deliberately does nothing. Mutation receipt: weaken the `startsWith` guard to an
    // unconditional slice and this goes red with the first twenty characters chopped off a
    // sentence the citizen can still see.
    let release = () => {}
    const gate = new Promise<void>((r) => { release = r })
    const onSubmit = vi.fn().mockReturnValue(gate)
    // The box's own report of what it kept is the settled signal — waiting on the text would be
    // waiting for a value the box already holds, which is no wait at all.
    const onAccepted = vi.fn()
    draw({ onSubmit, onAccepted })

    type('make the header blue')
    fireEvent.click(send())
    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1))

    type('actually make it red')
    release()

    await waitFor(() => expect(onAccepted).toHaveBeenCalledTimes(1))
    expect(onAccepted).toHaveBeenCalledWith('chat-1', 'actually make it red')
    expect(box().value).toBe('actually make it red')
    // …and what went to the server was the snapshot, which is the other half of the rule.
    expect(onSubmit.mock.calls[0]?.[0]?.text).toBe('make the header blue')
  })

  it('★ keeps a file ATTACHED while the send was in flight — the other half of the same rule', async () => {
    // THE SIBLING OF THE TEST ABOVE, and the one whose absence let the attachment half of the fix
    // ship unpinned: `kept` / `clearAttachments()` / re-add could be reverted to a bare
    // `clearAttachments()` and every other test here would stay green.
    let release = () => {}
    const gate = new Promise<void>((r) => { release = r })
    const onSubmit = vi.fn().mockReturnValue(gate)
    draw({ onSubmit })

    type('here is the first file')
    drop(new File(['id,name'], 'sent.csv', { type: 'text/csv' }))
    await waitFor(() => expect(screen.getByTestId('composer-chips').textContent).toContain('sent.csv'))

    fireEvent.click(send())
    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1))

    // The citizen stages a SECOND file while the first send is still out.
    drop(new File(['a,b'], 'staged-during.csv', { type: 'text/csv' }))
    await waitFor(() =>
      expect(screen.getByTestId('composer-chips').textContent).toContain('staged-during.csv'))

    release()

    // The one that was sent goes. The one that was not stays.
    await waitFor(() =>
      expect(screen.getByTestId('composer-chips').textContent).not.toContain('sent.csv'))
    expect(screen.getByTestId('composer-chips').textContent).toContain('staged-during.csv')
  })

  it('★ does not claim the message failed when only the tidying up did', async () => {
    // The send SUCCEEDED. Whatever goes wrong while clearing afterwards, the citizen must not be
    // told their message did not send — it did, and telling them otherwise invites a duplicate.
    const onSubmit = vi.fn().mockResolvedValue(undefined)
    const onUrgent = vi.fn()
    draw({ onSubmit, onUrgent })
    type('this one lands')
    fireEvent.click(send())

    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1))
    // Whether or not the tidy-up throws in this environment, the failure sentence is reserved for
    // sends that actually failed.
    const said = onUrgent.mock.calls.map((c) => String(c[0])).join(' ')
    expect(said).not.toMatch(/did not send/i)
  })

  it('★ still empties the box when nothing was added during the send', async () => {
    // The ordinary case must not regress into "never clears".
    const onSubmit = vi.fn().mockResolvedValue(undefined)
    draw({ onSubmit })
    type('just this')
    fireEvent.click(send())
    await waitFor(() => expect(box().value).toBe(''))
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

  it('★ holds the cap when EIGHT files arrive in ONE gesture, which is how the library adds them', async () => {
    // THE REGRESSION THIS IS WRITTEN AGAINST. The dropzone, the OS picker and the paste handler
    // all fan out with `Promise.all(files.map(…))`, so every file in one drop starts its `add`
    // before any of them finishes. Validating against `staged()` alone therefore validated all
    // eight against an empty list, and all eight were staged with nothing said — 5 is the cap.
    // Mutation receipt: drop the adapter's claim list and this goes red at eight chips.
    const onUrgent = vi.fn()
    draw({ onUrgent })

    dropAll(...Array.from({ length: 8 }, (_, i) => new File(['x'], `f${i}.png`, { type: 'image/png' })))

    // WAIT FOR THE WHOLE GESTURE TO SETTLE, not for a count to pass through 5 on its way to 8:
    // every one of the eight files ends as either a chip or a refusal, so their sum is the one
    // condition that is true exactly once and only at the end.
    await waitFor(() => expect(chips().length + onUrgent.mock.calls.length).toBe(8))
    expect(chips()).toHaveLength(5)
    expect(onUrgent.mock.calls.at(-1)?.[0]).toMatch(/at most 5 files/i)
  })

  it('★ holds the 512 KB text budget inside one gesture too — the other cap the same gap opened', async () => {
    // Inline text rides in every turn of the conversation, so the budget is cumulative. Three
    // 250 KB spreadsheets dropped together are 750 KB; two fit and the third is refused, and
    // saying so is the difference between a bounded prompt and a silently doubled one.
    const onUrgent = vi.fn()
    draw({ onUrgent })
    const sheet = (name: string) => new File([new Uint8Array(250 * 1024)], name, { type: 'text/csv' })

    dropAll(sheet('jan.csv'), sheet('feb.csv'), sheet('mar.csv'))

    await waitFor(() => expect(chips().length + onUrgent.mock.calls.length).toBe(3))
    expect(chips()).toHaveLength(2)
    expect(onUrgent.mock.calls.at(-1)?.[0]).toMatch(/512 KB total limit/i)
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
