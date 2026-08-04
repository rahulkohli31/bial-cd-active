import { describe, it, expect } from 'vitest'
import {
  validateAttachmentFiles,
  validateConversationAttachmentCap,
  resolveMediaType,
  officeFormat,
  textAttachmentBytes,
  fileToBase64,
  toAttachmentRef,
  ACCEPT_ATTR,
  LEGACY_DOC_REJECT_MSG,
  LEGACY_PPT_REJECT_MSG,
  PPTX_MEDIA_TYPE,
  WORD_MEDIA_TYPE,
  EXCEL_MEDIA_TYPE,
  MAX_FILE_SIZE,
  MAX_TEXT_FILE_SIZE,
  MAX_TEXT_BYTES_PER_CONVERSATION,
  MAX_FILES_PER_MESSAGE,
  MAX_ATTACHMENTS_PER_CONVERSATION,
} from '../attachmentInput'

// validateAttachmentFiles only reads name/type/size, so plain objects suffice
// (and let us set an arbitrary size without allocating megabytes).
const file = (name, type, size = 1024) => ({ name, type, size })

describe('validateAttachmentFiles', () => {
  it('accepts a .docx and .xlsx (even with an empty/generic MIME) via the resolved Office type', () => {
    expect(validateAttachmentFiles([file('plan.docx', '')], 0)).toEqual({ ok: true })
    expect(validateAttachmentFiles([file('data.xlsx', 'application/octet-stream')], 0)).toEqual({ ok: true })
    expect(validateAttachmentFiles([file('report.docx', WORD_MEDIA_TYPE)], 0)).toEqual({ ok: true })
  })

  it('still rejects a legacy .doc with a clear "save as .docx" message', () => {
    expect(validateAttachmentFiles([file('legacy.doc', 'application/msword')], 0)).toEqual({ error: LEGACY_DOC_REJECT_MSG })
    expect(validateAttachmentFiles([file('old.doc', '')], 0)).toEqual({ error: LEGACY_DOC_REJECT_MSG })
  })

  it('accepts a real .docx even when the OS mislabels it as application/msword (extension wins)', () => {
    expect(validateAttachmentFiles([file('plan.docx', 'application/msword')], 0)).toEqual({ ok: true })
    expect(validateAttachmentFiles([file('data.xlsx', 'application/msword')], 0)).toEqual({ ok: true })
  })

  it('rejects a .docx/.xlsx over the 4 MB binary cap', () => {
    expect(validateAttachmentFiles([file('big.docx', WORD_MEDIA_TYPE, MAX_FILE_SIZE + 1)], 0).error).toMatch(/4 MB/)
    expect(validateAttachmentFiles([file('big.xlsx', '', MAX_FILE_SIZE + 1)], 0).error).toMatch(/4 MB/)
  })

  it('rejects a genuinely unsupported type with a generic message', () => {
    const res = validateAttachmentFiles([file('clip.mp3', 'audio/mpeg')], 0)
    expect(res.error).toMatch(/isn't supported/)
  })

  it('rejects a file over the 4 MB limit', () => {
    const res = validateAttachmentFiles([file('huge.png', 'image/png', MAX_FILE_SIZE + 1)], 0)
    expect(res.error).toMatch(/4 MB/)
  })

  it('rejects exceeding the per-message file cap', () => {
    const res = validateAttachmentFiles([file('a.png', 'image/png')], MAX_FILES_PER_MESSAGE)
    expect(res.error).toMatch(new RegExp(`at most ${MAX_FILES_PER_MESSAGE} files`))
  })

  it('accepts valid images and a PDF under the caps', () => {
    expect(validateAttachmentFiles([file('a.png', 'image/png')], 0)).toEqual({ ok: true })
    expect(validateAttachmentFiles([file('b.jpg', 'image/jpeg')], 0)).toEqual({ ok: true })
    expect(validateAttachmentFiles([file('c.pdf', 'application/pdf')], 0)).toEqual({ ok: true })
  })

  it('accepts a .txt (text/plain) and a .csv under the text caps', () => {
    expect(validateAttachmentFiles([file('notes.txt', 'text/plain')], 0)).toEqual({ ok: true })
    expect(validateAttachmentFiles([file('rows.csv', 'text/csv')], 0)).toEqual({ ok: true })
  })

  it('accepts an OS-mislabeled .csv (reported application/vnd.ms-excel or empty) via resolved type', () => {
    // Validation must run against the resolved type, not raw file.type — so a CSV
    // the OS labels as Excel (or leaves blank) is still accepted.
    expect(validateAttachmentFiles([file('data.csv', 'application/vnd.ms-excel')], 0)).toEqual({ ok: true })
    expect(validateAttachmentFiles([file('data.csv', '')], 0)).toEqual({ ok: true })
  })

  it('rejects a text file over the 256 KB per-file limit (binary 4 MB cap unchanged)', () => {
    const res = validateAttachmentFiles([file('big.csv', 'text/csv', MAX_TEXT_FILE_SIZE + 1)], 0)
    expect(res.error).toMatch(/256 KB/)
    // A 4 MB PDF is still accepted under the binary cap.
    expect(validateAttachmentFiles([file('spec.pdf', 'application/pdf', MAX_FILE_SIZE)], 0)).toEqual({ ok: true })
  })

  it('rejects a selection whose total text bytes exceed the per-conversation budget', () => {
    // 5 × 256 KB text files pass the per-file cap but bust the 512 KB total.
    const five = Array.from({ length: 5 }, (_, i) => file(`f${i}.txt`, 'text/plain', MAX_TEXT_FILE_SIZE))
    const res = validateAttachmentFiles(five, 0)
    expect(res.error).toMatch(new RegExp(`${MAX_TEXT_BYTES_PER_CONVERSATION / 1024} KB total`))
  })

  it('enforces the text budget CUMULATIVELY across pending picks (existingTextBytes)', () => {
    // 400 KB already pending + a new 200 KB pick = 600 KB > 512 KB → rejected,
    // even though the new pick alone is well under the budget.
    const res = validateAttachmentFiles([file('more.csv', 'text/csv', 200 * 1024)], 2, 400 * 1024)
    expect(res.error).toMatch(new RegExp(`${MAX_TEXT_BYTES_PER_CONVERSATION / 1024} KB total`))
    // A pick that keeps the running total under budget still passes.
    expect(validateAttachmentFiles([file('ok.csv', 'text/csv', 100 * 1024)], 1, 200 * 1024)).toEqual({ ok: true })
  })
})

describe('textAttachmentBytes', () => {
  it('sums the size of text refs only (ignores image/PDF)', () => {
    expect(
      textAttachmentBytes([
        { mediaType: 'text/csv', size: 1000 },
        { mediaType: 'image/png', size: 5000 },
        { mediaType: 'application/pdf', size: 9000 },
        { mediaType: 'text/plain', size: 200 },
      ]),
    ).toBe(1200)
  })

  it('is 0 for empty / non-array inputs', () => {
    expect(textAttachmentBytes([])).toBe(0)
    expect(textAttachmentBytes(null)).toBe(0)
  })
})

describe('resolveMediaType', () => {
  it('canonicalizes .csv → text/csv and .txt → text/plain by extension', () => {
    expect(resolveMediaType(file('data.csv', 'application/vnd.ms-excel'))).toBe('text/csv')
    expect(resolveMediaType(file('data.CSV', ''))).toBe('text/csv')
    expect(resolveMediaType(file('notes.txt', ''))).toBe('text/plain')
  })

  it('canonicalizes .docx/.xlsx by extension when the browser MIME is blank/generic', () => {
    expect(resolveMediaType(file('plan.docx', ''))).toBe(WORD_MEDIA_TYPE)
    expect(resolveMediaType(file('Q1.XLSX', 'application/octet-stream'))).toBe(EXCEL_MEDIA_TYPE)
  })

  it('falls through to file.type for non-text extensions', () => {
    expect(resolveMediaType(file('a.png', 'image/png'))).toBe('image/png')
    expect(resolveMediaType(file('c.pdf', 'application/pdf'))).toBe('application/pdf')
  })
})

describe('officeFormat', () => {
  it('maps the Office media types to word/excel and nothing else', () => {
    expect(officeFormat(WORD_MEDIA_TYPE)).toBe('word')
    expect(officeFormat(EXCEL_MEDIA_TYPE)).toBe('excel')
    expect(officeFormat('application/pdf')).toBeNull()
    expect(officeFormat('text/csv')).toBeNull()
  })
})

describe('ACCEPT_ATTR', () => {
  it('carries the text + Office MIME types AND .csv/.txt/.docx/.xlsx extension tokens for the OS picker', () => {
    expect(ACCEPT_ATTR).toContain('text/csv')
    expect(ACCEPT_ATTR).toContain('text/plain')
    expect(ACCEPT_ATTR).toContain('.csv')
    expect(ACCEPT_ATTR).toContain('.txt')
    expect(ACCEPT_ATTR).toContain(WORD_MEDIA_TYPE)
    expect(ACCEPT_ATTR).toContain(EXCEL_MEDIA_TYPE)
    expect(ACCEPT_ATTR).toContain('.docx')
    expect(ACCEPT_ATTR).toContain('.xlsx')
  })
})

// These hold regardless of the deck feature flag. The shipped default now has
// DECK_ATTACHMENTS_ENABLED ON, so the off-state offering is covered separately in
// attachmentInput-deck-disabled.test.js (which mocks the flag off) rather than
// asserted against the real flag here.
describe('deck (.pptx) — flag-independent behavior', () => {
  it('resolveMediaType canonicalizes .pptx by extension even with empty/generic MIME', () => {
    expect(resolveMediaType(file('q3.pptx', ''))).toBe(PPTX_MEDIA_TYPE)
    expect(resolveMediaType(file('DECK.PPTX', 'application/octet-stream'))).toBe(PPTX_MEDIA_TYPE)
    expect(resolveMediaType(file('zip.pptx', 'application/zip'))).toBe(PPTX_MEDIA_TYPE)
  })

  it('rejects a legacy .ppt with a clear "save as .pptx" message (no PDF mention)', () => {
    expect(validateAttachmentFiles([file('old.ppt', 'application/vnd.ms-powerpoint')], 0)).toEqual({
      error: LEGACY_PPT_REJECT_MSG,
    })
    expect(validateAttachmentFiles([file('deck.ppt', '')], 0)).toEqual({ error: LEGACY_PPT_REJECT_MSG })
    expect(LEGACY_PPT_REJECT_MSG).not.toMatch(/pdf/i) // invisible conversion
  })

  it('treats a real .pptx the OS mislabels as application/vnd.ms-powerpoint by extension (never as legacy .ppt)', () => {
    // Extension wins over the ms-powerpoint mislabel, so it is NEVER the legacy
    // .ppt rejection — independent of whether the deck feature is on or off.
    const res = validateAttachmentFiles([file('real.pptx', 'application/vnd.ms-powerpoint')], 0)
    expect(res.error || '').not.toBe(LEGACY_PPT_REJECT_MSG)
  })
})

describe('validateConversationAttachmentCap', () => {
  it('accepts when the cumulative total stays within the cap', () => {
    expect(validateConversationAttachmentCap(0, 5)).toEqual({ ok: true })
    expect(validateConversationAttachmentCap(MAX_ATTACHMENTS_PER_CONVERSATION - 1, 1)).toEqual({ ok: true })
  })

  it('rejects when an incoming batch would cross the cap', () => {
    const res = validateConversationAttachmentCap(MAX_ATTACHMENTS_PER_CONVERSATION, 1)
    expect(res.error).toMatch(new RegExp(`limit of ${MAX_ATTACHMENTS_PER_CONVERSATION} attachments`))
    // a batch that crosses the boundary is rejected wholesale
    expect(validateConversationAttachmentCap(MAX_ATTACHMENTS_PER_CONVERSATION - 1, 3).error).toBeTruthy()
  })

  it('uses wording distinct from the per-message and storage-full caps', () => {
    const res = validateConversationAttachmentCap(MAX_ATTACHMENTS_PER_CONVERSATION, 1)
    expect(res.error).toMatch(/this conversation/i)
    expect(res.error).not.toMatch(/per message/i)
  })
})

describe('toAttachmentRef', () => {
  it('strips the transient base64 down to the persisted ref', () => {
    expect(
      toAttachmentRef({ id: 'x', name: 'a.png', mediaType: 'image/png', size: 12, base64: 'SECRETBYTES' }),
    ).toEqual({ id: 'x', name: 'a.png', mediaType: 'image/png', size: 12 })
  })
})

describe('fileToBase64', () => {
  it('reads a Blob as raw base64 (data: prefix stripped)', async () => {
    const blob = new File(['ABC'], 'a.png', { type: 'image/png' })
    expect(await fileToBase64(blob)).toBe('QUJD') // base64('ABC')
  })
})
