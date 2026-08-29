// Deck (.pptx) composer behavior with the feature flag DISABLED. This is the SHIPPED
// state as of #157 B2 — the mock below pins the behaviour to the off-world explicitly
// so these tests keep their meaning if the default ever flips back, and so they read
// as the deliberate mirror of attachmentInput-deck.test.js (ENABLED) rather than as
// an accident of today's constant. The shipped VALUE itself is asserted against the
// real, unmocked flag in attachmentInput.test.js; a mocked module can never pin that.
// (vi.mock is hoisted before the attachmentInput import, so its module-load gating
// sees `false`.)
import { describe, it, expect, vi } from 'vitest'

vi.mock('../../config/features', () => ({
  DECK_ATTACHMENTS_ENABLED: false,
}))

const { validateAttachmentFiles, ACCEPT_ATTR, ALLOWED_MEDIA_TYPES, PPTX_MEDIA_TYPE, LEGACY_PPT_REJECT_MSG } =
  await import('../attachmentInput')

const file = (name, type, size = 1024) => ({ name, type, size })
const SAVE_AS_PPTX = /save as \.pptx/i
const UNSUPPORTED = /isn't supported/

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

  it('sends a legacy .ppt down the SAME generic path, not to a .pptx that is also refused', () => {
    // The flag-off half of the pair whose flag-on half lives in attachmentInput-deck.test.js.
    // With decks off the legacy-.ppt branch does not run, because its only advice
    // ("save as .pptx") leads to a file this very allowlist refuses. Both PowerPoint
    // formats get one honest message that names what IS accepted.
    for (const f of [file('old.ppt', 'application/vnd.ms-powerpoint'), file('deck.ppt', '')]) {
      const res = validateAttachmentFiles([f], 0)
      expect(res.error).not.toBe(LEGACY_PPT_REJECT_MSG)
      expect(res.error).not.toMatch(SAVE_AS_PPTX)
      expect(res.error).toMatch(UNSUPPORTED)
    }
  })

  it('a real .pptx the OS mislabels as ms-powerpoint is still not the legacy reject', () => {
    // The gate must not swallow the extension-wins rule: with the branch skipped this
    // file reaches the allowlist like any other, so it gets the generic message too.
    const res = validateAttachmentFiles([file('real.pptx', 'application/vnd.ms-powerpoint')], 0)
    expect(res.error).not.toBe(LEGACY_PPT_REJECT_MSG)
    expect(res.error).toMatch(UNSUPPORTED)
  })
})
