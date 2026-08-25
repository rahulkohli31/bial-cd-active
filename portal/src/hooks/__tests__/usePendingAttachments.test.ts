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

describe('usePendingAttachments — restorePending respects the per-message cap', () => {
  const attachment = (id: string): PendingAttachment => ({
    id, name: `${id}.png`, mediaType: 'image/png', size: 10, base64: 'AAA',
  })

  it('clamps the merged total to the cap and says so, rather than appending unbounded', async () => {
    // The real sequence: 5 files staged and sent (the composer clears optimistically), 5 more
    // staged during the upload window — those validate against an empty list, so they are
    // legitimately accepted — then the upload fails and the first batch is restored on top.
    // An unclamped merge leaves 10 chips against a cap of 5, and `validateAttachmentFiles`
    // (the only enforcement point) never runs again on the send path, so all 10 would send.
    h.fileToBase64.mockImplementation(async () => 'AAA')
    const { result } = renderHook(() => usePendingAttachments())

    await act(async () => {
      await result.current.handleFiles([
        makeFile('n1.png'), makeFile('n2.png'), makeFile('n3.png'), makeFile('n4.png'), makeFile('n5.png'),
      ])
    })
    expect(result.current.pendingAttachments).toHaveLength(5)

    act(() => {
      result.current.restorePending([attachment('r1'), attachment('r2'), attachment('r3')])
    })

    expect(result.current.pendingAttachments).toHaveLength(5)
    expect(result.current.attachToast).toBe('You can attach at most 5 files per message.')
  })

  it('truncates the RESTORED batch, never the files the user just staged', () => {
    const { result } = renderHook(() => usePendingAttachments())

    act(() => {
      result.current.restorePending([attachment('own1'), attachment('own2'), attachment('own3')])
    })
    act(() => {
      // Only two slots left, three restored — the user can see and re-pick what they staged
      // a moment ago, but has no way of knowing what the restore dropped.
      result.current.restorePending([attachment('r1'), attachment('r2'), attachment('r3')])
    })

    expect(result.current.pendingAttachments.map((a) => a.id)).toEqual(['own1', 'own2', 'own3', 'r1', 'r2'])
  })
})

describe('usePendingAttachments — the send clear vs. the chat-switch clear', () => {
  it('clearPendingAfterSend lets an in-flight read land, staged for the next message', async () => {
    // A large PDF dropped just before Enter used to vanish outright: the send path shared the
    // chat-switch clear, whose generation bump supersedes the read — no chip, no toast, no log,
    // and the user believes it was attached when the model never saw it.
    let resolveRead: (v: string) => void = () => {}
    h.fileToBase64.mockImplementation(() => new Promise<string>((resolve) => { resolveRead = resolve }))
    const { result } = renderHook(() => usePendingAttachments())

    let pending: Promise<void> = Promise.resolve()
    act(() => {
      pending = result.current.handleFiles([makeFile('big.pdf')])
    })
    act(() => {
      result.current.clearPendingAfterSend()
    })
    await act(async () => {
      resolveRead('AAA')
      await pending
    })

    expect(result.current.pendingAttachments.map((a) => a.name)).toEqual(['big.pdf'])
  })

  it('clearPending (the chat switch) still supersedes it — the bytes belong to the old chat', async () => {
    let resolveRead: (v: string) => void = () => {}
    h.fileToBase64.mockImplementation(() => new Promise<string>((resolve) => { resolveRead = resolve }))
    const { result } = renderHook(() => usePendingAttachments())

    let pending: Promise<void> = Promise.resolve()
    act(() => {
      pending = result.current.handleFiles([makeFile('big.pdf')])
    })
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
