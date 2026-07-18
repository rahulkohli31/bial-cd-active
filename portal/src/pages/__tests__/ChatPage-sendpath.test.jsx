/**
 * U10 streaming send-path guards (the plan's highest-risk async conversion +
 * execution note). Two behaviors that must hold regardless of timing:
 *   1. If persisting the USER turn fails, the send aborts BEFORE streaming — no
 *      orphan assistant turn, the failure surfaces as a toast.
 *   2. If the user navigates to another conversation mid-stream, the late
 *      assistant-turn write must NOT land on the previous conversation (guarded
 *      by the active-id ref) — "assistant write lands on the correct (or no)
 *      conversation."
 *
 * Since the assistant-ui migration, the send path runs through
 * assistantUiAdapter.js's ChatModelAdapter, driven by Thread's own composer
 * rather than a hand-rolled textarea+button — so these tests interact with
 * the real <Thread> composer (by its accessible placeholder) and mock
 * `fetchClaudeStream` (imported directly by the adapter) instead of the old
 * useClaudeAPI().sendMessage wrapper, which the adapter no longer goes
 * through for this path (it's still used, unrelated, by handleBuildApp's
 * one-off summarization call).
 *
 * The API + history store are mocked at the module boundary so we can hold the
 * stream open and assert exactly what gets persisted. The two-route MemoryRouter
 * mirrors App.jsx so navigate() preserves the ChatPage instance (refs survive).
 */
import { StrictMode } from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act, cleanup } from '@testing-library/react'
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom'

const h = vi.hoisted(() => ({
  sendMessage: vi.fn(),
  fetchClaudeStream: vi.fn(),
  loadHistory: vi.fn(),
  newConversation: vi.fn(),
  appendMessage: vi.fn(),
  getConversation: vi.fn(),
  deleteConversation: vi.fn(),
  listProjectConversations: vi.fn(),
  notifyUsageChanged: vi.fn(),
}))

vi.mock('../../hooks/useClaudeAPI', async () => {
  // truncateMessages is the REAL implementation (PR #35 comment 8's wiring test
  // needs its actual token-budget logic — already unit-tested in isolation by
  // useClaudeAPI-estimate.test.js, so this only verifies the adapter calls it).
  const actual = await vi.importActual('../../hooks/useClaudeAPI')
  return {
    useClaudeAPI: () => ({ sendMessage: h.sendMessage, error: null }),
    fetchClaudeStream: h.fetchClaudeStream,
    getContextLimits: () => ({ soft: 1e9, hard: 1e9 }),
    estimateConversationTokens: () => 0,
    truncateMessages: actual.truncateMessages,
  }
})
vi.mock('../../utils/chatHistory', () => ({
  loadHistory: h.loadHistory,
  newConversation: h.newConversation,
  appendMessage: h.appendMessage,
  getConversation: h.getConversation,
  deleteConversation: h.deleteConversation,
  relativeTime: () => 'now',
  deriveTitle: (t) => (t || '').slice(0, 40),
}))
vi.mock('../../components/layout/Navbar', () => ({ default: () => null }))
vi.mock('../../utils/conversationApi', () => ({ listProjectConversations: h.listProjectConversations }))
vi.mock('../../utils/usage', () => ({ notifyUsageChanged: h.notifyUsageChanged }))

import ChatPage from '../ChatPage'
import { ApiError } from '../../utils/apiError'

// Flat chat URL, as ChatRoute renders it. A brand-new chat carries its project in a
// transient query; the props are what ChatRoute would inject.
function LocationProbe() {
  const loc = useLocation()
  return <div data-testid="location">{`${loc.pathname}${loc.search}`}</div>
}

function renderChat(entry) {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <LocationProbe />
      <Routes>
        <Route path="/chat/:chatId" element={<ChatPage projectId="p1" projectName="VIP Movement" />} />
        <Route path="/projects/:projectId" element={<div>project home</div>} />
        <Route path="/projects" element={<div>projects index</div>} />
        <Route path="/login" element={<div>login page</div>} />
      </Routes>
    </MemoryRouter>,
  )
}

const assistantWrites = () => h.appendMessage.mock.calls.filter((c) => c[1].role === 'assistant')
const userWrites = () => h.appendMessage.mock.calls.filter((c) => c[1].role === 'user')

// A single-chunk "stream" that resolves (and calls onChunk) immediately —
// the equivalent of the old `h.sendMessage.mockResolvedValue(text)`.
function mockStreamResolves(text) {
  h.fetchClaudeStream.mockImplementation(({ onChunk }) => {
    onChunk(text, text)
    return Promise.resolve(text)
  })
}

// A stream that never resolves until the test calls the returned function —
// the equivalent of the old `h.sendMessage.mockImplementation(() => new
// Promise((res) => { resolveSend = res }))`, but also fires onChunk (the
// adapter's `finalText` comes from onChunk, not the promise's resolved value).
function mockStreamDeferred() {
  let resolveSend
  h.fetchClaudeStream.mockImplementation(({ onChunk }) => new Promise((res) => {
    resolveSend = (text) => {
      onChunk(text, text)
      res(text)
    }
  }))
  return (text) => resolveSend(text)
}

