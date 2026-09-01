/**
 * The browser's half of the per-conversation guardrail.
 *
 * The hard boundary is the server's and is tested there
 * (`backend/tests/api/v1/conversations/test_context_gate.py`). What is testable HERE is the
 * warning: that it appears at the administrator's threshold and not one token before, that it
 * respects an override, and that the estimate counts what the server counts.
 */
import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest'

import {
  CHARS_PER_TOKEN,
  DEFAULT_CONTEXT_HARD,
  DEFAULT_CONTEXT_SOFT,
  NOMINAL_BINARY_TOKENS,
  SYSTEM_PROMPT_RESERVE,
  contextState,
  estimateConversationTokens,
  getContextLimits,
} from '../contextLimits'
import type { ChatMessage } from '../messageTypes'

vi.mock('../auth', async () => {
  const actual = await vi.importActual<typeof import('../auth')>('../auth')
  return { ...actual, getStoredUser: vi.fn(() => null) }
})

const { getStoredUser } = await import('../auth')
const mockedUser = vi.mocked(getStoredUser)

/**
 * A stored profile carrying (or not carrying) limits.
 *
 * Cast through `unknown`: `getContextLimits` reads exactly one field off the profile, and the
 * rest of `UserProfile` is irrelevant to every assertion here. Spelling out six unrelated
 * fields per case would make the tests read as if those fields mattered.
 */
function signedInWith(limits: Record<string, unknown> | undefined) {
  const profile = limits === undefined ? {} : { limits }
  mockedUser.mockReturnValue(profile as unknown as ReturnType<typeof getStoredUser>)
}

function prose(chars: number): ChatMessage {
  return { id: 'm', role: 'user', parts: [{ type: 'text', text: 'x'.repeat(chars) }] }
}

beforeEach(() => signedInWith(undefined))
afterEach(() => vi.clearAllMocks())

describe('getContextLimits', () => {
  it('falls back to the defaults for a session that carries no limits', () => {
    expect(getContextLimits()).toEqual({ soft: DEFAULT_CONTEXT_SOFT, hard: DEFAULT_CONTEXT_HARD })
  })

  it('uses the administrator’s override when the profile carries one', () => {
    signedInWith({ contextSoftLimit: 40_000, contextHardLimit: 50_000 })
    expect(getContextLimits()).toEqual({ soft: 40_000, hard: 50_000 })
  })

  it('clamps a soft threshold that is not below the hard one', () => {
    // A warning that first fires AT the wall arrives in the same breath as the refusal, which
    // is the one moment it is no use to anybody.
    signedInWith({ contextSoftLimit: 90_000, contextHardLimit: 50_000 })
    expect(getContextLimits()).toEqual({ soft: 49_999, hard: 50_000 })
  })

  it.each([0, -1, 1.5, '80000', null])('ignores a non-positive-integer override (%s)', (bad) => {
    signedInWith({ contextSoftLimit: bad, contextHardLimit: bad })
    expect(getContextLimits()).toEqual({ soft: DEFAULT_CONTEXT_SOFT, hard: DEFAULT_CONTEXT_HARD })
  })
})

