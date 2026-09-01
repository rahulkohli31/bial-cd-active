/**
 * PRESENTATIONS ARE GONE — the inertness guard (L8).
 *
 * This file used to mock `DECK_ATTACHMENTS_ENABLED` to `false` and describe one of the flag's two
 * positions. There is no flag now: Plan D's U13 deleted it along with the arms it guarded and the
 * media type itself, so there is no "off world" left to pin. What remains worth asserting is that
 * the capability is UNREACHABLE — which is what a removal's tests become, rather than being
 * deleted with the code.
 *
 * IT STOPS MOCKING, and that matters. A mocked flag is exactly what let the old suite pass in both
 * positions while a citizen met a third behaviour; every assertion below runs against the real,
 * unmocked module. Its ENABLED sibling (`attachmentInput-deck.test.js`, 6 cases) is deleted rather
 * than converted: every one of its cases asserted a capability that can no longer exist, and a
 * test that mocks a deleted constant into existence proves nothing about the shipped product. That
 * deletion is a documented inertness conversion, and its count is named in the PR so the suite
 * arithmetic reconciles rather than being waved through.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import path from 'node:path'

import { validateAttachmentFiles, ACCEPT_ATTR, ALLOWED_MEDIA_TYPES } from '../attachmentInput'

const PPTX_MEDIA_TYPE =
  'application/vnd.openxmlformats-officedocument.presentationml.presentation'
const WORD_MEDIA_TYPE =
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
const EXCEL_MEDIA_TYPE =
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'

const file = (name, type, size = 1024) => ({ name, type, size })
const UNSUPPORTED = /isn't supported/

describe('formats that need a conversion step are unreachable (R46)', () => {
  it('offers no presentation, spreadsheet or document type in the allowlist or the OS picker', () => {
    for (const type of [PPTX_MEDIA_TYPE, WORD_MEDIA_TYPE, EXCEL_MEDIA_TYPE]) {
      expect(ALLOWED_MEDIA_TYPES).not.toContain(type)
      expect(ACCEPT_ATTR).not.toContain(type)
    }
    for (const ext of ['.pptx', '.docx', '.xlsx']) {
      expect(ACCEPT_ATTR).not.toContain(ext)
    }
    // The liveness half: the picker still offers what it should, so the four absences above
    // mean "narrowed" rather than "emptied".
    expect(ACCEPT_ATTR).toContain('application/pdf')
    expect(ACCEPT_ATTR).toContain('.csv')
  })

  it('refuses each of them at the moment of drop, with one honest message', () => {
    for (const f of [
      file('q3.pptx', PPTX_MEDIA_TYPE),
      file('brief.docx', WORD_MEDIA_TYPE),
      file('rota.xlsx', EXCEL_MEDIA_TYPE),
      file('old.ppt', 'application/vnd.ms-powerpoint'),
      file('legacy.doc', 'application/msword'),
      file('deck.ppt', ''),
    ]) {
      const res = validateAttachmentFiles([f], 0)
      expect(res.error, `"${f.name}" was accepted`).toMatch(UNSUPPORTED)
      // Advice only, not the echoed filename — the message quotes the citizen's own file back.
      const advice = res.error.replace(/^"[^"]*"/, '')
      expect(advice).not.toMatch(/save as|\.pptx|\.docx|\.xlsx/i)
      expect(advice).toMatch(/PDF/i)
    }
  })

  it('the deck flag is not exported from config/features, and nothing imports it', () => {
    // L8's fifth link. The constant, its thirty-line docblock and every branch reading it are
    // gone together — a flag left behind with no arms is read by the next person as a capability
    // that still exists somewhere.
    const src = path.resolve(__dirname, '../../..')
    const features = readFileSync(path.join(src, 'src/config/features.ts'), 'utf8')
    expect(features).not.toMatch(/DECK_ATTACHMENTS_ENABLED/)
  })

  it('still accepts everything that needs no conversion', () => {
    for (const f of [
      file('plan.pdf', 'application/pdf'),
      file('shot.png', 'image/png'),
      file('rows.csv', 'text/csv'),
      file('notes.txt', 'text/plain'),
    ]) {
      expect(validateAttachmentFiles([f], 0)).toEqual({ ok: true })
    }
  })
})
