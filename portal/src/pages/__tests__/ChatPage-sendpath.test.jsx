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
 * The API + history store are mocked at the module boundary so we can hold the
 * stream open and assert exactly what gets persisted. The two-route MemoryRouter
 * mirrors App.jsx so navigate() preserves the ChatPage instance (refs survive).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act, cleanup } from '@testing-library/react'
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom'

const h = vi.hoisted(() => ({
  sendMessage: vi.fn(),
  abort: vi.fn(), // the F7 chat-switch abort — asserted by the mid-stream navigation tests
  error: null, // the useClaudeAPI error banner value — set per-test to exercise Regenerate
  loadHistory: vi.fn(),
  newConversation: vi.fn(),
  createConversation: vi.fn(),
  getConversation: vi.fn(),
  deleteConversation: vi.fn(),
  listProjectConversations: vi.fn(),
}))

vi.mock('../../hooks/useClaudeAPI', () => ({
  useClaudeAPI: () => ({ sendMessage: h.sendMessage, error: h.error ?? null, clearError: vi.fn(), abort: h.abort }),
  getContextLimits: () => ({ soft: 1e9, hard: 1e9 }),
  estimateConversationTokens: () => 0,
}))
vi.mock('../../utils/chatHistory', () => ({
  loadHistory: h.loadHistory,
  newConversation: h.newConversation,
  createConversation: h.createConversation,
  getConversation: h.getConversation,
  deleteConversation: h.deleteConversation,
  relativeTime: () => 'now',
  deriveTitle: (t) => (t || '').slice(0, 40),
}))
vi.mock('../../components/layout/Navbar', () => ({ default: () => null }))
// MessageContent is deliberately NOT mocked here (unlike ChatPage-launchbuilder.test.jsx) —
// this suite is the one place the real markdown renderer runs under a page-level integration
// test rather than only its own unit tests. Nothing in this file asserts on bubble prose
// content itself — the markers this suite reads, e.g. "cut off before it finished", render
// outside MessageContent.
// `uuidv7` — ChatPage's Launch-Builder handoff mints through the shared v7 mint (ADR-0006).
vi.mock('../../utils/conversationApi', () => ({
  listProjectConversations: h.listProjectConversations,
  uuidv7: () => '01900000-0000-7000-8000-000000000000',
}))

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

// U7: the client persists nothing — the server records both sides of the turn. The only
// client-side write is the FIRST-turn conversation create.
const creates = () => h.createConversation.mock.calls

beforeEach(() => {
  vi.clearAllMocks()
  h.error = null // reset the banner value (clearAllMocks doesn't touch plain props)
  Element.prototype.scrollIntoView = vi.fn() // jsdom doesn't implement it
  h.loadHistory.mockResolvedValue([])
  h.listProjectConversations.mockResolvedValue([])
  h.getConversation.mockResolvedValue(null)
  h.createConversation.mockResolvedValue({ id: 'chat-1', kind: 'planning', mode: 'plan' })
  h.deleteConversation.mockResolvedValue(true)
})
afterEach(() => cleanup())

