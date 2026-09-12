/**
 * A FILE THAT HAS BEEN TAKEN BUT IS NOT STAGED YET.
 *
 * `add` reads the file to base64 before the runtime appends anything, so between the drop and the
 * chip there is a window in which the composer's own view of itself is "no attachments". On a
 * four-megabyte workbook that window is long enough to press Enter in — and the send that resulted
 * carried the question WITHOUT the file, and the answer that came back simply never mentioned it.
 * Nothing about that reads as a failure to the person who attached it, which is what makes it the
 * dangerous shape rather than merely an annoying one.
 *
 * Two things close it, and both are asserted here: the box says a file is arriving, and Send
 * refuses until it has.
 *
 * THE READ IS THE ONLY THING STUBBED — the same boundary, and for the same reason, as
 * `Composer.attachmentrace.test.tsx`: holding it open is the only way to stand inside the window
 * under test. The adapter, the runtime and the composer are the real ones.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup, act } from '@testing-library/react'

import { ComposerHarness } from './_composerHarness'
import Composer, { type ComposerProps } from '../Composer'

const h = vi.hoisted(() => ({ fileToBase64: vi.fn() }))

vi.mock('../../../utils/attachmentInput', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../../utils/attachmentInput')>()
  return { ...actual, fileToBase64: h.fileToBase64 }
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
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
  return { props, ...render(<ComposerHarness><Composer {...props} /></ComposerHarness>) }
}

function dropWorkbook(name = 'movements.csv') {
  fireEvent.drop(screen.getByTestId('composer-dropzone'), {
    dataTransfer: {
      types: ['Files'],
      files: [new File(['id,name\n1,Priya'], name, { type: 'text/csv' })],
    },
  })
}

describe('a file still being read holds the send', () => {
  it('says a file is arriving, and refuses a send until it has', async () => {
    let finishRead: (base64: string) => void = () => {}
    h.fileToBase64.mockImplementation(
      () => new Promise<string>((resolve) => { finishRead = resolve }),
    )
    const { props } = draw()

    fireEvent.change(screen.getByTestId('composer-input'), { target: { value: 'what is in this?' } })
    dropWorkbook()
    await waitFor(() => expect(h.fileToBase64).toHaveBeenCalledTimes(1))

    // ★ THE WINDOW. Nothing is staged — the chip list is not even rendered — so without the
    // pending row the box is indistinguishable from one where the drop was ignored. That is what
    // a citizen concludes, and it is why they press Send.
    expect(screen.queryByTestId('composer-chips')).toBeNull()
    const pending = screen.getByTestId('composer-pending')
    expect(pending.textContent).toMatch(/adding your file/i)
    // The same news for someone who cannot see the row.
    expect(pending.getAttribute('aria-live')).toBe('polite')

    const send = screen.getByTestId('composer-send')
    expect(send.getAttribute('aria-disabled')).toBe('true')
    // The reason rides in the accessible name, the way every other reason Send waits does.
    expect(send.getAttribute('aria-label')).toMatch(/adding your file/i)

    // ★ AND IT IS ENFORCED, NOT MERELY DRAWN. `aria-disabled` says so; it does not do so, and a
    // press lands in `doSend` either way.
    fireEvent.click(send)
    await act(async () => { await Promise.resolve() })
    expect(props.onSubmit).not.toHaveBeenCalled()

    await act(async () => {
      finishRead('aWQsbmFtZQoxLFByaXlh')
      await Promise.resolve()
    })

    // Once the file is in, the row goes and Send comes back — the citizen resolved this by doing
    // nothing, which is why it is worded as an in-progress fact rather than an instruction.
    await waitFor(() => expect(screen.queryByTestId('composer-pending')).toBeNull())
    expect(screen.getByTestId('composer-chips')).toBeTruthy()
    expect(screen.getByTestId('composer-send').getAttribute('aria-disabled')).toBe('false')
  })

  it('sends the file rather than the question alone, once the read has landed', async () => {
    h.fileToBase64.mockResolvedValue('aWQsbmFtZQoxLFByaXlh')
    const { props } = draw()

    fireEvent.change(screen.getByTestId('composer-input'), { target: { value: 'summarise this' } })
    dropWorkbook()
    await waitFor(() => expect(screen.getByTestId('composer-chips')).toBeTruthy())

    fireEvent.click(screen.getByTestId('composer-send'))

    // ★ THE OUTCOME THE HOLD EXISTS FOR. The failure it prevents is not an error — it is this
    // exact call with an empty `attachments`, answered confidently and about nothing.
    await waitFor(() => expect(props.onSubmit).toHaveBeenCalledTimes(1))
    const submission = (props.onSubmit as ReturnType<typeof vi.fn>).mock.calls[0][0]
    expect(submission.attachments).toHaveLength(1)
    expect(submission.attachments[0].name).toBe('movements.csv')
  })
})