beforeEach(() => {
  vi.clearAllMocks()
  Element.prototype.scrollIntoView = vi.fn() // jsdom doesn't implement it
  h.loadHistory.mockResolvedValue([])
  h.listProjectConversations.mockResolvedValue([])
  h.getConversation.mockResolvedValue(null)
  h.appendMessage.mockResolvedValue({ ok: true })
  h.deleteConversation.mockResolvedValue(true)
})
afterEach(() => cleanup())

describe('ChatPage — send-path guards (U10)', () => {
  it('aborts the send before streaming when the user-turn persist fails (no orphan assistant turn)', async () => {
    h.newConversation.mockReturnValue('chat-1')
    h.appendMessage.mockRejectedValue(new Error('network down'))
    renderChat('/chat/chat-1?projectId=p1&kind=planning')

    const textarea = await screen.findByPlaceholderText(/Describe what you're thinking/i)
    fireEvent.change(textarea, { target: { value: 'hello' } })
    fireEvent.keyDown(textarea, { key: 'Enter' })

    // The user turn was attempted and rejected → the send aborts with a toast.
    expect(await screen.findByText(/Could not save your message/i)).toBeTruthy()
    expect(userWrites()).toHaveLength(1)
    // The stream was never started and no assistant turn was persisted.
    expect(h.fetchClaudeStream).not.toHaveBeenCalled()
    expect(assistantWrites()).toHaveLength(0)
  })

  it('a conversation switch mid-stream does not write the assistant turn onto the previous conversation', async () => {
    h.listProjectConversations.mockResolvedValue([
      { id: 'chat-1', kind: 'planning', title: 'First', updatedAt: new Date().toISOString() },
      { id: 'chat-2', kind: 'planning', title: 'Second', updatedAt: new Date(Date.now() - 1000).toISOString() },
    ])
    h.getConversation.mockImplementation(async (id) => ({
      id, kind: 'planning', title: id, messages: [], updatedAt: new Date().toISOString(),
    }))
    const resolveSend = mockStreamDeferred()

    renderChat('/chat/chat-1')
    // Wait until chat-1 has hydrated (empty-state implies hydrating=false + active id set).
    expect(await screen.findByText(/Plan your next app/i)).toBeTruthy()

    const textarea = screen.getByPlaceholderText(/Describe what you're thinking/i)
    fireEvent.change(textarea, { target: { value: 'hi' } })
    fireEvent.keyDown(textarea, { key: 'Enter' })
    await waitFor(() => expect(h.fetchClaudeStream).toHaveBeenCalledTimes(1))
    expect(userWrites().some((c) => c[0] === 'chat-1')).toBe(true)

    // Switch to chat-2 while chat-1's reply is still streaming.
    fireEvent.click(screen.getByText('Second'))
    await waitFor(() => expect(h.getConversation).toHaveBeenCalledWith('chat-2'))

    // The stream completes after the switch.
    await act(async () => { resolveSend('assistant reply'); await Promise.resolve() })

    // No assistant turn is persisted — it would otherwise land on chat-1.
    expect(assistantWrites()).toHaveLength(0)
  })
})

describe('ChatPage — seq is minted from what actually persisted, not the live thread length (PR #35 comment 1)', () => {
  it('a failed user-turn persist does not burn a seq number — the retry reuses it', async () => {
    h.newConversation.mockReturnValue('chat-1')
    h.appendMessage.mockRejectedValueOnce(new Error('network down'))
    renderChat('/chat/chat-1?projectId=p1&kind=planning')

    const textarea = await screen.findByPlaceholderText(/Describe what you're thinking/i)
    fireEvent.change(textarea, { target: { value: 'first attempt' } })
    fireEvent.keyDown(textarea, { key: 'Enter' })
    await screen.findByText(/Could not save your message/i)
    expect(userWrites()).toHaveLength(1)
    expect(userWrites()[0][1].seq).toBe(0)

    // Retry — this time persistence succeeds. assistant-ui's own live thread
    // array still holds the failed first turn (it was optimistically appended
    // before run() was ever called), so the old `messages.length - 1` heuristic
    // would mint seq 1 here — skipping 0 forever and desyncing every later seq
    // in this conversation. The corrected counter never advanced past the
    // failed attempt, so the retry correctly reuses seq 0.
    mockStreamResolves('ok')
    h.appendMessage.mockResolvedValue({ ok: true })
    fireEvent.change(textarea, { target: { value: 'second attempt' } })
    fireEvent.keyDown(textarea, { key: 'Enter' })
    await waitFor(() => expect(userWrites()).toHaveLength(2))
    expect(userWrites()[1][1].seq).toBe(0)
  })

  it('resuming a hydrated conversation seeds the next seq from the persisted max', async () => {
    h.getConversation.mockResolvedValue({
      id: 'chat-1',
      kind: 'planning',
      title: 'Resumed',
      messages: [
        { id: 'm0', role: 'user', parts: [{ type: 'text', text: 'hi' }], seq: 0 },
        { id: 'm1', role: 'assistant', parts: [{ type: 'text', text: 'hello' }], seq: 1 },
      ],
    })
    mockStreamResolves('sure')
    renderChat('/chat/chat-1')

    const textarea = await screen.findByPlaceholderText(/Describe what you're thinking/i)
    fireEvent.change(textarea, { target: { value: 'continuing' } })
    fireEvent.keyDown(textarea, { key: 'Enter' })

    await waitFor(() => expect(userWrites()).toHaveLength(1))
    // Not 0 (which the old messages.length-derived approach could produce if the
    // live array were ever seeded fresh) and not colliding with either
    // already-persisted turn.
    expect(userWrites()[0][1].seq).toBe(2)
    await waitFor(() => expect(assistantWrites()).toHaveLength(1))
    expect(assistantWrites()[0][1].seq).toBe(3)
  })
})

describe('ChatPage — a failed persist marks the turn as errored, not a normal sent message (PR #35 comment 2)', () => {
  it('a genuinely failed user-turn persist shows an inline error on the turn', async () => {
    h.newConversation.mockReturnValue('chat-1')
    h.appendMessage.mockRejectedValue(new Error('network down'))
    renderChat('/chat/chat-1?projectId=p1&kind=planning')

    const textarea = await screen.findByPlaceholderText(/Describe what you're thinking/i)
    fireEvent.change(textarea, { target: { value: 'hello' } })
    fireEvent.keyDown(textarea, { key: 'Enter' })

    await screen.findByText(/Could not save your message/i) // the existing banner still fires
    const alert = await screen.findByRole('alert') // MessagePrimitive.Error, wired in thread.jsx
    expect(alert.textContent).toMatch(/not saved/i)
  })

  it('a duplicate-message 409 (the turn actually landed) does NOT mark it as errored', async () => {
    h.newConversation.mockReturnValue('chat-1')
    h.appendMessage.mockRejectedValue(new ApiError('duplicate', 409))
    renderChat('/chat/chat-1?projectId=p1&kind=planning')

    const textarea = await screen.findByPlaceholderText(/Describe what you're thinking/i)
    fireEvent.change(textarea, { target: { value: 'hello' } })
    fireEvent.keyDown(textarea, { key: 'Enter' })

    await screen.findByText(/already saved/i) // describeSaveFailure's duplicate-message copy
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('a stream failure clears any partial text and shows an inline error instead of a normal reply', async () => {
    h.fetchClaudeStream.mockImplementation(({ onChunk }) => {
      onChunk('partial rep', 'partial rep')
      return Promise.reject(new Error('stream broke'))
    })
    renderChat('/chat/chat-1?projectId=p1&kind=planning')

    const textarea = await screen.findByPlaceholderText(/Describe what you're thinking/i)
    fireEvent.change(textarea, { target: { value: 'hello' } })
    fireEvent.keyDown(textarea, { key: 'Enter' })

    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toMatch(/stream broke/i)
    expect(screen.queryByText('partial rep')).toBeNull()
    expect(assistantWrites()).toHaveLength(0)
  })

  it('an assistant-turn persist failure (after a full successful stream) shows an inline error too', async () => {
    mockStreamResolves('a full reply')
    h.appendMessage.mockImplementation(async (_id, message) => {
      if (message.role === 'assistant') throw new Error('save failed')
      return { ok: true }
    })
    renderChat('/chat/chat-1?projectId=p1&kind=planning')

    const textarea = await screen.findByPlaceholderText(/Describe what you're thinking/i)
    fireEvent.change(textarea, { target: { value: 'hello' } })
    fireEvent.keyDown(textarea, { key: 'Enter' })

    await screen.findByText(/reply could not be saved/i)
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toMatch(/could not be saved/i)
  })
})

describe('ChatPage — rapid A -> B -> A navigation does not strand an empty runtime (PR #35 comment 3)', () => {
  it('a message from A is still visible after switching to B and back before B ever hydrates', async () => {
    h.listProjectConversations.mockResolvedValue([
      { id: 'chat-A', kind: 'planning', title: 'First', updatedAt: new Date().toISOString() },
      { id: 'chat-B', kind: 'planning', title: 'Second', updatedAt: new Date(Date.now() - 1000).toISOString() },
    ])

    // Every getConversation call gets its own controllable promise, in call
    // order, so the test can hold each fetch open exactly as long as needed.
    const pending = []
    h.getConversation.mockImplementation(
      (id) => new Promise((resolve) => pending.push({ id, resolve })),
    )
    const conv = (id, messages) => ({ id, kind: 'planning', title: id, messages, updatedAt: new Date().toISOString() })

    renderChat('/chat/chat-A')
    await waitFor(() => expect(pending).toHaveLength(1))
    await act(async () => {
      pending[0].resolve(conv('chat-A', [{ id: 'm0', role: 'user', parts: [{ type: 'text', text: 'hi from A' }], seq: 0 }]))
    })
    await screen.findByText('hi from A')

    // Switch to B — its fetch is left unresolved for the rest of this test.
    fireEvent.click(await screen.findByText('Second'))
    await waitFor(() => expect(pending).toHaveLength(2))
    expect(pending[1].id).toBe('chat-B')

    // Switch back to A before B's fetch ever resolves — triggers a fresh A fetch.
    fireEvent.click(await screen.findByText('First'))
    await waitFor(() => expect(pending).toHaveLength(3))
    expect(pending[2].id).toBe('chat-A')

    // Resolve A's second fetch. Under the bug, ChatRuntimeArea already mounted
    // with an empty initialMessages the instant chatId flipped back to
    // 'chat-A' (readyForChatId was stale-equal to 'chat-A' the whole time, so
    // `hydrating` incorrectly computed false on that render) — useLocalRuntime
    // reads initialMessages exactly once, so the thread would stay empty
    // forever even after this resolves. Fixed, the spinner holds the mount
    // until this resolves, so the fresh runtime seeds with the real message.
    await act(async () => {
      pending[2].resolve(conv('chat-A', [{ id: 'm0', role: 'user', parts: [{ type: 'text', text: 'hi from A' }], seq: 0 }]))
    })
    await waitFor(() => expect(screen.getByText('hi from A')).toBeTruthy())
  })
})

describe('ChatPage — continuing an old attachment conversation keeps the model grounded (PR #35 comment 6)', () => {
  it('sends a historical inline text-attachment as sticky fenced text in the API prompt', async () => {
    h.getConversation.mockResolvedValue({
      id: 'chat-1',
      kind: 'planning',
      title: 'Roster chat',
      messages: [
        {
          id: 'm0',
          role: 'user',
          seq: 0,
          parts: [
            { type: 'text', text: 'name,role\nA,Pilot', attachment: { attachmentId: 'r1', name: 'roster.csv', mediaType: 'text/csv' } },
            { type: 'text', text: 'who is this' },
          ],
        },
        { id: 'm1', role: 'assistant', seq: 1, parts: [{ type: 'text', text: 'A is a pilot.' }] },
      ],
    })
    mockStreamResolves('follow-up answer')
    renderChat('/chat/chat-1')

    // The display bubble stays prose-only (documented gap) — the attachment
    // text itself never renders raw in the UI.
    await screen.findByText('who is this')
    expect(screen.queryByText(/name,role/)).toBeNull()

    const textarea = screen.getByPlaceholderText(/Describe what you're thinking/i)
    fireEvent.change(textarea, { target: { value: 'and now?' } })
    fireEvent.keyDown(textarea, { key: 'Enter' })

    await waitFor(() => expect(h.fetchClaudeStream).toHaveBeenCalled())
    const sentMessages = h.fetchClaudeStream.mock.calls[0][0].body.messages
    // The historical user turn still carries the sticky attachment fence —
    // this is what would have silently vanished without getOriginalParts.
    expect(sentMessages[0].content).toContain('<attachment name="roster.csv" type="text">')
    expect(sentMessages[0].content).toContain('name,role\nA,Pilot')
    expect(sentMessages[0].content).toContain('who is this')
    // The assistant turn and the brand-new user turn are unaffected (prose only).
    expect(sentMessages[1].content).toBe('A is a pilot.')
    expect(sentMessages[2].content).toBe('and now?')
  })
})

describe('ChatPage — the 180k-token input backstop applies to the migrated send path (PR #35 comment 8)', () => {
  it('trims historical turns from the front when the conversation exceeds the token budget', async () => {
    // ~720k chars ≈ 180k tokens at the 4-chars/token estimate — comfortably over
    // the INPUT_TOKEN_BUDGET backstop, well past the (mocked-permissive here)
    // 150k/200k warn/hard-block UI band.
    const bigTurn = (i) => ({ id: `m${i}`, role: i % 2 === 0 ? 'user' : 'assistant', seq: i, parts: [{ type: 'text', text: 'x'.repeat(80_000) }] })
    const history = Array.from({ length: 10 }, (_, i) => bigTurn(i))
    h.getConversation.mockResolvedValue({ id: 'chat-1', kind: 'planning', title: 'Long chat', messages: history })
    mockStreamResolves('ok')
    renderChat('/chat/chat-1')

    await screen.findByPlaceholderText(/Describe what you're thinking/i)
    const textarea = screen.getByPlaceholderText(/Describe what you're thinking/i)
    fireEvent.change(textarea, { target: { value: 'continue' } })
    fireEvent.keyDown(textarea, { key: 'Enter' })

    await waitFor(() => expect(h.fetchClaudeStream).toHaveBeenCalled())
    const sentMessages = h.fetchClaudeStream.mock.calls[0][0].body.messages
    // 10 historical turns + the new one = 11 total; truncation must have
    // dropped some of the oldest ones rather than sending all 11 raw.
    expect(sentMessages.length).toBeLessThan(11)
    // The newest (just-sent) turn always survives — truncateMessages trims
    // from the front, never the tail.
    expect(sentMessages.at(-1).content).toBe('continue')
  })

  it('sends the full history unchanged when comfortably under the token budget', async () => {
    h.getConversation.mockResolvedValue({
      id: 'chat-1',
      kind: 'planning',
      title: 'Short chat',
      messages: [
        { id: 'm0', role: 'user', seq: 0, parts: [{ type: 'text', text: 'hi' }] },
        { id: 'm1', role: 'assistant', seq: 1, parts: [{ type: 'text', text: 'hello' }] },
      ],
    })
    mockStreamResolves('ok')
    renderChat('/chat/chat-1')

    const textarea = await screen.findByPlaceholderText(/Describe what you're thinking/i)
    fireEvent.change(textarea, { target: { value: 'continue' } })
    fireEvent.keyDown(textarea, { key: 'Enter' })

    await waitFor(() => expect(h.fetchClaudeStream).toHaveBeenCalled())
    const sentMessages = h.fetchClaudeStream.mock.calls[0][0].body.messages
    expect(sentMessages.length).toBe(3) // both historical turns + the new one, nothing trimmed
  })
})

describe('ChatPage — project-first send path', () => {
  it('sends header.projectId on the create branch and the conversationId to /claude', async () => {
    mockStreamResolves('sure thing')
    renderChat('/chat/chat-1?projectId=p1&kind=planning')

    const textarea = await screen.findByPlaceholderText(/Describe what you're thinking/i)
    fireEvent.change(textarea, { target: { value: 'hello' } })
    fireEvent.keyDown(textarea, { key: 'Enter' })

    await waitFor(() => expect(h.fetchClaudeStream).toHaveBeenCalled())
    const [id, message, header] = h.appendMessage.mock.calls[0]
    expect(id).toBe('chat-1')
    expect(message.role).toBe('user')
    expect(header.projectId).toBe('p1')
    // The server resolves this to fold in the project's description.
    expect(h.fetchClaudeStream.mock.calls[0][0].body.conversationId).toBe('chat-1')
  })

  it('persists the user turn BEFORE streaming — the row must exist when /claude resolves it', async () => {
    mockStreamResolves('ok')
    renderChat('/chat/chat-1?projectId=p1&kind=planning')
    const textarea = await screen.findByPlaceholderText(/Describe what you're thinking/i)
    fireEvent.change(textarea, { target: { value: 'hello' } })
    fireEvent.keyDown(textarea, { key: 'Enter' })

    await waitFor(() => expect(h.fetchClaudeStream).toHaveBeenCalled())
    expect(h.appendMessage.mock.invocationCallOrder[0]).toBeLessThan(h.fetchClaudeStream.mock.invocationCallOrder[0])
  })

  it('leaves for /projects when the append 404s because the project was deleted', async () => {
    h.appendMessage.mockRejectedValue(new ApiError('Project not found.', 404))
    renderChat('/chat/chat-1?projectId=p1&kind=planning')
    const textarea = await screen.findByPlaceholderText(/Describe what you're thinking/i)
    fireEvent.change(textarea, { target: { value: 'hello' } })
    fireEvent.keyDown(textarea, { key: 'Enter' })

    expect(await screen.findByText('projects index')).toBeTruthy()
    expect(h.fetchClaudeStream).not.toHaveBeenCalled()
  })

  it('renders the project breadcrumb — the only way out of a flat chat URL', async () => {
    renderChat('/chat/chat-1')
    const link = await screen.findByRole('link', { name: /VIP Movement/i })
    expect(link.getAttribute('href')).toBe('/projects/p1')
  })
})

describe('ChatPage — the composer is not shared across a chat navigation', () => {
  it('drops a typed draft when navigating to a different chat', async () => {
    // ChatRuntimeArea is keyed by chatId, so a navigation to a different chat fully
    // remounts the Thread/composer subtree (a fresh useLocalRuntime instance seeded
    // with that chat's own hydrated transcript) — a typed draft can't leak from
    // chat A into chat B's composer.
    h.listProjectConversations.mockResolvedValue([
      { id: 'chat-1', kind: 'planning', title: 'First', updatedAt: new Date().toISOString() },
      { id: 'chat-2', kind: 'planning', title: 'Second', updatedAt: new Date(Date.now() - 1000).toISOString() },
    ])
    h.getConversation.mockImplementation(async (id) => ({
      id, kind: 'planning', title: id, messages: [], updatedAt: new Date().toISOString(),
    }))
    renderChat('/chat/chat-1')
    expect(await screen.findByText(/Plan your next app/i)).toBeTruthy()

    const textarea = screen.getByPlaceholderText(/Describe what you're thinking/i)
    fireEvent.change(textarea, { target: { value: 'a draft meant only for chat-1' } })
    expect(textarea.value).toBe('a draft meant only for chat-1')

    // Switch to chat-2 in the same instance; the remount must reset the composer.
    fireEvent.click(screen.getByText('Second'))
    await waitFor(() => expect(h.getConversation).toHaveBeenCalledWith('chat-2'))

    await waitFor(() => {
      expect(screen.getByPlaceholderText(/Describe what you're thinking/i).value).toBe('')
    })
  })
})

describe('ChatPage — deleting a streaming chat is gated (F-1)', () => {
  it('disables delete for the streaming chat but still allows deleting a different one', async () => {
    // Belt over the activeChatIdRef write-path guard (which the mid-stream-switch test above
    // already proves no-ops a late assistant write): while chat-1 streams, its own delete
    // control is disabled so the resurrecting delete can't be issued in-chat — but a different,
    // non-streaming chat stays deletable (no over-gating).
    h.listProjectConversations.mockResolvedValue([
      { id: 'chat-1', kind: 'planning', title: 'First', updatedAt: new Date().toISOString() },
      { id: 'chat-2', kind: 'planning', title: 'Second', updatedAt: new Date(Date.now() - 1000).toISOString() },
    ])
    h.getConversation.mockImplementation(async (id) => ({
      id, kind: 'planning', title: id, messages: [], updatedAt: new Date().toISOString(),
    }))
    const resolveSend = mockStreamDeferred()

    renderChat('/chat/chat-1')
    expect(await screen.findByText(/Plan your next app/i)).toBeTruthy()

    const textarea = screen.getByPlaceholderText(/Describe what you're thinking/i)
    fireEvent.change(textarea, { target: { value: 'hi' } })
    fireEvent.keyDown(textarea, { key: 'Enter' })
    await waitFor(() => expect(h.fetchClaudeStream).toHaveBeenCalledTimes(1))

    // chat-1 is the active, streaming chat → its delete is disabled...
    expect(screen.getByLabelText('Delete First').disabled).toBe(true)
    // ...while chat-2 (not streaming) stays deletable.
    const delTwo = screen.getByLabelText('Delete Second')
    expect(delTwo.disabled).toBe(false)
    fireEvent.click(delTwo)
    await waitFor(() => expect(h.deleteConversation).toHaveBeenCalledWith('chat-2'))
    expect(h.deleteConversation).not.toHaveBeenCalledWith('chat-1')

    // chat-1 was never deleted — let its stream finish cleanly.
    await act(async () => { resolveSend('assistant reply'); await Promise.resolve() })
  })

  it('follows the STREAMING chat after a mid-stream navigate (no over-gate on the new view; re-enables when done)', async () => {
    // The gate keys off the streaming id, not the viewed id — so navigating to a sibling chat
    // mid-stream must NOT disable the sibling's own delete, while the still-streaming chat stays
    // gated even though it is no longer on screen.
    h.listProjectConversations.mockResolvedValue([
      { id: 'chat-1', kind: 'planning', title: 'First', updatedAt: new Date().toISOString() },
      { id: 'chat-2', kind: 'planning', title: 'Second', updatedAt: new Date(Date.now() - 1000).toISOString() },
    ])
    h.getConversation.mockImplementation(async (id) => ({
      id, kind: 'planning', title: id, messages: [], updatedAt: new Date().toISOString(),
    }))
    const resolveSend = mockStreamDeferred()

    renderChat('/chat/chat-1')
    expect(await screen.findByText(/Plan your next app/i)).toBeTruthy()
    const textarea = screen.getByPlaceholderText(/Describe what you're thinking/i)
    fireEvent.change(textarea, { target: { value: 'hi' } })
    fireEvent.keyDown(textarea, { key: 'Enter' })
    await waitFor(() => expect(h.fetchClaudeStream).toHaveBeenCalledTimes(1))

    // Navigate to chat-2 while chat-1 still streams in the background.
    fireEvent.click(screen.getByText('Second'))
    await waitFor(() => expect(h.getConversation).toHaveBeenCalledWith('chat-2'))

    // chat-1 (streaming) stays disabled; chat-2 (the new view, not streaming) is NOT over-gated.
    expect(screen.getByLabelText('Delete First').disabled).toBe(true)
    expect(screen.getByLabelText('Delete Second').disabled).toBe(false)

    // When chat-1's stream ends, its delete re-enables.
    await act(async () => { resolveSend('assistant reply'); await Promise.resolve() })
    await waitFor(() => expect(screen.getByLabelText('Delete First').disabled).toBe(false))
  })

  it('an overlapping run on a different chat does not clobber the first chat\'s delete gate (PR #35 comment 17)', async () => {
    // A scalar streamingChatId would let the second chat's onRunStart overwrite
    // the first's, falsely re-enabling chat-1's delete while its persist may
    // still be in flight — navigating away doesn't synchronously end its run().
    h.listProjectConversations.mockResolvedValue([
      { id: 'chat-1', kind: 'planning', title: 'First', updatedAt: new Date().toISOString() },
      { id: 'chat-2', kind: 'planning', title: 'Second', updatedAt: new Date(Date.now() - 1000).toISOString() },
    ])
    h.getConversation.mockImplementation(async (id) => ({
      id, kind: 'planning', title: id, messages: [], updatedAt: new Date().toISOString(),
    }))
    const resolveFirst = mockStreamDeferred()

    renderChat('/chat/chat-1')
    expect(await screen.findByText(/Plan your next app/i)).toBeTruthy()
    const textarea1 = screen.getByPlaceholderText(/Describe what you're thinking/i)
    fireEvent.change(textarea1, { target: { value: 'hi' } })
    fireEvent.keyDown(textarea1, { key: 'Enter' })
    await waitFor(() => expect(h.fetchClaudeStream).toHaveBeenCalledTimes(1))
    expect(screen.getByLabelText('Delete First').disabled).toBe(true)

    // Navigate to chat-2 while chat-1 still streams, then start chat-2's OWN
    // stream too, before chat-1's has settled.
    fireEvent.click(screen.getByText('Second'))
    await waitFor(() => expect(h.getConversation).toHaveBeenCalledWith('chat-2'))
    const resolveSecond = mockStreamDeferred()
    const textarea2 = screen.getByPlaceholderText(/Describe what you're thinking/i)
    fireEvent.change(textarea2, { target: { value: 'hi from two' } })
    fireEvent.keyDown(textarea2, { key: 'Enter' })
    await waitFor(() => expect(h.fetchClaudeStream).toHaveBeenCalledTimes(2))

    // Both runs are in flight now — chat-1's gate must survive chat-2's onRunStart.
    expect(screen.getByLabelText('Delete First').disabled).toBe(true)
    expect(screen.getByLabelText('Delete Second').disabled).toBe(true)

    // Finish chat-1's stream — only ITS gate clears; chat-2's stays (still running).
    await act(async () => { resolveFirst('first reply'); await Promise.resolve() })
    await waitFor(() => expect(screen.getByLabelText('Delete First').disabled).toBe(false))
    expect(screen.getByLabelText('Delete Second').disabled).toBe(true)

    // Finish chat-2's stream too, for cleanliness.
    await act(async () => { resolveSecond('second reply'); await Promise.resolve() })
    await waitFor(() => expect(screen.getByLabelText('Delete Second').disabled).toBe(false))
  })
})

describe('ChatPage — the transient ?projectId= query is dropped once the row exists', () => {
  it('rewrites to the bare /chat/{id} after the first successful append', async () => {
    mockStreamResolves('ok')
    renderChat('/chat/chat-1?projectId=p1&kind=planning')
    expect(screen.getByTestId('location').textContent).toBe('/chat/chat-1?projectId=p1&kind=planning')

    const textarea = await screen.findByPlaceholderText(/Describe what you're thinking/i)
    fireEvent.change(textarea, { target: { value: 'hello' } })
    fireEvent.keyDown(textarea, { key: 'Enter' })

    // The conversation now exists, so conversation.projectId is authoritative and the
    // query is dead weight — the address a user copies must be the flat one.
    await waitFor(() => expect(screen.getByTestId('location').textContent).toBe('/chat/chat-1'))
  })

  it('does not rewrite when the append fails — the query still carries the only project link', async () => {
    h.appendMessage.mockRejectedValue(new Error('network down'))
    renderChat('/chat/chat-1?projectId=p1&kind=planning')
    const textarea = await screen.findByPlaceholderText(/Describe what you're thinking/i)
    fireEvent.change(textarea, { target: { value: 'hello' } })
    fireEvent.keyDown(textarea, { key: 'Enter' })

    await screen.findByText(/Could not save your message/i)
    expect(screen.getByTestId('location').textContent).toBe('/chat/chat-1?projectId=p1&kind=planning')
  })
})

describe('ChatPage — the StrictMode hydration/double-send guards (PR #35 comment 7)', () => {
  // React StrictMode dev-mounts each effect twice (mount -> cleanup -> remount).
  // Mirrors BuilderPage-projectfirst.test.jsx's established StrictMode block —
  // same two hazards the PR body describes: a double-effect hydration stall
  // (readyForChatId/activeChatIdRef's StrictMode-safe guards), and a double-send
  // of the initial handoff message (InitialMessageSender's firedRef guard).
  it('renders a hydrated conversation under <StrictMode> (no hydration stall)', async () => {
    h.getConversation.mockResolvedValue({
      id: 'chat-1',
      kind: 'planning',
      title: 'Existing',
      messages: [{ id: 'm0', role: 'assistant', parts: [{ type: 'text', text: 'STRICTMODE SAVED LINE' }], seq: 0 }],
      updatedAt: new Date().toISOString(),
    })
    render(
      <StrictMode>
        <MemoryRouter initialEntries={['/chat/chat-1']}>
          <Routes>
            <Route path="/chat/:chatId" element={<ChatPage projectId="p1" projectName="VIP Movement" />} />
          </Routes>
        </MemoryRouter>
      </StrictMode>,
    )
    expect((await screen.findAllByText('STRICTMODE SAVED LINE')).length).toBeGreaterThan(0)
  })

  it('fires the initial handoff message exactly once under <StrictMode> (no double-send)', async () => {
    h.getConversation.mockResolvedValue(null) // a brand-new chat: seed from location.state.initialMessage
    mockStreamResolves('sure thing')
    render(
      <StrictMode>
        <MemoryRouter
          initialEntries={[
            { pathname: '/chat/chat-1', search: '?projectId=p1&kind=planning', state: { initialMessage: 'build me a report' } },
          ]}
        >
          <Routes>
            <Route path="/chat/:chatId" element={<ChatPage projectId="p1" projectName="VIP Movement" />} />
          </Routes>
        </MemoryRouter>
      </StrictMode>,
    )
    await waitFor(() => expect(h.fetchClaudeStream).toHaveBeenCalled())
    await act(async () => { await Promise.resolve() })
    expect(h.fetchClaudeStream).toHaveBeenCalledTimes(1)
    expect(h.fetchClaudeStream.mock.calls[0][0].body.messages.at(-1).content).toBe('build me a report')
  })

  it('non-StrictMode single mount still fires the initial message exactly once (no regression)', async () => {
    h.getConversation.mockResolvedValue(null)
    mockStreamResolves('sure thing')
    render(
      <MemoryRouter
        initialEntries={[
          { pathname: '/chat/chat-1', search: '?projectId=p1&kind=planning', state: { initialMessage: 'build me a report' } },
        ]}
      >
        <Routes>
          <Route path="/chat/:chatId" element={<ChatPage projectId="p1" projectName="VIP Movement" />} />
        </Routes>
      </MemoryRouter>,
    )
    await waitFor(() => expect(h.fetchClaudeStream).toHaveBeenCalled())
    expect(h.fetchClaudeStream).toHaveBeenCalledTimes(1)
  })
})

describe('ChatPage — adapter failure branches reach the right end-to-end outcome (PR #35 comment 9)', () => {
  // The generic-rejection path (banner shown, no assistant persist) and the
  // duplicate-message 409 carve-out are already covered by the "failed persist
  // marks the turn as errored" describe block (PR #35 comment 2). The 429
  // daily-limit MESSAGE ITSELF is already dedicated-tested in
  // useClaudeAPI-retry.test.js; the adapter relays whatever thrown.message says
  // through the exact same unconditional code path already exercised there, so
  // it isn't re-tested here. These two fill the two branches that were
  // genuinely untested anywhere: the auth-refresh redirect, and the abort path.

  it('AUTH_REFRESH_FAILED clears the session and navigates to /login', async () => {
    const authErr = new Error('session dead')
    authErr.code = 'AUTH_REFRESH_FAILED'
    h.fetchClaudeStream.mockImplementation(() => Promise.reject(authErr))
    renderChat('/chat/chat-1?projectId=p1&kind=planning')

    const textarea = await screen.findByPlaceholderText(/Describe what you're thinking/i)
    fireEvent.change(textarea, { target: { value: 'hello' } })
    fireEvent.keyDown(textarea, { key: 'Enter' })

    await screen.findByText('login page')
  })

  it('a cancelled (aborted) stream persists nothing and does not notify usage', async () => {
    const resolveSend = mockStreamDeferred()
    renderChat('/chat/chat-1?projectId=p1&kind=planning')

    const textarea = await screen.findByPlaceholderText(/Describe what you're thinking/i)
    fireEvent.change(textarea, { target: { value: 'hello' } })
    fireEvent.keyDown(textarea, { key: 'Enter' })
    await waitFor(() => expect(h.fetchClaudeStream).toHaveBeenCalled())

    // Cancel while streaming — shares the same abortSignal plumbing as
    // fetchClaudeStream's logout/unmount handling, which resolves normally
    // with whatever text had streamed so far rather than rejecting.
    fireEvent.click(screen.getByLabelText('Stop generating'))
    await act(async () => {
      resolveSend('partial text that streamed before cancel')
      await Promise.resolve()
    })

    expect(assistantWrites()).toHaveLength(0)
    expect(h.notifyUsageChanged).not.toHaveBeenCalled()
  })
})

describe('ChatPage — the assistant action bar has no regenerate control (PR #35 comment 13)', () => {
  // ChatPage always renders via AssistantActionBarNoRegenerate (regenerating
  // would recompute seq from a truncated messages array and collide with the
  // already-persisted turn — see thread.jsx's own comment). Pins that the
  // hideRegenerate refactor (comment 13) didn't silently let Refresh back in.
  it('shows Copy but not Refresh on a completed assistant reply', async () => {
    mockStreamResolves('a completed reply')
    renderChat('/chat/chat-1?projectId=p1&kind=planning')

    const textarea = await screen.findByPlaceholderText(/Describe what you're thinking/i)
    fireEvent.change(textarea, { target: { value: 'hello' } })
    fireEvent.keyDown(textarea, { key: 'Enter' })

    expect((await screen.findAllByText('Copy')).length).toBeGreaterThan(0)
    expect(screen.queryByText('Refresh')).toBeNull()
  })
})
