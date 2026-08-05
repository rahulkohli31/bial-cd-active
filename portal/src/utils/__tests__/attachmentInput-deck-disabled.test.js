// Deck (.pptx) composer behavior with the feature flag DISABLED. The shipped
// default now has DECK_ATTACHMENTS_ENABLED ON, so the off-state offering can no
// longer be asserted against the real flag. We mock the features module to false
// for this whole file (vi.mock is hoisted before the attachmentInput import, so its
// module-load gating sees `false`). Mirrors attachmentInput-deck.test.js (ENABLED).
import { describe, it, expect, vi } from 'vitest'

vi.mock('../../config/features', () => ({
  DECK_ATTACHMENTS_ENABLED: false,
}))

const { validateAttachmentFiles, ACCEPT_ATTR, ALLOWED_MEDIA_TYPES, PPTX_MEDIA_TYPE } = await import(
  '../attachmentInput'
)

const file = (name, type, size = 1024) => ({ name, type, size })

describe('deck (.pptx) — feature DISABLED', () => {
  it('does NOT offer .pptx in the allowlist or the OS picker ACCEPT_ATTR', () => {
    expect(ALLOWED_MEDIA_TYPES).not.toContain(PPTX_MEDIA_TYPE)
    expect(ACCEPT_ATTR).not.toContain('.pptx')
    expect(ACCEPT_ATTR).not.toContain(PPTX_MEDIA_TYPE)
  })

  it('rejects a .pptx upload with the generic "isn\'t supported" copy (no PowerPoint mention)', () => {
    const res = validateAttachmentFiles([file('q3.pptx', PPTX_MEDIA_TYPE)], 0)
    expect(res.error).toMatch(/isn't supported/)
    expect(res.error).not.toMatch(/powerpoint/i) // copy doesn't advertise it when off
  })
})
