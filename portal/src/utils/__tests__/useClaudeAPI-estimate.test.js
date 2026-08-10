import { describe, it, expect } from 'vitest'
import {
  estimateConversationTokens,
  CONTEXT_SOFT_LIMIT,
  CONTEXT_HARD_LIMIT,
} from '../../hooks/useClaudeAPI'

// U7: `estimateTokens` / `truncateMessages` died with the browser-side transcript — the
// server owns history now, so the only client-side estimator left is the conversation
// guardrail below (warn/block UI, advisory only).

describe('estimateConversationTokens', () => {
  // Helpers to build parts-model messages (the in-memory shape the pages hold).
  const textPart = (text) => ({ type: 'text', text })
  const filePart = (attachmentId, mediaType = 'image/png') => ({ type: 'file', attachmentId, kind: 'image', mediaType })

  it("counts message text + system prompt + the newest turn's file parts", () => {
    const messages = [
      { role: 'user', parts: [textPart('a'.repeat(400))] }, // 100 tokens
      { role: 'assistant', parts: [textPart('b'.repeat(400))] }, // 100 tokens
      { role: 'user', parts: [textPart('c'.repeat(400)), filePart('1'), filePart('2')] }, // 100 + 2 files
    ]
    const system = 's'.repeat(2000) // 500 tokens
    const est = estimateConversationTokens(messages, system)
    // 300 (text) + 500 (system) + 2 * 1600 (nominal per last-turn file part) = 4000
    expect(est).toBe(4000)
  })

  it('does not count file parts on non-final turns (they send text-only)', () => {
    const withOldAttach = [
      { role: 'user', parts: [filePart('1'), filePart('2'), filePart('3')] },
      { role: 'assistant', parts: [textPart('')] },
    ]
    // only text (0) + system (0) + last turn has no file parts → 0
    expect(estimateConversationTokens(withOldAttach, '')).toBe(0)
  })

  it('is empty-safe and exposes ordered thresholds', () => {
    expect(estimateConversationTokens([], '')).toBe(0)
    expect(estimateConversationTokens(null, '')).toBe(0)
    expect(CONTEXT_SOFT_LIMIT).toBeLessThan(CONTEXT_HARD_LIMIT)
  })

  it('counts an inline text-attachment part by its content length on EVERY turn (sticky, not a flat 1600)', () => {
    // An inline csv/txt is a text part whose `text` holds the file content.
    const csv = 'x'.repeat(200 * 1024) // ~51,200 tokens
    const inlineText = { type: 'text', text: csv, attachment: { attachmentId: 't', name: 'd.csv', mediaType: 'text/csv', size: 200 * 1024 } }
    const messages = [
      { role: 'user', parts: [inlineText] }, // old turn — still counted (sticky)
      { role: 'assistant', parts: [textPart('')] },
      { role: 'user', parts: [textPart('')] }, // newest, no attachments
    ]
    const est = estimateConversationTokens(messages, '')
    expect(est).toBe(Math.ceil((200 * 1024) / 4)) // 51,200 — counted though it's not the newest turn
  })

  it('counts an office file part by its extracted-text length on EVERY turn (sticky, not a flat 1600)', () => {
    const md = 'm'.repeat(80 * 1024) // ~20,480 tokens of extracted Markdown
    const officePart = { type: 'file', kind: 'office', format: 'excel', attachmentId: 'o', mediaType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', text: md }
    const messages = [
      { role: 'user', parts: [officePart] }, // old turn — still counted (sticky text)
      { role: 'assistant', parts: [textPart('')] },
      { role: 'user', parts: [textPart('')] }, // newest, no attachments
    ]
    expect(estimateConversationTokens(messages, '')).toBe(Math.ceil((80 * 1024) / 4)) // 20,480, not 1600
  })

  it('still counts an image/PDF file part as a flat nominal on the newest turn only', () => {
    // File part on the newest turn → one flat nominal (1600), NOT size-based.
    expect(estimateConversationTokens([{ role: 'user', parts: [filePart('i')] }], '')).toBe(1600)
    // Same file part on a non-newest turn → not re-sent, so not counted.
    const older = [
      { role: 'user', parts: [filePart('i')] },
      { role: 'assistant', parts: [textPart('')] },
    ]
    expect(estimateConversationTokens(older, '')).toBe(0)
  })
})

describe('estimateConversationTokens — deck (.pptx) parts', () => {
  const PPTX_TYPE = 'application/vnd.openxmlformats-officedocument.presentationml.presentation'
  const textPart = (text) => ({ type: 'text', text })
  const deckPart = (extra = {}) => ({
    type: 'file', kind: 'deck', attachmentId: 'd1', mediaType: PPTX_TYPE,
    name: 'q3.pptx', size: 1, pdfFileId: 'file_d1', pageCount: 10, ...extra,
  })

  it('adds a heavy per-page cost for a deck (far more than a nominal binary)', () => {
    // 10 pages — counted on its (first) turn even though it carries no `text`.
    const est = estimateConversationTokens([{ role: 'user', parts: [deckPart({ pageCount: 10 })] }], '')
    expect(est).toBe(10 * 3000)
    expect(est).toBeGreaterThan(1600) // not the flat per-file nominal
  })

  it('counts a sticky deck mostly ONCE (first turn full, follow-ups ~0.1x), not full every turn', () => {
    const dp = deckPart({ pageCount: 20 }) // 60,000 full
    const oneTurn = estimateConversationTokens([{ role: 'user', parts: [dp, textPart('a')] }], '')
    const threeTurns = estimateConversationTokens(
      [
        { role: 'user', parts: [dp, textPart('a')] },
        { role: 'assistant', parts: [textPart('ok')] },
        { role: 'user', parts: [dp, textPart('b')] }, // SAME sticky deck again
      ],
      '',
    )
    // The repeated deck adds only ~0.1x on its second appearance — nowhere near 2x.
    expect(threeTurns).toBeLessThan(oneTurn * 1.3)
    expect(threeTurns).toBeGreaterThan(oneTurn) // the cached re-read still adds a little
  })

  it('falls back to 1 page when pageCount is missing/invalid', () => {
    expect(estimateConversationTokens([{ role: 'user', parts: [deckPart({ pageCount: undefined })] }], '')).toBe(3000)
    expect(estimateConversationTokens([{ role: 'user', parts: [deckPart({ pageCount: 0 })] }], '')).toBe(3000)
  })

  it('a large deck pushes the estimate past the soft warn threshold', () => {
    // 60 pages * 3000 = 180,000 > CONTEXT_SOFT_LIMIT (150k) → the high-usage warning fires.
    const est = estimateConversationTokens([{ role: 'user', parts: [deckPart({ pageCount: 60 })] }], '')
    expect(est).toBeGreaterThan(CONTEXT_SOFT_LIMIT)
  })
})
