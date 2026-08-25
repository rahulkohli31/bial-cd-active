/**
 * usePendingAttachments had no test file at all despite three claimed race/restore
 * fixes (generation-guarded reads, the per-message cap re-checked against concurrent
 * drops, and restorePending's merge-not-replace semantics) — this file pins all four
 * cases the code-review round on PR #128 called out as unasserted.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { usePendingAttachments } from '../usePendingAttachments'
import type { PendingAttachment } from '../../utils/attachmentInput'

const h = vi.hoisted(() => ({
  fileToBase64: vi.fn(),
}))

vi.mock('../../utils/attachmentInput', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../utils/attachmentInput')>()
  return { ...actual, fileToBase64: h.fileToBase64 }
})

function makeFile(name: string, size = 100, type = 'image/png') {
  return new File([new Uint8Array(size)], name, { type })
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('usePendingAttachments — chat-switch mid-read', () => {
  it('a read that resolves after clearPending() is superseded and lands nothing', async () => {
    let resolveRead: (v: string) => void = () => {}
    h.fileToBase64.mockImplementation(() => new Promise<string>((resolve) => { resolveRead = resolve }))
    const { result } = renderHook(() => usePendingAttachments())

    let pending: Promise<void> = Promise.resolve()
    act(() => {
      pending = result.current.handleFiles([makeFile('a.png')])
    })
    // The chat switch: fires before the read resolves.
    act(() => {
      result.current.clearPending()
    })
    await act(async () => {
      resolveRead('AAA')
      await pending
    })

    expect(result.current.pendingAttachments).toEqual([])
  })
})

describe('usePendingAttachments — partial batch failure', () => {
  it('one unreadable file does not discard the rest of the batch, and names the bad file', async () => {
    h.fileToBase64
      .mockResolvedValueOnce('AAA')
      .mockRejectedValueOnce(new Error('permission denied'))
      .mockResolvedValueOnce('CCC')
    const { result } = renderHook(() => usePendingAttachments())

    await act(async () => {
      await result.current.handleFiles([makeFile('a.png'), makeFile('bad.png'), makeFile('c.png')])
    })

    expect(result.current.pendingAttachments.map((a) => a.name)).toEqual(['a.png', 'c.png'])
    expect(result.current.attachToast).toBe('Could not read "bad.png".')
  })
})

describe('usePendingAttachments — concurrent drops vs. the per-message cap', () => {
  it('two overlapping 3-file drops land exactly at the cap (5) and the cap toast fires', async () => {
    h.fileToBase64.mockImplementation(async () => 'AAA')
    const { result } = renderHook(() => usePendingAttachments())

    const batch1 = [makeFile('a1.png'), makeFile('a2.png'), makeFile('a3.png')]
    const batch2 = [makeFile('b1.png'), makeFile('b2.png'), makeFile('b3.png')]

    await act(async () => {
      await Promise.all([result.current.handleFiles(batch1), result.current.handleFiles(batch2)])
    })

    // Both batches individually validate against the SAME pre-drop count (0), which is
    // exactly the race the cap has to survive — the combined total (6) exceeds the cap (5).
    expect(result.current.pendingAttachments).toHaveLength(5)
    expect(result.current.attachToast).toBe('You can attach at most 5 files per message.')
  })
})

describe('usePendingAttachments — restorePending', () => {
  const attachment = (id: string, name: string): PendingAttachment => ({
    id, name, mediaType: 'image/png', size: 10, base64: 'AAA',
  })

  it('merges restored items with whatever is already staged, instead of replacing it', () => {
    const { result } = renderHook(() => usePendingAttachments())

    act(() => {
      // Something staged before the restore fires (e.g. attached while the failing send
      // was still in flight) must survive it.
      result.current.restorePending([attachment('new1', 'staged-since.png')])
    })
    expect(result.current.pendingAttachments.map((a) => a.id)).toEqual(['new1'])

    act(() => {
      result.current.restorePending([attachment('r1', 'restored.png')])
    })
    expect(result.current.pendingAttachments.map((a) => a.id)).toEqual(['new1', 'r1'])
  })

  it('deduplicates by id — restoring the same batch twice does not double it up', () => {
    const { result } = renderHook(() => usePendingAttachments())

    act(() => {
      result.current.restorePending([attachment('r1', 'restored.png')])
    })
    act(() => {
      result.current.restorePending([attachment('r1', 'restored.png')])
    })

    expect(result.current.pendingAttachments.map((a) => a.id)).toEqual(['r1'])
  })
})
