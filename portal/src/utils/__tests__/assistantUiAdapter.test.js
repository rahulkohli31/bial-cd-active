/**
 * Direct unit tests for createClaudeChatModelAdapter's run() — invoked as a
 * plain function (it has no React dependency), not through a full ChatPage
 * render. PR #35 comment 10's scenario (a stream error arriving after the
 * user has already navigated away) is a genuine race in real usage: React's
 * useLocalRuntime detaches its runtime core on unmount, which aborts the
 * in-flight run's abortSignal — and the adapter's OWN `if (abortSignal.aborted)
 * return` (a few lines above the code this comment is about) already
 * short-circuits before reaching onError in that common case. Driving the
 * exact ordering needed to bypass that guard through a real component
 * unmount is timing-dependent and not reliably reproducible in jsdom;
 * calling run() directly lets the test construct the precise state under
 * test (a genuine stream error, isConversationStillActive false, signal NOT
 * aborted) deterministically instead.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'

const h = vi.hoisted(() => ({ fetchClaudeStream: vi.fn() }))
vi.mock('../../hooks/useClaudeAPI', () => ({
  fetchClaudeStream: h.fetchClaudeStream,
  truncateMessages: (messages) => messages,
}))

import { createClaudeChatModelAdapter } from '../assistantUiAdapter'

function makeDeps(overrides = {}) {
  return {
    systemPrompt: 'sys',
    getChatId: () => 'chat-1',
    getProjectId: () => 'p1',
    appendMessage: vi.fn().mockResolvedValue({ ok: true }),
    deriveTitle: (t) => t,
    onAuthFailed: vi.fn(),
    isConversationStillActive: () => true,
    onError: vi.fn(),
    onConversationGone: vi.fn(),
    ctxLevelFull: () => false,
    initialNextSeq: 0,
    ...overrides,
  }
}

const userMessage = (text = 'hello') => [{ id: 'u1', role: 'user', content: [{ type: 'text', text }] }]

async function drain(asyncGen) {
  const yields = []
  for await (const r of asyncGen) yields.push(r)
  return yields
}

beforeEach(() => {
  h.fetchClaudeStream.mockReset()
})

describe('createClaudeChatModelAdapter — stream error banner scoping (PR #35 comment 10)', () => {
  it('does not call onError when the conversation is no longer active, but still marks the message errored', async () => {
    h.fetchClaudeStream.mockImplementation(() => Promise.reject(new Error('stream broke')))
    const deps = makeDeps({ isConversationStillActive: () => false })
    const adapter = createClaudeChatModelAdapter(deps)

    const yields = await drain(adapter.run({ messages: userMessage(), abortSignal: new AbortController().signal }))

    expect(deps.onError).not.toHaveBeenCalled()
    // The message is still correctly marked errored (PR #35 comment 2) — this
    // half is deliberately unconditional, unlike the banner.
    expect(yields.at(-1).status).toEqual({ type: 'incomplete', reason: 'error', error: 'stream broke' })
  })

  it('still calls onError when the conversation is the currently active one (sanity check — not swallowing every error)', async () => {
    h.fetchClaudeStream.mockImplementation(() => Promise.reject(new Error('stream broke')))
    const deps = makeDeps({ isConversationStillActive: () => true })
    const adapter = createClaudeChatModelAdapter(deps)

    await drain(adapter.run({ messages: userMessage(), abortSignal: new AbortController().signal }))

    expect(deps.onError).toHaveBeenCalledWith('stream broke')
  })

  it('AUTH_REFRESH_FAILED stays unguarded — a session-wide event, not chat-scoped', async () => {
    const authErr = new Error('session dead')
    authErr.code = 'AUTH_REFRESH_FAILED'
    h.fetchClaudeStream.mockImplementation(() => Promise.reject(authErr))
    const deps = makeDeps({ isConversationStillActive: () => false })
    const adapter = createClaudeChatModelAdapter(deps)

    await drain(adapter.run({ messages: userMessage(), abortSignal: new AbortController().signal }))

    expect(deps.onAuthFailed).toHaveBeenCalled()
    expect(deps.onError).not.toHaveBeenCalled() // this branch never reaches onError at all
  })
})