describe('ChatPage — send-path guards (U10)', () => {
  it('aborts the send before streaming when the first-turn CREATE fails (U7)', async () => {
    h.newConversation.mockReturnValue('chat-1')
    h.createConversation.mockRejectedValue(new Error('network down'))
    renderChat('/chat/chat-1?projectId=p1&kind=planning')

    const textarea = await screen.findByPlaceholderText(/Describe what you're thinking/i)
    fireEvent.change(textarea, { target: { value: 'hello' } })
    fireEvent.keyDown(textarea, { key: 'Enter' })

    // The create was attempted and rejected → the send aborts with a toast.
    expect(await screen.findByText(/Could not save your message/i)).toBeTruthy()
    expect(creates()).toHaveLength(1)
    // The stream was never started — no orphan turn reaches the server.
    expect(h.sendMessage).not.toHaveBeenCalled()
  })

  it('a conversation switch mid-stream does not write the assistant turn onto the previous conversation', async () => {
    h.listProjectConversations.mockResolvedValue([
      { id: 'chat-1', kind: 'planning', title: 'First', updatedAt: new Date().toISOString() },
      { id: 'chat-2', kind: 'planning', title: 'Second', updatedAt: new Date(Date.now() - 1000).toISOString() },
    ])
    h.getConversation.mockImplementation(async (id) => ({
      id, kind: 'planning', title: id, messages: [], updatedAt: new Date().toISOString(),
    }))
    let resolveSend
    h.sendMessage.mockImplementation(() => new Promise((res) => { resolveSend = res }))

    renderChat('/chat/chat-1')
    // Wait until chat-1 has hydrated (empty-state implies hydrating=false + active id set).
    expect(await screen.findByText(/Plan your next app/i)).toBeTruthy()

    const textarea = screen.getByPlaceholderText(/Describe what you're thinking/i)
    fireEvent.change(textarea, { target: { value: 'hi' } })
    fireEvent.keyDown(textarea, { key: 'Enter' })
    await waitFor(() => expect(h.sendMessage).toHaveBeenCalledTimes(1))

    // Switch to chat-2 while chat-1's reply is still streaming.
    fireEvent.click(screen.getByText('Second'))
    await waitFor(() => expect(h.getConversation).toHaveBeenCalledWith('chat-2'))

    // The stream completes after the switch — the superseded resolve must render nothing
    // into chat-2 and fire nothing further (U7: persistence is the server's; the client's
    // only duty is not to leak the late text across chats).
    await act(async () => { resolveSend('assistant reply'); await Promise.resolve() })
    expect(screen.queryByText('assistant reply')).toBeNull()
    expect(h.sendMessage).toHaveBeenCalledTimes(1)
  })
})

describe('ChatPage — project-first send path', () => {
  it('creates the row with projectId, then names the conversation to /claude (U7)', async () => {
    h.sendMessage.mockResolvedValue('sure thing')
    renderChat('/chat/chat-1?projectId=p1&kind=planning')

    const textarea = await screen.findByPlaceholderText(/Describe what you're thinking/i)
    fireEvent.change(textarea, { target: { value: 'hello' } })
    fireEvent.keyDown(textarea, { key: 'Enter' })

    await waitFor(() => expect(h.sendMessage).toHaveBeenCalled())
    const [id, header] = h.createConversation.mock.calls[0]
    expect(id).toBe('chat-1')
    expect(header.projectId).toBe('p1')
    // The server resolves this to fold in the project's description (U7 signature:
    // message, onChunk, conversationId).
    expect(h.sendMessage.mock.calls[0][2]).toBe('chat-1')
    expect(h.sendMessage.mock.calls[0][0]).toEqual({ text: 'hello' })
  })

  it('creates the row BEFORE streaming — the relay 404s an unknown conversation (U7)', async () => {
    h.sendMessage.mockResolvedValue('ok')
    renderChat('/chat/chat-1?projectId=p1&kind=planning')
    const textarea = await screen.findByPlaceholderText(/Describe what you're thinking/i)
    fireEvent.change(textarea, { target: { value: 'hello' } })
    fireEvent.keyDown(textarea, { key: 'Enter' })

    await waitFor(() => expect(h.sendMessage).toHaveBeenCalled())
    expect(h.createConversation.mock.invocationCallOrder[0]).toBeLessThan(h.sendMessage.mock.invocationCallOrder[0])
  })

  it('leaves for /projects when the append 404s because the project was deleted', async () => {
    h.createConversation.mockRejectedValue(new ApiError('Project not found.', 404))
    renderChat('/chat/chat-1?projectId=p1&kind=planning')
    const textarea = await screen.findByPlaceholderText(/Describe what you're thinking/i)
    fireEvent.change(textarea, { target: { value: 'hello' } })
    fireEvent.keyDown(textarea, { key: 'Enter' })

    expect(await screen.findByText('projects index')).toBeTruthy()
    expect(h.sendMessage).not.toHaveBeenCalled()
  })

  it('renders the project breadcrumb — the only way out of a flat chat URL', async () => {
    renderChat('/chat/chat-1')
    const link = await screen.findByRole('link', { name: /VIP Movement/i })
    expect(link.getAttribute('href')).toBe('/projects/p1')
  })
})

describe('ChatPage — the composer is not shared across a chat navigation', () => {
  it('drops a typed draft when the same instance hydrates a different chat', async () => {
    // ChatRoute reuses this ChatPage across /chat/A → /chat/B (no key={chatId} remount), so a
    // draft the hydrate effect fails to clear is chat B wearing chat A's clothes.
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

    // Switch to chat-2 in the same instance; the hydrate effect must reset the composer.
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
    let resolveSend
    h.sendMessage.mockImplementation(() => new Promise((res) => { resolveSend = res }))

    renderChat('/chat/chat-1')
    expect(await screen.findByText(/Plan your next app/i)).toBeTruthy()

    const textarea = screen.getByPlaceholderText(/Describe what you're thinking/i)
    fireEvent.change(textarea, { target: { value: 'hi' } })
    fireEvent.keyDown(textarea, { key: 'Enter' })
    await waitFor(() => expect(h.sendMessage).toHaveBeenCalledTimes(1))

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

  it('a mid-stream navigate ABORTS the stream, so NO chat stays delete-gated afterwards (F7)', async () => {
    // Superseded semantics, deliberately: the gate used to FOLLOW the still-streaming chat after
    // a navigate, because the stream kept running in the background. Since the chat switch now
    // aborts the stream (F7), there is nothing left that could resurrect chat-1 — so both deletes
    // are correctly enabled the moment the switch lands, and the late resolve writes nothing.
    h.listProjectConversations.mockResolvedValue([
      { id: 'chat-1', kind: 'planning', title: 'First', updatedAt: new Date().toISOString() },
      { id: 'chat-2', kind: 'planning', title: 'Second', updatedAt: new Date(Date.now() - 1000).toISOString() },
    ])
    h.getConversation.mockImplementation(async (id) => ({
      id, kind: 'planning', title: id, messages: [], updatedAt: new Date().toISOString(),
    }))
    let resolveSend
    h.sendMessage.mockImplementation(() => new Promise((res) => { resolveSend = res }))

    renderChat('/chat/chat-1')
    expect(await screen.findByText(/Plan your next app/i)).toBeTruthy()
    const textarea = screen.getByPlaceholderText(/Describe what you're thinking/i)
    fireEvent.change(textarea, { target: { value: 'hi' } })
    fireEvent.keyDown(textarea, { key: 'Enter' })
    await waitFor(() => expect(h.sendMessage).toHaveBeenCalledTimes(1))
    expect(screen.getByLabelText('Delete First').disabled).toBe(true) // gated WHILE streaming

    // Navigate to chat-2: the switch aborts chat-1's stream (h.abort) and clears the gate.
    const abortsBefore = h.abort.mock.calls.length
    fireEvent.click(screen.getByText('Second'))
    await waitFor(() => expect(h.getConversation).toHaveBeenCalledWith('chat-2'))
    expect(h.abort.mock.calls.length).toBeGreaterThan(abortsBefore)
    expect(screen.getByLabelText('Delete First').disabled).toBe(false)
    expect(screen.getByLabelText('Delete Second').disabled).toBe(false)

    // The aborted stream's late resolve renders nothing — deleting chat-1 now is safe.
    await act(async () => { resolveSend('assistant reply'); await Promise.resolve() })
    expect(screen.queryByText('assistant reply')).toBeNull()
  })
})

describe('ChatPage — the transient ?projectId= query is dropped once the row exists', () => {
  it('rewrites to the bare /chat/{id} once the first turn is under way', async () => {
    h.sendMessage.mockResolvedValue('ok')
    renderChat('/chat/chat-1?projectId=p1&kind=planning')
    expect(screen.getByTestId('location').textContent).toBe('/chat/chat-1?projectId=p1&kind=planning')

    const textarea = await screen.findByPlaceholderText(/Describe what you're thinking/i)
    fireEvent.change(textarea, { target: { value: 'hello' } })
    fireEvent.keyDown(textarea, { key: 'Enter' })

    // The conversation now exists, so conversation.projectId is authoritative and the
    // query is dead weight — the address a user copies must be the flat one.
    await waitFor(() => expect(screen.getByTestId('location').textContent).toBe('/chat/chat-1'))
  })

  it('does not rewrite when the create fails — the query still carries the only project link', async () => {
    h.createConversation.mockRejectedValue(new Error('network down'))
    renderChat('/chat/chat-1?projectId=p1&kind=planning')
    const textarea = await screen.findByPlaceholderText(/Describe what you're thinking/i)
    fireEvent.change(textarea, { target: { value: 'hello' } })
    fireEvent.keyDown(textarea, { key: 'Enter' })

    await screen.findByText(/Could not save your message/i)
    expect(screen.getByTestId('location').textContent).toBe('/chat/chat-1?projectId=p1&kind=planning')
  })
})

describe('ChatPage — handoff fires once, never re-posts on reload (F1)', () => {
  const handoff = (chatId, initialMessage) => ({
    pathname: `/chat/${chatId}`,
    search: '?projectId=p1&kind=planning',
    state: { initialMessage },
  })

  it('fires the handoff prompt exactly once when the SERVER transcript is empty', async () => {
    h.getConversation.mockResolvedValue(null) // brand-new chat — nothing persisted yet
    h.sendMessage.mockResolvedValue('an assistant reply')
    renderChat(handoff('chat-1', 'plan my app'))

    await waitFor(() => expect(h.sendMessage).toHaveBeenCalledTimes(1))
    // Exactly one create + one model call — no duplicate re-post on the handoff.
    expect(creates()).toHaveLength(1)
    expect(h.sendMessage.mock.calls[0][0]).toEqual({ text: 'plan my app' })
    expect(h.sendMessage).toHaveBeenCalledTimes(1)
  })

  it('does NOT re-fire the handoff when the server transcript already holds the turn (reload)', async () => {
    // On reload the browser restores location.state (initialMessage) AND the persisted turn is now
    // in the server transcript. Gating on the SERVER transcript (not the transient in-memory count,
    // which is [] for a beat on every mount) is what stops the duplicate re-post + re-call.
    h.getConversation.mockResolvedValue({
      id: 'chat-1',
      kind: 'planning',
      title: 'x',
      messages: [
        { id: 'm0', role: 'user', parts: [{ type: 'text', text: 'plan my app' }], seq: 0 },
        { id: 'm1', role: 'assistant', parts: [{ type: 'text', text: 'sure' }], seq: 1 },
      ],
      updatedAt: new Date().toISOString(),
    })
    renderChat(handoff('chat-1', 'plan my app'))

    await waitFor(() => expect(h.getConversation).toHaveBeenCalledWith('chat-1'))
    await act(async () => { await Promise.resolve() }) // flush the hydration .then decision
    expect(h.sendMessage).not.toHaveBeenCalled()
    expect(creates()).toHaveLength(0)
  })

  it('reopening a completed conversation shows it immediately with no re-fire', async () => {
    h.getConversation.mockResolvedValue({
      id: 'chat-1',
      kind: 'planning',
      title: 'x',
      messages: [{ id: 'm0', role: 'user', parts: [{ type: 'text', text: 'earlier' }], seq: 0 }],
      updatedAt: new Date().toISOString(),
    })
    renderChat('/chat/chat-1?projectId=p1&kind=planning') // no handoff state
    await waitFor(() => expect(h.getConversation).toHaveBeenCalledWith('chat-1'))
    await act(async () => { await Promise.resolve() })
    expect(h.sendMessage).not.toHaveBeenCalled()
    expect(screen.queryByText(/Plan your next app/i)).toBeNull() // not the empty state
  })
})

describe('ChatPage — a chat switch aborts the stream and leaks nothing cross-chat (F7)', () => {
  const twoChats = () => {
    h.listProjectConversations.mockResolvedValue([
      { id: 'chat-1', kind: 'planning', title: 'First', updatedAt: new Date().toISOString() },
      { id: 'chat-2', kind: 'planning', title: 'Second', updatedAt: new Date(Date.now() - 1000).toISOString() },
    ])
    h.getConversation.mockImplementation(async (id) => ({
      id, kind: 'planning', title: id, messages: [], updatedAt: new Date().toISOString(),
    }))
  }
  const attachButton = () => screen.getByTitle(/Attach images/i)
  /** The icon-only Send, i.e. the composer row's last button. */
  const sendButton = () =>
    screen.getByPlaceholderText(/Describe what you're thinking/i).parentElement.querySelector('button:last-of-type')

  it('navigating away mid-stream aborts the request and leaves the sibling chat fully composable', async () => {
    twoChats()
    let resolveSend
    h.sendMessage.mockImplementation(() => new Promise((res) => { resolveSend = res }))

    renderChat('/chat/chat-1')
    expect(await screen.findByText(/Plan your next app/i)).toBeTruthy()
    const textarea = screen.getByPlaceholderText(/Describe what you're thinking/i)
    fireEvent.change(textarea, { target: { value: 'hi' } })
    fireEvent.keyDown(textarea, { key: 'Enter' })
    await waitFor(() => expect(h.sendMessage).toHaveBeenCalledTimes(1))
    // Streaming HERE gates SENDING, not composing (U4): attach stays live so the user can stage
    // the file that rides their next message, and only Send waits.
    expect(attachButton().disabled).toBe(false)
    expect(sendButton().getAttribute('aria-disabled')).toBe('true')

    const abortsBefore = h.abort.mock.calls.length
    fireEvent.click(screen.getByText('Second'))
    await waitFor(() => expect(h.getConversation).toHaveBeenCalledWith('chat-2'))

    // The switch ABORTED the in-flight stream (the request must not keep billing into a void)…
    expect(h.abort.mock.calls.length).toBeGreaterThan(abortsBefore)
    // …and chat-2 is not locked by a stream that isn't its own: with something to send, send arms.
    // (Typing first is the honest check — an empty composer dims Send for its own reason.)
    fireEvent.change(screen.getByPlaceholderText(/Describe what you're thinking/i), { target: { value: 'a different question' } })
    expect(sendButton().getAttribute('aria-disabled')).toBe('false')
    expect(screen.queryByRole('status')).toBeNull()

    // The (aborted) stream resolving later renders nothing anywhere.
    await act(async () => { resolveSend(null); await Promise.resolve() })
    expect(attachButton().disabled).toBe(false)
  })

  it('A→B→A before the stream resolves: the superseded stream writes nothing into the rehydrated A', async () => {
    twoChats()
    let resolveSend
    h.sendMessage.mockImplementation(() => new Promise((res) => { resolveSend = res }))

    renderChat('/chat/chat-1')
    expect(await screen.findByText(/Plan your next app/i)).toBeTruthy()
    const textarea = screen.getByPlaceholderText(/Describe what you're thinking/i)
    fireEvent.change(textarea, { target: { value: 'hi' } })
    fireEvent.keyDown(textarea, { key: 'Enter' })
    await waitFor(() => expect(h.sendMessage).toHaveBeenCalledTimes(1))

    // Away and back before the stream settles — A is rehydrated from the server, WITHOUT the
    // stream's optimistic bubble, so a late resolve has nowhere honest to land.
    fireEvent.click(screen.getByText('Second'))
    await waitFor(() => expect(h.getConversation).toHaveBeenCalledWith('chat-2'))
    fireEvent.click(screen.getByText('First'))
    await waitFor(() => expect(h.getConversation).toHaveBeenCalledTimes(3)) // mount + B + A again

    // The stream resolves WITH text after the round trip: the generation fence must drop it —
    // same-chat-id alone would have rendered it onto the rebuilt transcript.
    await act(async () => { resolveSend('a late reply'); await Promise.resolve() })
    expect(screen.queryByText('a late reply')).toBeNull()
    expect(screen.getByTitle(/Attach images/i).disabled).toBe(false) // no generating leak either
  })
})

describe('ChatPage — a stalled stream keeps the partial reply (F1/U7)', () => {
  it('keeps the partial text with an interrupted marker; Regenerate REPLACES it by id (no duplicate)', async () => {
    h.error = 'The response stalled. Check your connection and try again.'
    h.getConversation.mockResolvedValue(null)
    // First turn: streams a partial then stalls (falsy result). The retry succeeds.
    h.sendMessage
      .mockImplementationOnce(async (_m, onChunk) => { onChunk('a partial ans'); return null })
      .mockImplementationOnce(async (_m, onChunk) => { onChunk('the full reply'); return 'the full reply' })
    renderChat('/chat/chat-1?projectId=p1&kind=planning')

    const textarea = await screen.findByPlaceholderText(/Describe what you're thinking/i)
    fireEvent.change(textarea, { target: { value: 'plan my app' } })
    fireEvent.keyDown(textarea, { key: 'Enter' })

    // The partial bubble SURVIVES the stall, visibly marked as interrupted (plan U7) — never
    // silently discarded (the user may already be reading it).
    expect(await screen.findByText(/cut off before it finished/i)).toBeTruthy()

    fireEvent.click(await screen.findByRole('button', { name: /try again/i }))
    await waitFor(() => expect(h.sendMessage).toHaveBeenCalledTimes(2))

    // The retry REPLACED the interrupted bubble (stable id, not an array index): the marker
    // is gone — and the retry rode the U7 regenerate flag, so the server records no second
    // copy of the user turn.
    await waitFor(() => expect(screen.queryByText(/cut off before it finished/i)).toBeNull())
    expect(h.sendMessage.mock.calls[1][3]).toEqual({ regenerate: true })
    expect(creates()).toHaveLength(1) // the create was never re-fired
  })

  it('an empty stalled reply (no partial) still drops the blank bubble', async () => {
    h.error = 'The response stalled. Check your connection and try again.'
    h.getConversation.mockResolvedValue(null)
    h.sendMessage.mockResolvedValue(null) // stalls before the first delta
    renderChat('/chat/chat-1?projectId=p1&kind=planning')

    const textarea = await screen.findByPlaceholderText(/Describe what you're thinking/i)
    fireEvent.change(textarea, { target: { value: 'plan my app' } })
    fireEvent.keyDown(textarea, { key: 'Enter' })
    await waitFor(() => expect(h.sendMessage).toHaveBeenCalledTimes(1))

    expect(screen.queryByText(/cut off before it finished/i)).toBeNull()
  })
})

describe('ChatPage — Regenerate after a stall (F1)', () => {
  it('re-requests the reply once, without re-posting the user turn', async () => {
    h.error = 'The response stalled. Check your connection and try again.'
    h.getConversation.mockResolvedValue(null)
    h.sendMessage.mockResolvedValueOnce(null) // the first turn stalls (a falsy result)
    h.sendMessage.mockResolvedValueOnce('the retried reply') // the regenerate succeeds
    renderChat('/chat/chat-1?projectId=p1&kind=planning')

    const textarea = await screen.findByPlaceholderText(/Describe what you're thinking/i)
    fireEvent.change(textarea, { target: { value: 'plan my app' } })
    fireEvent.keyDown(textarea, { key: 'Enter' })

    await waitFor(() => expect(h.sendMessage).toHaveBeenCalledTimes(1))
    expect(creates()).toHaveLength(1) // the row was created once

    // The error banner offers an explicit, user-initiated "Try again".
    const retry = await screen.findByRole('button', { name: /try again/i })
    fireEvent.click(retry)

    await waitFor(() => expect(h.sendMessage).toHaveBeenCalledTimes(2)) // regenerate fired once
    expect(h.sendMessage.mock.calls[1][3]).toEqual({ regenerate: true }) // no user-turn duplicate
    expect(creates()).toHaveLength(1) // the create was NOT re-fired
  })

  it('Try again after navigating to another chat does NOT re-fire the previous chat\'s turn', async () => {
    // ChatPage stays mounted across chat navigations, so a chat-1 stall's Regenerate context must
    // not leak onto chat-2 — clearing it on navigation stops a phantom bubble + a discarded bill.
    h.error = 'The response stalled. Check your connection and try again.'
    h.getConversation.mockImplementation(async (id) => ({
      id, kind: 'planning', title: id, messages: [], updatedAt: new Date().toISOString(),
    }))
    h.sendMessage.mockResolvedValue(null) // chat-1's turn stalls
    h.listProjectConversations.mockResolvedValue([
      { id: 'chat-1', kind: 'planning', title: 'First', updatedAt: new Date().toISOString() },
      { id: 'chat-2', kind: 'planning', title: 'Second', updatedAt: new Date(Date.now() - 1000).toISOString() },
    ])
    renderChat('/chat/chat-1')

    const textarea = await screen.findByPlaceholderText(/Describe what you're thinking/i)
    fireEvent.change(textarea, { target: { value: 'hi from chat 1' } })
    fireEvent.keyDown(textarea, { key: 'Enter' })
    await waitFor(() => expect(h.sendMessage).toHaveBeenCalledTimes(1))

    // Navigate to chat-2, then click the (statically-mocked) Try again banner.
    fireEvent.click(screen.getByText('Second'))
    await waitFor(() => expect(h.getConversation).toHaveBeenCalledWith('chat-2'))
    fireEvent.click(await screen.findByRole('button', { name: /try again/i }))
    await act(async () => { await Promise.resolve() })

    expect(h.sendMessage).toHaveBeenCalledTimes(1) // chat-1's turn was NOT re-fired into chat-2
  })
})

// N1 (U3), the ChatPage half. This page carries `initialMessage` rather than `prompt` and fires
// it through `fireMessage`, but the mechanism is byte-identical: `window.history.replaceState`
// strips the browser entry without a popstate, and the shared `useDropTransientQuery` used to
// write react-router's surviving in-memory state straight back. One shared hook, two readers —
// so the guard is pinned on both surfaces, not just the one where the bug was first seen.
describe('ChatPage — the initialMessage hand-off does not replay on reload (N1)', () => {
  const HANDOFF = {
    pathname: '/chat/c-new',
    search: '?projectId=p1&kind=planning',
    state: { initialMessage: 'what can this platform do?' },
  }

  function StateProbe() {
    const loc = useLocation()
    return <div data-testid="router-state">{JSON.stringify(loc.state)}</div>
  }

  const renderAt = (entry) =>
    render(
      <MemoryRouter initialEntries={[entry]}>
        <LocationProbe />
        <StateProbe />
        <Routes>
          <Route path="/chat/:chatId" element={<ChatPage projectId="p1" projectName="VIP Movement" />} />
          <Route path="/projects" element={<div>projects index</div>} />
        </Routes>
      </MemoryRouter>,
    )

  it('drops the hand-off with the query, so a remount fires no second turn', async () => {
    h.getConversation.mockResolvedValue(null)
    renderAt(HANDOFF)

    await waitFor(() => expect(h.sendMessage).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(screen.getByTestId('location').textContent).toBe('/chat/c-new'))
    expect(screen.getByTestId('router-state').textContent).toBe('null')

    // The reload: a fresh mount over the entry the drop left, with the server row now present.
    h.getConversation.mockResolvedValue({
      id: 'c-new',
      messages: [{ id: 'm0', role: 'user', seq: 0, parts: [{ type: 'text', text: 'what can this platform do?' }] }],
    })
    cleanup()
    h.sendMessage.mockClear()

    renderAt({ pathname: '/chat/c-new', search: '', state: null })
    await act(async () => { await Promise.resolve() })
    expect(h.sendMessage).not.toHaveBeenCalled()
  })
})
