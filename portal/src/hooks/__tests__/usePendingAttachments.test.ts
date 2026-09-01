/**
 * usePendingAttachments had no test file at all despite three claimed race/restore
 * fixes (generation-guarded reads, the per-message cap re-checked against concurrent
 * drops, and restorePending's merge-not-replace semantics) — this file pins all four
 * cases the code-review round on PR #128 called out as unasserted.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { usePendingAttachments } from '../usePendingAttachments'
import { MAX_FILES_PER_MESSAGE, type PendingAttachment } from '../../utils/attachmentInput'

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

  // ── R57 — THE CAP BYPASS ──────────────────────────────────────────────────────────────────
  //
  // This merged with NO `MAX_FILES_PER_MESSAGE` clamp while `handleFiles` twenty lines above had
  // one, so five staged plus five restored became ten and ten became fifteen — and because the
  // per-conversation text budget is computed from what is staged, that doubled too. It was
  // pinned by nothing, and the composer's own docblock claimed it was fixed when it was not.

  it('clamps the RESTORED batch to the per-message cap, and says one was dropped', () => {
    const { result } = renderHook(() => usePendingAttachments())
    const batch = (prefix: string, n: number) =>
      Array.from({ length: n }, (_, i) => attachment(`${prefix}${i}`, `${prefix}${i}.png`))

    // Four already staged, three handed back by a failed send: only one fits.
    act(() => result.current.restorePending(batch('a', MAX_FILES_PER_MESSAGE - 1)))
    expect(result.current.pendingAttachments).toHaveLength(MAX_FILES_PER_MESSAGE - 1)

    act(() => result.current.restorePending(batch('b', 3)))

    expect(result.current.pendingAttachments).toHaveLength(MAX_FILES_PER_MESSAGE)
    // ASSERT THE COUNT, not merely the absence of an error: a bypass that let all three through
    // would also produce no error, and that is the bug.
    expect(result.current.attachToast).toMatch(/2 files couldn’t be put back/i)
  })

  it('clamps the RESTORED items, never the ones the user just staged', () => {
    // THE ASYMMETRY IS THE DESIGN. A citizen can SEE the files they just picked and cannot see
    // the ones a failed send is handing back, so dropping the visible ones reads as the app
    // eating their work while dropping the invisible ones is a limit being enforced.
    const { result } = renderHook(() => usePendingAttachments())
    const staged = Array.from({ length: MAX_FILES_PER_MESSAGE }, (_, i) =>
      attachment(`mine${i}`, `mine${i}.png`),
    )
    act(() => result.current.restorePending(staged))

    act(() => result.current.restorePending([attachment('back1', 'restored.png')]))

    expect(result.current.pendingAttachments.map((a) => a.id)).toEqual(staged.map((a) => a.id))
    expect(result.current.attachToast).toMatch(/one file couldn’t be put back/i)
  })

  it('says nothing when the whole batch fits', () => {
    // The liveness half: the clamp must be silent on the ordinary path, or every successful
    // restore would apologise for nothing.
    const { result } = renderHook(() => usePendingAttachments())
    act(() => result.current.restorePending([attachment('r1', 'restored.png')]))
    expect(result.current.pendingAttachments).toHaveLength(1)
    expect(result.current.attachToast).toBeNull()
  })
})
