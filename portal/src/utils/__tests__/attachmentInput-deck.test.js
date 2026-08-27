// Deck (.pptx) composer behavior with the feature flag ENABLED. The allowlist +
// ACCEPT_ATTR offering is gated on DECK_ATTACHMENTS_ENABLED (a build-time const),
// so we mock the features module to true for this whole file (vi.mock is hoisted
// before the attachmentInput import, so its module-load gating sees `true`).
import { describe, it, expect, vi } from 'vitest'

vi.mock('../../config/features', () => ({
  DECK_ATTACHMENTS_ENABLED: true,
}))

const {
  validateAttachmentFiles,
  resolveMediaType,
  ACCEPT_ATTR,
  ALLOWED_MEDIA_TYPES,
  PPTX_MEDIA_TYPE,
  LEGACY_PPT_REJECT_MSG,
  MAX_FILE_SIZE,
} = await import('../attachmentInput')

const file = (name, type, size = 1024) => ({ name, type, size })

describe('deck (.pptx) — feature ENABLED', () => {
  it('offers .pptx in the allowlist and the OS picker ACCEPT_ATTR', () => {
    expect(ALLOWED_MEDIA_TYPES).toContain(PPTX_MEDIA_TYPE)
    expect(ACCEPT_ATTR).toContain('.pptx')
    expect(ACCEPT_ATTR).toContain(PPTX_MEDIA_TYPE)
  })

  it('accepts a .pptx ≤ 4 MB (even with an empty/generic OS MIME) via the resolved type', () => {
    expect(validateAttachmentFiles([file('q3.pptx', '')], 0)).toEqual({ ok: true })
    expect(validateAttachmentFiles([file('deck.pptx', 'application/octet-stream')], 0)).toEqual({ ok: true })
    expect(validateAttachmentFiles([file('z.pptx', 'application/zip')], 0)).toEqual({ ok: true })
    expect(validateAttachmentFiles([file('d.pptx', PPTX_MEDIA_TYPE, MAX_FILE_SIZE)], 0)).toEqual({ ok: true })
  })

  it('rejects a .pptx over the 4 MB cap', () => {
    expect(validateAttachmentFiles([file('big.pptx', PPTX_MEDIA_TYPE, MAX_FILE_SIZE + 1)], 0).error).toMatch(/4 MB/)
  })

  it('rejects a legacy .ppt with the "save as .pptx" message (no PDF mention)', () => {
    // Moved here from the unmocked attachmentInput.test.js, where it sat under a
    // "flag-independent" heading it had stopped earning: the advice to save as .pptx
    // only leads anywhere while .pptx is in the allowlist, i.e. only in THIS file's
    // world. The flag-off counterpart is in attachmentInput-deck-disabled.test.js.
    expect(validateAttachmentFiles([file('old.ppt', 'application/vnd.ms-powerpoint')], 0)).toEqual({
      error: LEGACY_PPT_REJECT_MSG,
    })
    expect(validateAttachmentFiles([file('deck.ppt', '')], 0)).toEqual({ error: LEGACY_PPT_REJECT_MSG })
    // The deck -> PDF conversion is invisible to the user, so the message must not
    // leak it. This assertion belongs where the conversion exists: with decks off
    // there is no conversion to reveal.
    expect(LEGACY_PPT_REJECT_MSG).not.toMatch(/pdf/i)
  })

  it('resolveMediaType maps .pptx to the OOXML presentation type', () => {
    expect(resolveMediaType(file('q3.pptx', ''))).toBe(PPTX_MEDIA_TYPE)
  })

  it('the unsupported-type message advertises PowerPoint when enabled', () => {
    const res = validateAttachmentFiles([file('clip.mp3', 'audio/mpeg')], 0)
    expect(res.error).toMatch(/PowerPoint \(\.pptx\)/)
  })
})
