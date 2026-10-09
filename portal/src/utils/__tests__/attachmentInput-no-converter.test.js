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

  it('still refuses the legacy binary formats, naming the file and the format to re-save it as', () => {
    // Nothing here opens a pre-2007 file, so they stay refused. The way forward is followable
    // because the OOXML twin is accepted.
    for (const [f, message] of [
      [
        file('old.ppt', 'application/vnd.ms-powerpoint'),
        '"old.ppt" is an older PowerPoint format. Open it in PowerPoint, re-save it as .pptx, and attach it again.',
      ],
      [
        file('legacy.doc', 'application/msword'),
        '"legacy.doc" is an older Word format. Open it in Word, re-save it as .docx, and attach it again.',
      ],
      [
        file('sheet.xls', 'application/vnd.ms-excel'),
        '"sheet.xls" is an older Excel format. Open it in Excel, re-save it as .xlsx, and attach it again.',
      ],
      [
        file('deck.ppt', ''),
        '"deck.ppt" is an older PowerPoint format. Open it in PowerPoint, re-save it as .pptx, and attach it again.',
      ],
    ]) {
      expect(validateAttachmentFiles([f], 0)).toEqual({ error: message })
    }
  })

  it('reads only the last extension, in any case', () => {
    expect(validateAttachmentFiles([file('REPORT.DOC', '')], 0)).toEqual({
      error: '"REPORT.DOC" is an older Word format. Open it in Word, re-save it as .docx, and attach it again.',
    })
    // `.doc` inside a longer name, or no real extension at all, is not a legacy file.
    expect(validateAttachmentFiles([file('report.doc.docx', '')], 0)).toEqual({ ok: true })
    for (const name of ['archive.doc.zip', 'doc', 'report.doc.']) {
      expect(validateAttachmentFiles([file(name, '')], 0).error, name).toMatch(/isn't supported/)
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
