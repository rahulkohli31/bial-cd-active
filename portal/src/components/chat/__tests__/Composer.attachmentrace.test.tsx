/**
 * A FILE PICKED IN ONE CHAT NEVER LANDS IN THE NEXT ONE (plan 002, U5).
 *
 * Reading a file is asynchronous and a citizen can step to another chat while it is still being
 * read, so the read resolves against a composer that by then belongs to a different conversation.
 * A file that landed anyway would be counted against the new chat's budgets and sent into a
 * conversation nobody attached it to.
 *
 * THIS PROPERTY OUTLIVED THE CODE THAT USED TO CARRY IT. It was pinned against the hand-rolled
 * `usePendingAttachments`, whose reads were guarded by a generation counter; that hook was
 * replaced by the library's composer and deleted, so the assertion is re-made here against the
 * path a citizen actually takes.
 *
 * THE READ IS THE ONLY THING STUBBED, because it is the boundary where the browser hands bytes
 * back and holding it open is the only way to stand inside the window under test. The adapter,
 * the runtime and the composer are all the real ones.
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

describe('a read that outlives the chat it was started in', () => {
  it('lands nothing after the citizen has stepped to another conversation', async () => {
    let finishRead: (base64: string) => void = () => {}
    h.fileToBase64.mockImplementation(
      () => new Promise<string>((resolve) => { finishRead = resolve }),
    )
    const { rerender, props } = draw()

    fireEvent.drop(screen.getByTestId('composer-dropzone'), {
      dataTransfer: {
        types: ['Files'],
        files: [new File(['id,name\n1,Priya'], 'payroll.csv', { type: 'text/csv' })],
      },
    })
    // The read has started and is being held open — the window this test exists for.
    await waitFor(() => expect(h.fileToBase64).toHaveBeenCalledTimes(1))
    expect(screen.queryByTestId('composer-chips')).toBeNull()

    rerender(<ComposerHarness><Composer {...props} conversationId="chat-2" /></ComposerHarness>)
    await act(async () => {
      finishRead('aWQsbmFtZQoxLFByaXlh')
      await Promise.resolve()
    })

    expect(screen.queryByTestId('composer-chips')).toBeNull()
    // LIVENESS — the sibling's composer is mounted and typeable, so the chip's absence is the
    // switch discarding the read rather than a composer that failed to render.
    const box = screen.getByTestId('composer-input') as HTMLTextAreaElement
    fireEvent.change(box, { target: { value: 'a fresh start' } })
    expect(box.value).toBe('a fresh start')
  })
})
