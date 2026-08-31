/**
 * CHARACTERIZATION — the planning surface as it behaves today (Plan A, U1).
 *
 * Two things are recorded here and they are recorded for opposite reasons.
 *
 * THE DRAFT, WRITTEN DOWN AS THE DEFECT IT IS. R72 asks for one conversation surface whose draft
 * survives the same way in each kind. It does not today: the builder surface keeps its text in
 * `composerDraft`'s `sessionStorage` store (`BuilderPage.tsx:785`/`:1512`/`:2478`), while the
 * planning surface holds it only in assistant-ui's in-memory external-store composer and CLEARS it
 * on every chat change *including the first mount after a reload* (`ChatPage.tsx:226`). So a
 * planning draft dies on a reload and on a round trip to a sibling chat.
 *
 * These two scenarios are written AS THEY ARE, deliberately, so that U5's change to them shows up
 * in review as a diff on an expectation rather than as a silently rewritten one. U5 is the only
 * unit permitted to flip them, and it says so.
 *
 * THE CONTEXT GUARDRAIL, WRITTEN DOWN AS THE THING U6 MUST NOT TAKE WITH IT. At `ctxLevel ===
 * 'full'` the send is a hard stop (`ChatPage.tsx:488`) whose ONLY escape is the two "start a new
 * chat" controls — and those call `handleNewChat`, which is also the sidebar's New Chat button.
 * U6 deletes the sidebar. Deleting the handler with it would leave a planning conversation
 * permanently unsendable, with copy still telling the reader to start a new chat and no control to
 * do it. That is the dead-UI removal trace inverted: the control gone and the sentence left. These
 * scenarios must stay green through U6 unchanged.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react'
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom'

const h = vi.hoisted(() => ({
  sendMessage: vi.fn(), abort: vi.fn(), clearError: vi.fn(),
  loadHistory: vi.fn(), newConversation: vi.fn(), createConversation: vi.fn(),
  getConversation: vi.fn(), deleteConversation: vi.fn(), listProjectConversations: vi.fn(),
  // The guardrail's two inputs, driven per test. `full` is the state whose only exit is the
  // control U6 must keep.
  contextLimits: { soft: 1e9, hard: 1e9 },
  conversationTokens: 0,
}))

vi.mock('../../hooks/useClaudeAPI', () => ({
  useClaudeAPI: () => ({ sendMessage: h.sendMessage, error: null, clearError: h.clearError, abort: h.abort }),
  getContextLimits: () => h.contextLimits,
  estimateConversationTokens: () => h.conversationTokens,
}))
vi.mock('../../utils/chatHistory', () => ({
  loadHistory: h.loadHistory,
  newConversation: h.newConversation,
  createConversation: h.createConversation,
  getConversation: h.getConversation,
  deleteConversation: h.deleteConversation,
  relativeTime: () => 'now',
  deriveTitle: (t: string) => (t || '').slice(0, 40),
}))
vi.mock('../../components/layout/Navbar', () => ({ default: () => null }))
vi.mock('../../utils/conversationApi', () => ({
  listProjectConversations: h.listProjectConversations,
  uuidv7: () => '01900000-0000-7000-8000-000000000000',
}))

import ChatPage from '../ChatPage'

function LocationProbe() {
  const loc = useLocation()
  return <div data-testid="location">{`${loc.pathname}${loc.search}`}</div>
}

/** Mount the planning surface at a flat chat URL, exactly as `ChatRoute` renders it. */
function renderChat(entry = '/chat/chat-1') {
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

const composer = () => screen.getByPlaceholderText(/Describe what you're thinking/i) as HTMLTextAreaElement
const here = () => screen.getByTestId('location').textContent

/**
 * The control that sits BESIDE a guardrail sentence.
 *
 * Scoped to the banner rather than queried globally, and that scoping is the assertion: the
 * sidebar carries a "New Chat" of its own today, so a global query would match either and would go
 * on passing after U6 deleted the sidebar's — or, worse, would have been passing on the sidebar's
 * all along. What R54 must not cost is the escape hatch, and the escape hatch is the button in
 * this banner.
 */
const escapeHatchBesides = (sentence: RegExp) => {
  const banner = screen.getByText(sentence).closest('div')
  if (!banner) throw new Error('the guardrail sentence is not inside a banner')
  return within(banner).getByRole('button')
}

beforeEach(() => {
  vi.clearAllMocks()
  sessionStorage.clear()
  Element.prototype.scrollIntoView = vi.fn()
  h.contextLimits = { soft: 1e9, hard: 1e9 }
  h.conversationTokens = 0
  h.loadHistory.mockResolvedValue([])
  h.listProjectConversations.mockResolvedValue([])
  h.getConversation.mockResolvedValue(null)
  h.newConversation.mockReturnValue('chat-fresh')
  h.createConversation.mockResolvedValue({ id: 'chat-1', kind: 'planning', mode: 'plan' })
  h.deleteConversation.mockResolvedValue(true)
})
afterEach(() => cleanup())

describe('ChatPage — the composer draft, as it behaves TODAY (U5 changes both of these)', () => {
  it('a typed draft is LOST on a reload', async () => {
    // The reload is modelled the only way a jsdom test can model one: unmount everything and mount
    // the same URL again with nothing carried over but storage. That is exactly what a reload is
    // for this page — assistant-ui's composer store is in-memory, so it starts empty either way,
    // and `ChatPage.tsx:226` then clears it again on the first hydration.
    const first = renderChat('/chat/chat-1')
    await screen.findByText(/Plan your next app/i)
    fireEvent.change(composer(), { target: { value: 'a visitor pass tracker' } })
    expect(composer().value).toBe('a visitor pass tracker')

    first.unmount()
    renderChat('/chat/chat-1')
    await screen.findByText(/Plan your next app/i)

    // TODAY: gone. U5 makes this survive by reading `composerDraft` for the routed chat instead of
    // clearing unconditionally, and flips this expectation.
    expect(composer().value).toBe('')
  })

  it('a typed draft is LOST on a round trip to a sibling conversation', async () => {
    h.listProjectConversations.mockResolvedValue([
      { id: 'chat-1', kind: 'planning', title: 'First', updatedAt: new Date().toISOString() },
      { id: 'chat-2', kind: 'planning', title: 'Second', updatedAt: new Date(Date.now() - 1000).toISOString() },
    ])
    h.getConversation.mockImplementation(async (id: string) => ({
      id, kind: 'planning', title: id, messages: [], updatedAt: new Date().toISOString(),
    }))
    renderChat('/chat/chat-1')
    await screen.findByText(/Plan your next app/i)
    fireEvent.change(composer(), { target: { value: 'a visitor pass tracker' } })

    fireEvent.click(screen.getByText('Second'))
    await waitFor(() => expect(h.getConversation).toHaveBeenCalledWith('chat-2'))
    expect(composer().value).toBe('') // correct in both directions: chat-1's text must not leak here

    fireEvent.click(screen.getByText('First'))
    await waitFor(() => expect(h.getConversation).toHaveBeenCalledWith('chat-1'))

    // TODAY: chat-1's own text is gone too, because the clear is unconditional. U5 makes each
    // conversation show its own draft, which is the half of this the builder surface already has.
    expect(composer().value).toBe('')
  })
})

describe('ChatPage — the context guardrail and its only escape hatch (U6 must not remove this)', () => {
  /** Put the conversation over the hard window, which is the state with the hard send stop. */
  const atTheWindow = () => {
    h.contextLimits = { soft: 10, hard: 20 }
    h.conversationTokens = 999
  }

  it('at the window, the hard stop refuses the send and the sentence is on screen', async () => {
    atTheWindow()
    renderChat('/chat/chat-1')
    await screen.findByText(/Plan your next app/i)

    expect(screen.getByText(/reached its maximum length/i)).toBeTruthy()

    fireEvent.change(composer(), { target: { value: 'one more thing' } })
    fireEvent.keyDown(composer(), { key: 'Enter' })

    // Enforced at `doSend`, not merely dimmed: the textarea is never disabled on this page.
    expect(h.sendMessage).not.toHaveBeenCalled()
    expect(h.createConversation).not.toHaveBeenCalled()
  })

  it('the sentence has a LIVE control beside it, and it mints a planning chat in the same project', async () => {
    atTheWindow()
    renderChat('/chat/chat-1')
    await screen.findByText(/reached its maximum length/i)

    fireEvent.click(escapeHatchBesides(/reached its maximum length/i))

    // The third mint site, and the only producer of `kind=planning` conversations — the project
    // screen's own composer mints `kind=builder`. Losing it would close the only door out of the
    // dead end above.
    await waitFor(() => expect(here()).toBe('/chat/chat-fresh?projectId=p1&kind=planning'))
  })

  it('the softer warning also carries a live control', async () => {
    h.contextLimits = { soft: 10, hard: 1e9 }
    h.conversationTokens = 999
    renderChat('/chat/chat-1')
    await screen.findByText(/getting long/i)

    fireEvent.click(escapeHatchBesides(/getting long/i))

    await waitFor(() => expect(here()).toBe('/chat/chat-fresh?projectId=p1&kind=planning'))
  })
})
