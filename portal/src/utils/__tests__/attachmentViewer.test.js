import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { base64ToBlob, openAttachmentBytes, openPdf } from '../attachmentViewer'

describe('base64ToBlob', () => {
  it('decodes raw base64 into a typed Blob of the right size', () => {
    const blob = base64ToBlob('QUJD', 'application/pdf') // base64('ABC')
    expect(blob.type).toBe('application/pdf')
    expect(blob.size).toBe(3)
  })
})

describe('openAttachmentBytes / openPdf', () => {
  let clickSpy
  beforeEach(() => {
    // jsdom implements neither of these — stub them.
    URL.createObjectURL = vi.fn(() => 'blob:mock-url')
    URL.revokeObjectURL = vi.fn()
    // Spy on the anchor click (jsdom would otherwise warn "navigation not implemented").
    clickSpy = vi.fn()
    const realCreate = document.createElement.bind(document)
    vi.spyOn(document, 'createElement').mockImplementation((tag) => {
      const el = realCreate(tag)
      if (tag === 'a') el.click = clickSpy
      return el
    })
    vi.useFakeTimers()
  })
  afterEach(() => {
    delete URL.createObjectURL
    delete URL.revokeObjectURL
    vi.restoreAllMocks()
    vi.useRealTimers()
  })

  it('returns false (and builds nothing) when there are no bytes', () => {
    expect(openAttachmentBytes('', 'a.pdf')).toBe(false)
    expect(URL.createObjectURL).not.toHaveBeenCalled()
    expect(clickSpy).not.toHaveBeenCalled()
  })

  it('opens the blob URL via a single new-tab anchor click and revokes it later', () => {
    expect(openPdf('QUJD', 'a.pdf')).toBe(true)
    expect(URL.createObjectURL).toHaveBeenCalledOnce()
    expect(clickSpy).toHaveBeenCalledOnce() // exactly one action — no double tab+download
    vi.advanceTimersByTime(60_000)
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:mock-url')
  })

  it('returns false when the blob cannot be built (no spurious open)', () => {
    URL.createObjectURL = vi.fn(() => {
      throw new Error('boom')
    })
    expect(openPdf('QUJD', 'x.pdf')).toBe(false)
    expect(clickSpy).not.toHaveBeenCalled()
  })
})
