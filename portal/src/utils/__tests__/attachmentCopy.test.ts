/**
 * The composer's own refusal sentence, reconciled against the allowlist it describes (R48, R56).
 *
 * The sentence a citizen actually meets when the picker says no —
 * `validateAttachmentFiles`'s "…isn't supported. Attach an image (PNG, JPEG, GIF, WebP), a PDF,
 * a Word (.docx) or Excel (.xlsx) file, or a text file (CSV, TXT)." — is a PROMISE about a file
 * picker, and no test reads it. It goes false the moment `ALLOWED_MEDIA_TYPES` narrows, which is
 * exactly the drift #157 B2 shipped once already, in this same area, in the other direction.
 *
 * TWO DIRECTIONS, like the Help FAQ's block: a format the picker accepts must be NAMED, and a
 * format it refuses must NOT be. A one-way test is what let ".pptx" survive in the copy after the
 * composer stopped taking it.
 *
 * IT DRIVES THE VALIDATOR RATHER THAN SCANNING THE CONSTANT, and that is the whole technique.
 * The message is assembled from a flag-dependent fragment, so reading the source string would
 * reconcile a sentence no citizen is shown; calling the validator reads what a citizen reads.
 * It also gives the reachability rule for free — a branch that cannot be taken at the shipped
 * flag values is a branch this file never sees, so an unreachable contradiction (the legacy
 * `.ppt` advice) cannot redden a release that has not narrowed anything yet.
 *
 * NOT COVERED HERE, on purpose: `LEGACY_DOC_REJECT_MSG`'s "save as .docx (or PDF)". It is
 * REACHABLE today and its advice is true today, and the constant is deleted by the same change
 * that would falsify it (the attachment narrowing, in the file that owns it). Gating a sentence
 * whose removal is already part of the change that breaks it buys nothing.
 */
import { describe, it, expect } from 'vitest'
import {
  validateAttachmentFiles,
  ALLOWED_MEDIA_TYPES,
  WORD_MEDIA_TYPE,
  EXCEL_MEDIA_TYPE,
  PPTX_MEDIA_TYPE,
} from '../attachmentInput'

/** The user-facing message for a file the composer refuses — fails loudly if it accepted it. */
function refusalFor(file: File): string {
  const result = validateAttachmentFiles([file])
  expect('error' in result, `the composer ACCEPTED "${file.name}" — there is no refusal to read`).toBe(true)
  return (result as { error: string }).error
}

/** A file no allowlist here has ever accepted, so it always reaches the generic refusal. */
const unsupported = () => new File(['x'], 'terminal-3.zip', { type: 'application/zip' })

describe('the composer refusal sentence agrees with the real allowlist', () => {
  it('names every format the picker accepts and none that it refuses', () => {
    const message = refusalFor(unsupported())
    // The bounded inventory of formats this sentence is ABOUT. Each is reconciled in both
    // directions, so narrowing the allowlist without editing the sentence goes red — and so
    // does widening it.
    for (const [type, phrase] of [
      ['image/png', /PNG/i],
      ['image/jpeg', /JPEG/i],
      ['image/gif', /GIF/i],
      ['image/webp', /WebP/i],
      ['application/pdf', /PDF/i],
      ['text/csv', /CSV/i],
      ['text/plain', /TXT/i],
      [WORD_MEDIA_TYPE, /Word|\.docx/i],
      [EXCEL_MEDIA_TYPE, /Excel|\.xlsx/i],
      [PPTX_MEDIA_TYPE, /PowerPoint|\.pptx/i],
    ] as const) {
      if (ALLOWED_MEDIA_TYPES.includes(type)) {
        expect(message, `the picker accepts ${type} but the refusal never names it`).toMatch(phrase)
      } else {
        expect(message, `the picker refuses ${type} but the refusal still offers it`).not.toMatch(phrase)
      }
    }
  })

  it('is a real sentence, not an empty string every negative assertion would pass against', () => {
    // The liveness half. `not.toMatch` passes on '' just as happily as on honest copy.
    const message = refusalFor(unsupported())
    expect(message.length).toBeGreaterThan(40)
    expect(message).toContain('terminal-3.zip')
  })

  it('never advises a citizen to bring back a format the picker would also refuse', () => {
    // R56 as behaviour rather than as a constant: advice is only honest while it leads
    // somewhere. `LEGACY_PPT_REJECT_MSG` says "save as .pptx" and is gated on the deck flag
    // precisely so it cannot be reached while .pptx is off the allowlist — which is what
    // driving the validator proves, instead of trusting the gate by reading it.
    const message = refusalFor(new File(['x'], 'gate-plan.ppt', { type: 'application/vnd.ms-powerpoint' }))
    if (!ALLOWED_MEDIA_TYPES.includes(PPTX_MEDIA_TYPE)) {
      expect(message, 'the refusal tells the citizen to re-save as a format the picker refuses').not.toMatch(/\.pptx/i)
    }
  })
})
