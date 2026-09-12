import { describe, it, expect } from 'vitest'
import {
  validateAttachmentFiles,
  validateConversationAttachmentCap,
  resolveMediaType,
  fileToBase64,
  ACCEPT_ATTR,
  MAX_FILE_SIZE,
  MAX_FILE_SIZE_MB,
  MAX_FILES_PER_MESSAGE,
  MAX_ATTACHMENTS_PER_CONVERSATION,
} from '../attachmentInput'

const XLSX = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
const DOCX = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
const PPTX = 'application/vnd.openxmlformats-officedocument.presentationml.presentation'


// validateAttachmentFiles only reads name/type/size, so plain objects suffice
// (and let us set an arbitrary size without allocating megabytes).
const file = (name, type, size = 1024) => ({ name, type, size })

describe('validateAttachmentFiles', () => {




  it('rejects a genuinely unsupported type with a generic message', () => {
    const res = validateAttachmentFiles([file('clip.mp3', 'audio/mpeg')], 0)
    expect(res.error).toMatch(/isn't supported/)
  })

  it('rejects a file one byte over the cap, and names the cap it enforced', () => {
    // Asserted against the CONSTANT, never a spelled number: a refusal that says a figure the
    // code does not enforce is the drift this interpolation exists to prevent, and a hardcoded
    // "4 MB" here is what let the two disagree in the first place.
    expect(validateAttachmentFiles([file('huge.png', 'image/png', MAX_FILE_SIZE)], 0)).toEqual({ ok: true })
    const res = validateAttachmentFiles([file('huge.png', 'image/png', MAX_FILE_SIZE + 1)], 0)
    expect(res.error).toMatch(new RegExp(`${MAX_FILE_SIZE_MB} MB`))
  })

  it('rejects exceeding the per-message file cap', () => {
    const res = validateAttachmentFiles([file('a.png', 'image/png')], MAX_FILES_PER_MESSAGE)
    expect(res.error).toMatch(new RegExp(`at most ${MAX_FILES_PER_MESSAGE} files`))
  })

  it('accepts the code-lane formats as ordinary uploads', () => {
    // THE INLINE-TEXT CAPS ARE GONE WITH THEIR LANE. A CSV used to be read in the browser
    // and inlined into the prompt, so it carried its own 256 KB per-file and 512 KB
    // per-conversation budgets. Every attachment is an uploaded file now, governed by the one
    // per-file cap — which is also what lets a chip be rebuilt on reload for every format.
    expect(validateAttachmentFiles([file('rows.csv', 'text/csv')], 0)).toEqual({ ok: true })
    expect(validateAttachmentFiles([file('rows.tsv', 'text/tab-separated-values')], 0)).toEqual({ ok: true })
    expect(validateAttachmentFiles([file('book.xlsx', XLSX)], 0)).toEqual({ ok: true })
    expect(validateAttachmentFiles([file('doc.docx', DOCX)], 0)).toEqual({ ok: true })
    expect(validateAttachmentFiles([file('deck.pptx', PPTX)], 0)).toEqual({ ok: true })
  })

  it('refuses plain text, which is a withdrawal rather than a format never added', () => {
    // It works on the branch today and stops. The mechanism argument died with the inline lane —
    // under the routing rule a .txt is simply a file code reads, exactly like a .csv — so the
    // refusal rests on the surviving reason: no client requirement names it, and every format
    // costs a reader arm, refusal copy, a test and a line in the help page.
    expect(validateAttachmentFiles([file('notes.txt', 'text/plain')], 0).error).toBeTruthy()
  })

  it('accepts valid images and a PDF under the caps', () => {
    expect(validateAttachmentFiles([file('a.png', 'image/png')], 0)).toEqual({ ok: true })
    expect(validateAttachmentFiles([file('b.jpg', 'image/jpeg')], 0)).toEqual({ ok: true })
    expect(validateAttachmentFiles([file('c.pdf', 'application/pdf')], 0)).toEqual({ ok: true })
  })


  it('accepts an OS-mislabeled .csv (reported application/vnd.ms-excel or empty) via resolved type', () => {
    // Validation must run against the resolved type, not raw file.type — so a CSV
    // the OS labels as Excel (or leaves blank) is still accepted.
    expect(validateAttachmentFiles([file('data.csv', 'application/vnd.ms-excel')], 0)).toEqual({ ok: true })
    expect(validateAttachmentFiles([file('data.csv', '')], 0)).toEqual({ ok: true })
  })

  it('gives every format the SAME cap — a CSV, an image and a PDF are one rule', () => {
    // RE-POINTED TWICE. It first asserted a 256 KB cap on inlined text, which
    // existed because a CSV's BYTES rode in the prompt on every turn. Nothing is inlined now, so
    // it became "a CSV gets the ordinary file cap" — and the ordinary cap has since become one
    // number for all ten formats, so what is worth asserting is that no format has its own.
    //
    // A citizen should never have to know which of their files the platform considers expensive,
    // and the surest way to break that is for one format to keep a private number.
    for (const [name, type] of [
      ['big.csv', 'text/csv'],
      ['big.tsv', 'text/tab-separated-values'],
      ['photo.png', 'image/png'],
      ['spec.pdf', 'application/pdf'],
      ['book.xlsx', XLSX],
      ['doc.docx', DOCX],
      ['deck.pptx', PPTX],
    ]) {
      expect(validateAttachmentFiles([file(name, type, MAX_FILE_SIZE)], 0)).toEqual({ ok: true })
      expect(validateAttachmentFiles([file(name, type, MAX_FILE_SIZE + 1)], 0).error).toMatch(
        new RegExp(`${MAX_FILE_SIZE_MB} MB`),
      )
    }
  })


})

describe('resolveMediaType', () => {
  it('canonicalizes every code-lane extension, which the OS reports inconsistently', () => {
    // Browsers and operating systems report Office and delimited types inconsistently — a .csv
    // arrives as `text/csv`, `application/vnd.ms-excel`, or nothing at all, and a .xlsx often
    // with an empty MIME. The extension is the reliable signal, and every allowlist and cap
    // decision runs against the resolved type rather than raw `file.type`.
    expect(resolveMediaType(file('data.csv', 'application/vnd.ms-excel'))).toBe('text/csv')
    expect(resolveMediaType(file('data.CSV', ''))).toBe('text/csv')
    expect(resolveMediaType(file('data.tsv', ''))).toBe('text/tab-separated-values')
    expect(resolveMediaType(file('book.xlsx', ''))).toBe(XLSX)
    expect(resolveMediaType(file('doc.docx', ''))).toBe(DOCX)
    expect(resolveMediaType(file('deck.pptx', ''))).toBe(PPTX)
  })


  it('falls through to file.type for non-text extensions', () => {
    expect(resolveMediaType(file('a.png', 'image/png'))).toBe('image/png')
    expect(resolveMediaType(file('c.pdf', 'application/pdf'))).toBe('application/pdf')
  })
})

// `officeFormat` and the two deck suites are GONE, along with the media types they described.
// Their inertness is asserted in `attachmentInput-deck-disabled.test.js`, which stopped
// mocking the flag when the flag stopped existing — a removal's tests become guards, not gaps.

describe('ACCEPT_ATTR', () => {
  it('offers both lanes and their extension tokens, and nothing needing conversion', () => {
    expect(ACCEPT_ATTR).toContain('image/png')
    expect(ACCEPT_ATTR).toContain('application/pdf')
    expect(ACCEPT_ATTR).toContain('text/csv')
    expect(ACCEPT_ATTR).toContain(XLSX)
    // Extension tokens matter more here than the MIME types: the OS picker shows a file only if
    // one of the two matches, and it reports these MIMEs inconsistently or not at all.
    for (const ext of ['.csv', '.tsv', '.xlsx', '.docx', '.pptx']) {
      expect(ACCEPT_ATTR).toContain(ext)
    }
    // A withdrawal and two legacy formats: absent, so the picker never offers them.
    expect(ACCEPT_ATTR).not.toContain('.txt')
    expect(ACCEPT_ATTR).not.toContain('.doc,')
  })

  it('★ THE AGREEMENT: every extension this composer resolves, the picker also offers', () => {
    // ASSERTS THE RELATIONSHIP, NOT THE CONSTANT. `expect(ACCEPT_ATTR).toContain('.tab')` would
    // restate the list back to itself and catch nothing; what actually broke was the two halves
    // DISAGREEING — `resolveMediaType` mapped `.tab` to TSV and the server's TSV door admits that
    // suffix by name, while the picker's filter did not list it. So a citizen browsing for
    // `movements.tab` could not select the file they had just been told was supported.
    //
    // IT FAILED ONLY ON THE PICKER PATH, which is why it survived: dragging the same file in
    // never consults `ACCEPT_ATTR` and worked the whole time.
    for (const ext of ['.csv', '.tsv', '.tab', '.xlsx', '.docx', '.pptx']) {
      const resolved = resolveMediaType(file(`movements${ext}`, ''))
      expect(resolved).toBeTruthy()
      expect(validateAttachmentFiles([file(`movements${ext}`, '')], 0)).toEqual({ ok: true })
      expect(ACCEPT_ATTR.split(',')).toContain(ext)
    }
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
    expect(validateConversationAttachmentCap(MAX_ATTACHMENTS_PER_CONVERSATION - 1, 3).error).toBeTruthy()
  })

  it('uses wording distinct from the per-message and storage-full caps', () => {
    const res = validateConversationAttachmentCap(MAX_ATTACHMENTS_PER_CONVERSATION, 1)
    expect(res.error).toMatch(/this conversation/i)
    expect(res.error).not.toMatch(/per message/i)
  })
})


describe('fileToBase64', () => {
  it('reads a Blob as raw base64 (data: prefix stripped)', async () => {
    const blob = new File(['ABC'], 'a.png', { type: 'image/png' })
    expect(await fileToBase64(blob)).toBe('QUJD') // base64('ABC')
  })
})

// THE SHIPPED DEFAULT block is gone with the flag it pinned.
//
// It existed because a past change turned the deck feature off and nothing went red: both deck spec
// files mocked `config/features`, so between them they covered two hypothetical worlds and
// neither said which one we shipped. There is no flag to pin now — presentations, spreadsheets and
// documents are refused outright — and the inertness guard that replaces this lives in
// `attachmentInput-deck-disabled.test.js`, which stopped mocking when the flag stopped existing.
