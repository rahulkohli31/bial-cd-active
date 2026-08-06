import { describe, it, expect } from 'vitest'
import { buildPromptFromHistory, relativeTime, deriveTitle, newConversation } from '../chatHistory'

describe('buildPromptFromHistory (parts model)', () => {
  it('renders an attachment turn as its prose, not [object Object]', () => {
    const prompt = buildPromptFromHistory([
      {
        role: 'user',
        parts: [
          { type: 'file', attachmentId: 'a1', kind: 'image', mediaType: 'image/png', name: 's.png' },
          { type: 'text', text: 'analyze this screenshot' },
        ],
      },
      { role: 'assistant', parts: [{ type: 'text', text: 'Sure, here is the plan.' }] },
    ])
    expect(prompt).toContain('analyze this screenshot')
    expect(prompt).not.toContain('[object Object]')
  })

  it('uses the first user message as the goal', () => {
    const prompt = buildPromptFromHistory([
      { role: 'user', parts: [{ type: 'text', text: 'build a gate tracker' }] },
      { role: 'assistant', parts: [{ type: 'text', text: 'ok' }] },
    ])
    expect(prompt).toContain("User's goal: build a gate tracker")
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
