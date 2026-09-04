/**
 * THE TWO OBJECT-URL HELPERS, WHICH IS ALL THIS MODULE IS NOW.
 *
 * This file used to test the base64 chain (`base64ToBlob` → `openAttachmentBytes` → `openPdf`)
 * from the AttachmentLightbox era. The pipeline moved to server-served object URLs, that chain
 * lost its last caller, and it went. What was never covered is what survived — `AttachmentChips`
 * reaches both helpers below on a click — so the file is repointed rather than deleted.
 *
 * WHAT IS WORTH ASSERTING HERE, given neither helper has a return value that carries much: that
 * exactly ONE action happens per call (a tab-open that also downloads, or the reverse, is the
 * failure mode of getting `target`/`download` wrong on the same anchor), that the new-tab path
 * carries `rel="noopener noreferrer"`, and that NEITHER revokes the URL — the caller's cache owns
 * its lifetime, and revoking here would break every later click on the same chip.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { openUrlInNewTab, downloadObjectUrl } from '../attachmentViewer'

describe('openUrlInNewTab / downloadObjectUrl', () => {
  let anchors
  beforeEach(() => {
    // jsdom implements neither, and would warn "navigation not implemented" on a real click.
    URL.revokeObjectURL = vi.fn()
    anchors = []
    const realCreate = document.createElement.bind(document)
    vi.spyOn(document, 'createElement').mockImplementation((tag) => {
      const el = realCreate(tag)
      if (tag === 'a') {
        el.click = vi.fn()
        anchors.push(el)
      }
      return el
    })
  })
  afterEach(() => {
    delete URL.revokeObjectURL
    vi.restoreAllMocks()
  })

  it('opens a cached object URL in a new tab with one click, and does not revoke it', () => {
    expect(openUrlInNewTab('blob:cached', 'spec.pdf')).toBe(true)
    expect(anchors).toHaveLength(1)
    const [a] = anchors
    expect(a.click).toHaveBeenCalledOnce() // exactly one action — no double tab+download
    expect(a.getAttribute('href')).toBe('blob:cached')
    expect(a.getAttribute('target')).toBe('_blank')
    expect(a.getAttribute('rel')).toBe('noopener noreferrer')
    expect(a.hasAttribute('download')).toBe(false) // absent → the browser renders the PDF inline
    expect(URL.revokeObjectURL).not.toHaveBeenCalled() // the caller's cache owns the lifetime
  })

  it('returns false (and builds nothing) when there is no URL', () => {
    expect(openUrlInNewTab('', 'spec.pdf')).toBe(false)
    expect(downloadObjectUrl('', 'spec.pdf')).toBe(false)
    expect(anchors).toHaveLength(0)
  })

  it('downloads under the part name, which is what gives the saved file its extension', () => {
    expect(downloadObjectUrl('blob:cached', 'payroll.xlsx')).toBe(true)
    const [a] = anchors
    expect(a.click).toHaveBeenCalledOnce()
    expect(a.getAttribute('download')).toBe('payroll.xlsx')
    expect(a.getAttribute('target')).toBeNull() // a download, not a second tab
    expect(URL.revokeObjectURL).not.toHaveBeenCalled()
  })

  it('falls back to a generic name rather than saving an unnamed file', () => {
    expect(downloadObjectUrl('blob:cached')).toBe(true)
    expect(anchors[0].getAttribute('download')).toBe('download')
  })
})