describe('estimateConversationTokens', () => {
  it('is the reserve on an empty conversation, not zero', () => {
    // The system prompt this cannot see is still going to be there.
    expect(estimateConversationTokens([])).toBe(SYSTEM_PROMPT_RESERVE)
  })

  it('counts prose at four characters to the token, the way the server does', () => {
    expect(estimateConversationTokens([prose(4_000)])).toBe(SYSTEM_PROMPT_RESERVE + 1_000)
    expect(CHARS_PER_TOKEN).toBe(4)
  })

  it('charges EVERY attachment on EVERY turn, not just the newest', () => {
    // ★ The change from the estimator this replaces. That one charged image/PDF parts only in
    // the last message, because the old relay sent binaries only for the newest turn. The turn
    // engine rehydrates every stored attachment on every turn — Foundry has no Files API — so
    // charging once under-counts a picture-heavy chat by however many pictures it holds.
    const withPhotos: ChatMessage[] = [
      {
        id: 'a',
        role: 'user',
        parts: [
          {
            type: 'file',
            kind: 'image',
            attachmentId: 'att_1',
            key: 'k',
            name: 'one.png',
            mediaType: 'image/png',
            size: 10,
          },
        ],
      },
      { id: 'b', role: 'assistant', parts: [{ type: 'text', text: 'nice' }] },
      {
        id: 'c',
        role: 'user',
        parts: [
          {
            type: 'file',
            kind: 'image',
            attachmentId: 'att_2',
            key: 'k',
            name: 'two.png',
            mediaType: 'image/png',
            size: 10,
          },
        ],
      },
    ]
    expect(estimateConversationTokens(withPhotos)).toBe(
      SYSTEM_PROMPT_RESERVE + NOMINAL_BINARY_TOKENS * 2 + 1,
    )
  })

  it('counts an office attachment by its extracted text, not the flat nominal', () => {
    // The extraction is sticky prose on the wire. A 200 KB spreadsheet is ~50k tokens, and
    // billing it at 1,600 would let a conversation nobody can send look comfortably short.
    const sheet: ChatMessage = {
      id: 'x',
      role: 'user',
      parts: [
        {
          type: 'file',
          kind: 'office',
          format: 'excel',
          attachmentId: 'att_3',
          key: 'k',
          name: 'rota.xlsx',
          mediaType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
          size: 10,
          text: 'c'.repeat(200_000),
          truncated: false,
        },
      ],
    }
    expect(estimateConversationTokens([sheet])).toBe(SYSTEM_PROMPT_RESERVE + 50_000)
  })

  it('ignores chrome the model is never sent', () => {
    // Plan cards, steps and build banners are drawn by the browser; the server's own
    // measurement never sees them either, so counting them here would make the two disagree.
    const chrome: ChatMessage = {
      id: 'y',
      role: 'assistant',
      parts: [
        { type: 'build_in_progress', sessionId: 's' },
        { type: 'build', sessionId: 's', status: 'ended', reason: null, previewUrl: null },
      ],
    }
    expect(estimateConversationTokens([chrome])).toBe(SYSTEM_PROMPT_RESERVE)
  })
})

describe('contextState', () => {
  it('is silent below the threshold', () => {
    const state = contextState([prose(4_000)])
    expect(state.gettingLong).toBe(false)
    expect(state.message).toBeNull()
  })

  it('fires AT the threshold and not one token before', () => {
    // ★ The boundary assertion. `>=` vs `>` is a one-character mutation and this is what
    // catches it; so is a threshold read from the wrong field.
    signedInWith({ contextSoftLimit: 10_000, contextHardLimit: 20_000 })
    const justUnder = (10_000 - SYSTEM_PROMPT_RESERVE - 1) * CHARS_PER_TOKEN
    expect(contextState([prose(justUnder)]).gettingLong).toBe(false)
    expect(contextState([prose(justUnder + CHARS_PER_TOKEN)]).gettingLong).toBe(true)
  })

  it('follows the administrator’s warn threshold rather than the default', () => {
    // The same conversation, two users: silent under the default, warned under a lowered one.
    const conversation = [prose(40_000)] // 10k tokens + reserve
    expect(contextState(conversation).gettingLong).toBe(false)
    signedInWith({ contextSoftLimit: 12_000, contextHardLimit: 20_000 })
    expect(contextState(conversation).gettingLong).toBe(true)
  })

  it('says what to do and that the work survives it', () => {
    // The only reason a citizen hesitates to start a new chat is the fear that the app goes
    // with the conversation. Without the second half the first half reads as a threat.
    signedInWith({ contextSoftLimit: 1, contextHardLimit: 20_000 })
    const message = contextState([prose(40)]).message ?? ''
    expect(message).toContain('new chat')
    expect(message).toContain('stays exactly as it is')
    // And it names no number: "150,000 of 200,000" is not something anyone can act on.
    expect(message).not.toMatch(/\d/)
  })
})
