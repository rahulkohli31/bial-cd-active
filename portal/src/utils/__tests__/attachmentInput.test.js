import { describe, it, expect } from 'vitest'
import {
  validateAttachmentFiles,
  pixelLimitRefusal,
  chatFileBytes,
  chatTotalRefusal,
  validateConversationAttachmentCap,
  resolveMediaType,
  fileToBase64,
  ACCEPT_ATTR,
  MAX_FILES_PER_MESSAGE,
  MAX_ATTACHMENTS_PER_CONVERSATION,
  MODEL_LANE_MEDIA_TYPES,
  CODE_LANE_MEDIA_TYPES,
  ALLOWED_MEDIA_TYPES,
  GENERIC_ATTACHMENT_LANES_SENTENCE,
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

  it('rejects exceeding the per-message file cap', () => {
    const res = validateAttachmentFiles([file('a.png', 'image/png')], MAX_FILES_PER_MESSAGE)
    expect(res.error).toMatch(new RegExp(`at most ${MAX_FILES_PER_MESSAGE} files`))
  })

  it('accepts the code-lane formats as ordinary uploads', () => {
    // THE INLINE-TEXT CAPS ARE GONE WITH THEIR LANE. A CSV used to be read in the browser
    // and inlined into the prompt, so it carried its own 256 KB per-file and 512 KB
    // per-conversation budgets. Every attachment is an uploaded file now, governed by its lane's
    // size limit — which is also what lets a chip be rebuilt on reload for every format.
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

  it.each([
    ['photo.png', 'image/png', 7],
    ['photo.jpg', 'image/jpeg', 7],
    ['photo.gif', 'image/gif', 7],
    ['photo.webp', 'image/webp', 7],
    ['rows.csv', 'text/csv', 30],
    ['rows.tsv', 'text/tab-separated-values', 30],
    ['book.xlsx', XLSX, 30],
    ['doc.docx', DOCX, 30],
    ['deck.pptx', PPTX, 30],
  ])('accepts %s at its lane limit and refuses it one byte over, naming that limit', (name, type, mb) => {
    // Spelled numbers, on purpose: these are the limits the server enforces, and a backend test
    // holds the module's constants to the server's. Deriving them here would let both drift together.
    const limit = mb * 1024 * 1024
    expect(validateAttachmentFiles([file(name, type, limit)], 0)).toEqual({ ok: true })
    expect(validateAttachmentFiles([file(name, type, limit + 1)], 0)).toEqual({
      error: `"${name}" exceeds the ${mb} MB limit.`,
    })
  })


})

describe("a chat's room for pictures and PDFs", () => {
  const MB = 1024 * 1024
  const wontFit = (name) =>
    `"${name}" won't fit in this chat — pictures and PDFs can add up to 20 MB. Attach a smaller file or start a new chat.`

  it('takes a PDF up to the whole 20 MB of an empty chat, and calls a byte more too large for any chat', () => {
    // Every picture and PDF is sent to the assistant again with each message, and it reads at
    // most 32 MB at once. One limit per chat is what keeps every later message under that.
    // Mutation receipt: give a PDF no limit of its own and the larger one is told to start a new chat.
    expect(validateAttachmentFiles([file('spec.pdf', 'application/pdf', 20 * MB)], 0, 0)).toEqual({ ok: true })
    expect(validateAttachmentFiles([file('spec.pdf', 'application/pdf', 20 * MB + 1)], 0, 0)).toEqual({
      error: '"spec.pdf" exceeds the 20 MB limit.',
    })
  })

  it('counts the pictures and PDFs the chat already holds', () => {
    expect(validateAttachmentFiles([file('photo.png', 'image/png', 5 * MB)], 0, 15 * MB)).toEqual({ ok: true })
    expect(validateAttachmentFiles([file('photo.png', 'image/png', 5 * MB + 1)], 0, 15 * MB)).toEqual({
      error: wontFit('photo.png'),
    })
  })

  it('adds up files picked together', () => {
    const pick = [file('a.pdf', 'application/pdf', 11 * MB), file('b.pdf', 'application/pdf', 11 * MB)]
    expect(validateAttachmentFiles(pick, 0, 0)).toEqual({ error: wontFit('b.pdf') })
  })

  it('never counts spreadsheets, documents or decks, which the assistant does not receive', () => {
    expect(validateAttachmentFiles([file('book.xlsx', XLSX, 30 * MB)], 0, 20 * MB)).toEqual({ ok: true })
  })
})

describe('chatFileBytes', () => {
  it("adds up the pictures and PDFs in the chat's messages, and nothing else", () => {
    const part = (mediaType, size) => ({ type: 'file', kind: 'file', attachmentId: 'a', name: 'f', mediaType, size })
    const messages = [
      { parts: [part('image/png', 100), { type: 'text', text: 'look' }] },
      { parts: [part('application/pdf', 1000), part(XLSX, 5000)] },
      // A file whose row is gone has no size, and holds no room.
      { parts: [{ type: 'file', kind: 'image', attachmentId: 'b', name: '', mediaType: 'image/png' }] },
    ]
    expect(chatFileBytes(messages)).toBe(1100)
  })

  it('leaves out the files it is told to, which a send still in flight holds twice', () => {
    const messages = [{ parts: [{ type: 'file', kind: 'document', attachmentId: 'a1', name: 'f', mediaType: 'application/pdf', size: 500 }] }]
    expect(chatFileBytes(messages, new Set(['a1']))).toBe(0)
  })
})

describe('chatTotalRefusal', () => {
  const MB = 1024 * 1024
  const held = (size) => [{ parts: [{ type: 'file', kind: 'document', attachmentId: 'a0', name: 'big.pdf', mediaType: 'application/pdf', size }] }]

  it("refuses a send that would take the chat's pictures and PDFs past 20 MB, naming the file", () => {
    expect(chatTotalRefusal(held(19 * MB), [{ name: 'more.pdf', mediaType: 'application/pdf', size: 2 * MB }])).toBe(
      '"more.pdf" won\'t fit in this chat — pictures and PDFs can add up to 20 MB. Attach a smaller file or start a new chat.',
    )
  })

  it('lets through a send that fits, and never counts a spreadsheet', () => {
    expect(chatTotalRefusal(held(19 * MB), [{ name: 'a.pdf', mediaType: 'application/pdf', size: MB }])).toBeNull()
    expect(chatTotalRefusal(held(20 * MB), [{ name: 'b.xlsx', mediaType: XLSX, size: 30 * MB }])).toBeNull()
  })
})

describe('pixelLimitRefusal', () => {
  /** The first bytes of a PNG this many pixels wide and tall — all the size check reads. */
  const pngHeader = (width, height) => {
    const bytes = new Uint8Array(33)
    bytes.set([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0, 0, 0, 13, 0x49, 0x48, 0x44, 0x52])
    const view = new DataView(bytes.buffer)
    view.setUint32(16, width)
    view.setUint32(20, height)
    return bytes
  }
  const png = (name, width, height, type = 'image/png') => new File([pngHeader(width, height)], name, { type })

  it('refuses a picture wider or taller than 8,000 pixels, saying its size and what to do', async () => {
    expect(await pixelLimitRefusal(png('logo.png', 8001, 300))).toBe(
      '"logo.png" is 8,001 × 300 pixels. Resize it to 8,000 pixels or less on each side.',
    )
    expect(await pixelLimitRefusal(png('scan.png', 300, 8001))).toMatch(/300 × 8,001 pixels/)
  })

  it('finds a JPEG\'s size behind the EXIF segment a phone writes first', async () => {
    const exif = new Uint8Array(4 + 4096)
    exif.set([0xff, 0xe1, 0x10, 0x02])
    const sof = new Uint8Array([0xff, 0xc0, 0x00, 0x11, 0x08, 0x01, 0x2c, 0x1f, 0x41, 0x03, 1, 0x22, 0, 2, 0x11, 1, 3, 0x11, 1])
    const jpeg = new File([new Uint8Array([0xff, 0xd8]), exif, sof], 'photo.jpg', { type: 'image/jpeg' })
    expect(await pixelLimitRefusal(jpeg)).toBe(
      '"photo.jpg" is 8,001 × 300 pixels. Resize it to 8,000 pixels or less on each side.',
    )
  })

  it("finds a JPEG's size behind a megabyte of metadata, which design tools write", async () => {
    // Mutation receipt: drop the segment walk and this picture is let through.
    const segment = new Uint8Array(2 + 65535)
    segment.set([0xff, 0xe2, 0xff, 0xff])
    const sof = new Uint8Array([0xff, 0xc0, 0x00, 0x11, 0x08, 0x01, 0x2c, 0x1f, 0x41, 0x03, 1, 0x22, 0, 2, 0x11, 1, 3, 0x11, 1])
    const parts = [new Uint8Array([0xff, 0xd8]), ...Array(17).fill(segment), sof]
    expect(await pixelLimitRefusal(new File(parts, 'poster.jpg', { type: 'image/jpeg' }))).toBe(
      '"poster.jpg" is 8,001 × 300 pixels. Resize it to 8,000 pixels or less on each side.',
    )
  })

  it('gives up quickly on a JPEG whose header is damaged', async () => {
    // Mutation receipt: hand `imageSize` the first megabyte and this takes tens of seconds.
    const damaged = new File([new Uint8Array([0xff, 0xd8]), new Uint8Array(1024 * 1024)], 'damaged.jpg', {
      type: 'image/jpeg',
    })
    const started = performance.now()
    expect(await pixelLimitRefusal(damaged)).toBeNull()
    expect(performance.now() - started).toBeLessThan(5000)
  })

  it('takes a picture of exactly 8,000 pixels', async () => {
    expect(await pixelLimitRefusal(png('logo.png', 8000, 8000))).toBeNull()
  })

  it('never reads a PDF or a spreadsheet', async () => {
    expect(await pixelLimitRefusal(png('spec.pdf', 9000, 9000, 'application/pdf'))).toBeNull()
  })

  it('lets a picture through when its header cannot be read, and the server says what is wrong', async () => {
    expect(await pixelLimitRefusal(new File(['not a picture'], 'broken.png', { type: 'image/png' }))).toBeNull()
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

// WHAT MUST STAY UNREACHABLE — anything needing a converter this platform cannot host — is
// asserted in `attachmentInput-no-converter.test.js`, against the real module, not here.

describe('ACCEPT_ATTR', () => {
  it('offers both lanes and their extension tokens, and not plain text', () => {
    expect(ACCEPT_ATTR).toContain('image/png')
    expect(ACCEPT_ATTR).toContain('application/pdf')
    expect(ACCEPT_ATTR).toContain('text/csv')
    expect(ACCEPT_ATTR).toContain(XLSX)
    // Extension tokens matter more here than the MIME types: the OS picker shows a file only if
    // one of the two matches, and it reports these MIMEs inconsistently or not at all.
    for (const ext of ['.csv', '.tsv', '.xlsx', '.docx', '.pptx']) {
      expect(ACCEPT_ATTR).toContain(ext)
    }
    // A withdrawal: absent, so the picker never offers it.
    expect(ACCEPT_ATTR).not.toContain('.txt')
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


describe('MODEL_LANE_MEDIA_TYPES', () => {
  it('is exactly the five formats the model reads itself, and nothing the code lane reads', () => {
    // The generic conversation narrows to this exact set — a drift here is a drift in what a
    // generic chat accepts, so the two directions are both worth asserting.
    expect(MODEL_LANE_MEDIA_TYPES).toEqual([
      'image/png', 'image/jpeg', 'image/gif', 'image/webp', 'application/pdf',
    ])
    for (const type of MODEL_LANE_MEDIA_TYPES) {
      expect(CODE_LANE_MEDIA_TYPES).not.toContain(type)
    }
  })

  it('is the first half of ALLOWED_MEDIA_TYPES, with the code lane completing it', () => {
    expect(ALLOWED_MEDIA_TYPES).toEqual([...MODEL_LANE_MEDIA_TYPES, ...CODE_LANE_MEDIA_TYPES])
  })
})

describe('GENERIC_ATTACHMENT_LANES_SENTENCE', () => {
  it('names what a generic chat accepts and never offers to open a file with code', () => {
    expect(GENERIC_ATTACHMENT_LANES_SENTENCE).toMatch(/picture|PDF/i)
    expect(GENERIC_ATTACHMENT_LANES_SENTENCE).not.toMatch(/code/i)
  })

  it('is a real sentence, not an empty string every negative assertion would pass against', () => {
    expect(GENERIC_ATTACHMENT_LANES_SENTENCE.length).toBeGreaterThan(40)
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
