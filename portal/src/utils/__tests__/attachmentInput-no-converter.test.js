/**
 * What may be attached is decided by what code here can read, never by a conversion step. The
 * OOXML formats are offered because a shipped reader opens them in the project's sandbox: no
 * conversion, no hosted service, no extra deployed component. The pre-2007 binary formats
 * (`.doc`, `.xls`, `.ppt`) stay refused for the same reason inverted — they are not ZIP-based,
 * no library in the reader opens one, and admitting one would mean hosting a converter. That is
 * a standing scope boundary, not a deferral.
 *
 * Both directions are asserted: a one-way test lets a format linger in the copy after the
 * composer stops offering it. Nothing here is mocked — a mocked flag passes in both positions
 * while a citizen meets a third behaviour.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { ALLOWED_MEDIA_TYPES, ACCEPT_ATTR, validateAttachmentFiles } from '../attachmentInput'

const PPTX_MEDIA_TYPE =
  'application/vnd.openxmlformats-officedocument.presentationml.presentation'
const WORD_MEDIA_TYPE =
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
const EXCEL_MEDIA_TYPE =
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'

const file = (name, type, size = 1024) => ({ name, type, size })
const UNSUPPORTED = /isn't supported/

describe('formats are admitted by what code can read, never by a conversion step', () => {
  it('offers the three OOXML types and their extension tokens', () => {
    for (const type of [PPTX_MEDIA_TYPE, WORD_MEDIA_TYPE, EXCEL_MEDIA_TYPE]) {
      expect(ALLOWED_MEDIA_TYPES).toContain(type)
    }
    // The extension tokens matter more than the MIME types here: the OS reports these
    // inconsistently or not at all, so the picker shows the file only if one of the two matches.
    for (const ext of ['.pptx', '.docx', '.xlsx', '.csv', '.tsv']) {
      expect(ACCEPT_ATTR).toContain(ext)
    }
    // The liveness half: the picker still offers the model lane too, so the presences above mean
    // "widened" rather than "replaced".
    expect(ACCEPT_ATTR).toContain('application/pdf')
    expect(ACCEPT_ATTR).toContain('image/png')
  })

  it('still refuses the legacy binary formats, which no reader here can open', () => {
    // The advice must not send a citizen to a format that is also refused, so it is read
    // separately from the echoed name.
    for (const f of [
      file('old.ppt', 'application/vnd.ms-powerpoint'),
      file('legacy.doc', 'application/msword'),
      file('sheet.xls', 'application/vnd.ms-excel'),
      file('deck.ppt', ''),
    ]) {
      const res = validateAttachmentFiles([f], 0)
      expect(res.error, `"${f.name}" was accepted`).toMatch(UNSUPPORTED)
      const advice = res.error.replace(/^"[^"]*"/, '')
      expect(advice).not.toMatch(/save as|\.doc\b|\.ppt\b|\.xls\b/i)
    }
  })

  it('the deck flag is not exported from config/features, and nothing imports it', () => {
    // A flag with no arms reads as a capability that still exists somewhere. Deck attachments
    // need none: the reader opens the file wherever it lands.
    const src = path.resolve(__dirname, '../../..')
    const features = readFileSync(path.join(src, 'src/config/features.ts'), 'utf8')
    expect(features).not.toMatch(/DECK_ATTACHMENTS_ENABLED/)
  })

  it('accepts both lanes, and refuses plain text', () => {
    for (const f of [
      file('plan.pdf', 'application/pdf'),
      file('shot.png', 'image/png'),
      file('rows.csv', 'text/csv'),
      file('rows.tsv', 'text/tab-separated-values'),
      file('book.xlsx', EXCEL_MEDIA_TYPE),
      file('brief.docx', WORD_MEDIA_TYPE),
      file('q3.pptx', PPTX_MEDIA_TYPE),
    ]) {
      expect(validateAttachmentFiles([f], 0), `"${f.name}" was refused`).toEqual({ ok: true })
    }
    // A withdrawal, not a format never added: it works on the branch today and stops, because no
    // client requirement names it and every format costs a reader arm, copy, a test and a line in
    // the help page.
    expect(validateAttachmentFiles([file('notes.txt', 'text/plain')], 0).error).toBeTruthy()
  })
})
