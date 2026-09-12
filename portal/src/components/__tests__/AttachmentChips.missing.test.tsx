/**
 * A CHIP WHOSE FILE HAS GONE SAYS SO — for every format, not just images.
 *
 * The `missing` state existed and was unreachable for two of the three chip types: the file and
 * PDF branches both `return` above the `if (missing)` check, so pressing one set the state, the
 * component re-rendered, the early return fired again, and nothing changed on screen. The press
 * was absorbed in silence — the single thing a control must never do, and precisely the defect the
 * state had been added to fix.
 *
 * The existing suite next door renders with `renderToStaticMarkup`, so it cannot press anything;
 * that is why this file exists and uses a real render.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'

import AttachmentChips from '../AttachmentChips'

const h = vi.hoisted(() => ({ fetchAttachmentObjectUrl: vi.fn() }))

vi.mock('../../utils/attachmentApi', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../utils/attachmentApi')>()
  return { ...actual, fetchAttachmentObjectUrl: h.fetchAttachmentObjectUrl }
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const XLSX = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'

describe('a chip whose file is gone', () => {
  it.each([
    ['a spreadsheet', XLSX, 'gate-roster.xlsx'],
    ['a PDF', 'application/pdf', 'lease.pdf'],
  ])('says so when %s can no longer be fetched', async (_label, mediaType, name) => {
    // The file is gone from storage: the fetch resolves to nothing rather than throwing, which is
    // the shape a swept or reclaimed attachment actually produces.
    h.fetchAttachmentObjectUrl.mockResolvedValue(null)
    render(<AttachmentChips attachments={[{ attachmentId: 'att_1', kind: 'file', name, mediaType }]} />)

    fireEvent.click(screen.getByRole('button'))

    // ★ THE PRESS CHANGES SOMETHING. Before the fix the component re-rendered into the same chip
    // and the citizen had no way to tell the press had been received at all.
    const status = await waitFor(() => screen.getByRole('status'))
    expect(status.textContent).toContain(name)
    expect(status.textContent).toMatch(/no longer available/)
    // R23c: the change has to reach assistive technology, which has no other way to notice it.
    expect(status.getAttribute('aria-live')).toBe('polite')
  })

  it('leaves a working chip alone', async () => {
    h.fetchAttachmentObjectUrl.mockResolvedValue('blob:ok')
    render(
      <AttachmentChips
        attachments={[{ attachmentId: 'att_2', kind: 'file', name: 'book.xlsx', mediaType: XLSX }]}
      />,
    )

    fireEvent.click(screen.getByRole('button'))

    await waitFor(() => expect(h.fetchAttachmentObjectUrl).toHaveBeenCalled())
    expect(screen.queryByRole('status')).toBeNull()
  })
})
