import { describe, it, expect } from 'vitest'
import * as chatHistory from '../chatHistory'
import { relativeTime, deriveTitle, newConversation } from '../chatHistory'

// U27: `buildPromptFromHistory` wrapped a planning transcript into the prompt for the old
// client-driven single-file build. The open-sandbox orchestrator builds from the conversation
// it already owns server-side, so the client never assembles this prompt itself any more — a
// zero-reference export. Inert guard against it silently returning, paired with a liveness
// check (same shape as the sibling in `buildSystemPrompt.test.js`) so the guard cannot
// false-green on a broken import.
describe('buildPromptFromHistory is retired (U27)', () => {
  it('no longer exports buildPromptFromHistory, while the rest of the module still works', () => {
    expect(chatHistory.buildPromptFromHistory).toBeUndefined()
    const a = newConversation()
    const b = newConversation()
    expect(a).not.toBe(b)
    expect(a).toMatch(/^[0-9a-f-]{36}$/i)
    expect(deriveTitle('  trimmed  ')).toBe('trimmed')
  })
})

describe('newConversation', () => {
  it('mints a client UUID synchronously (no network, distinct each call)', () => {
    const a = newConversation()
    const b = newConversation()
    expect(typeof a).toBe('string')
    expect(a).not.toBe(b)
    expect(a).toMatch(/^[0-9a-f-]{36}$/i) // uuid shape
  })
})

describe('deriveTitle', () => {
  it('truncates to 40 chars with an ellipsis', () => {
    expect(deriveTitle('short')).toBe('short')
    expect(deriveTitle('x'.repeat(50))).toBe('x'.repeat(40) + '…')
    expect(deriveTitle('  trimmed  ')).toBe('trimmed')
  })
})

describe('relativeTime', () => {
  it('formats recent timestamps', () => {
    expect(relativeTime(new Date().toISOString())).toBe('just now')
    expect(relativeTime(new Date(Date.now() - 5 * 60000).toISOString())).toBe('5m ago')
    expect(relativeTime(new Date(Date.now() - 3 * 3600000).toISOString())).toBe('3h ago')
  })
})
