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
}))

vi.mock('../../hooks/useClaudeAPI', () => ({
  useClaudeAPI: () => ({ sendMessage: h.sendMessage, error: null }),
  fetchClaudeStream: h.fetchClaudeStream,
  getContextLimits: () => ({ soft: 1e9, hard: 1e9 }),
  estimateConversationTokens: () => 0,
}))
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
