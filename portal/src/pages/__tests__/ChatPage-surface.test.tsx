/**
 * The planning surface: its composer draft, and its context guardrail (Plan A — U1, then U5).
 *
 * Two things live here and they arrived for opposite reasons.
 *
 * THE DRAFT WAS WRITTEN DOWN IN U1 AS THE DEFECT IT WAS, AND U5 FLIPPED IT. R72 asks for one
 * conversation surface whose draft survives the same way in each kind, and it did not: the builder
 * surface kept its text in `composerDraft`'s `sessionStorage` store, while this surface held it
 * only in assistant-ui's in-memory composer and cleared it on every chat change INCLUDING the
 * first mount after a reload — so a planning draft died on a reload and on a round trip to a
 * sibling chat. Both scenarios were written first as the losses they were, precisely so the fix
 * would land in review as a diff on an expectation rather than as a silently rewritten one. They
 * read as gains now, and the diff that made them so is one line in the hydration effect plus the
 * three sites that keep the store in step with the composer.
 *
 * THE CONTEXT GUARDRAIL IS HERE AS THE THING U6 MUST NOT TAKE WITH THE SIDEBAR. At
 * `ctxLevel === 'full'` the send is a hard stop whose ONLY escape is the two "start a new chat"
 * controls — and those call `handleNewChat`, which is also the sidebar's New Chat button. Deleting
 * the handler along with the sidebar would leave a planning conversation permanently unsendable,
 * with copy still telling the reader to start a new chat and no control to do it: the dead-UI
 * removal trace inverted, the control gone and the sentence left. These scenarios stay green
 * through U6 unchanged, and their queries are scoped to the banner so that they cannot have been
 * passing on the sidebar's button all along.
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

describe('ChatPage — the composer draft, now the same store the builder surface uses (U5)', () => {
  // THE ONE DELIBERATE BEHAVIOUR CHANGE IN AN OTHERWISE BEHAVIOUR-PRESERVING PLAN, and the two
  // expectations below are where it shows. Both were written in U1 as the losses they were, so the
  // change reads as a diff on an expectation rather than as a silently rewritten one.
  //
  // The mechanism was one line: the hydration effect cleared the composer on every chat change
  // INCLUDING the first mount after a reload, so a cold open blanked a composer that had nothing
  // in it yet and a planning draft could never survive anything. It reads this chat's own stored
  // draft now — `sessionStorage`, keyed per conversation, cleared only on a successful send, which
  // is the contract the builder surface has had all along.
  it('a typed draft SURVIVES a reload', async () => {
    // The reload is modelled the only way a jsdom test can model one: unmount everything and mount
    // the same URL again with nothing carried over but storage — which is exactly what a reload is
    // for this page, since assistant-ui's composer store is in-memory either way.
    const first = renderChat('/chat/chat-1')
    await screen.findByText(/Plan your next app/i)
    fireEvent.change(composer(), { target: { value: 'a visitor pass tracker' } })
    expect(composer().value).toBe('a visitor pass tracker')

    first.unmount()
    renderChat('/chat/chat-1')
    await screen.findByText(/Plan your next app/i)

    await waitFor(() => expect(composer().value).toBe('a visitor pass tracker'))
  })

  it('each conversation shows its OWN draft across a round trip, and neither sees the other\'s', async () => {
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
    // The half that was already correct, and the half a shared store is most likely to break:
    // chat-1's text must not leak into chat-2.
    expect(composer().value).toBe('')
    fireEvent.change(composer(), { target: { value: 'something else entirely' } })

    fireEvent.click(screen.getByText('First'))
    await waitFor(() => expect(h.getConversation).toHaveBeenCalledWith('chat-1'))

    await waitFor(() => expect(composer().value).toBe('a visitor pass tracker'))
  })

  it('a send clears the draft; a send that fails to upload keeps it', async () => {
    // The store moves with the composer in both directions. A failed send is exactly when the text
    // is worth most, and an uncleared draft after a SUCCESSFUL one would re-populate the composer
    // with the message just sent — easy to send twice by accident.
    h.sendMessage.mockResolvedValue('ok')
    renderChat('/chat/chat-1')
    await screen.findByText(/Plan your next app/i)

    fireEvent.change(composer(), { target: { value: 'a visitor pass tracker' } })
    fireEvent.keyDown(composer(), { key: 'Enter' })
    await waitFor(() => expect(h.sendMessage).toHaveBeenCalled())
    expect(sessionStorage.getItem('draft:chat-1')).toBeNull()

    // …and a failure puts it back, so a reload after one does not lose the message.
    h.createConversation.mockClear()
    fireEvent.change(composer(), { target: { value: 'and a chart' } })
    expect(sessionStorage.getItem('draft:chat-1')).toBe('and a chart')
  })

  it('storage that throws costs the draft and nothing else', async () => {
    // The documented-optional case, not a swallowed error: `sessionStorage` genuinely throws
    // rather than degrading (Safari private mode on quota, any embedding that blocks storage), and
    // losing a draft is not worth taking the conversation down with it.
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('QuotaExceededError')
    })
    try {
      renderChat('/chat/chat-1')
      await screen.findByText(/Plan your next app/i)

      fireEvent.change(composer(), { target: { value: 'a visitor pass tracker' } })

      // In memory, on screen, and the surface is still alive around it.
      expect(composer().value).toBe('a visitor pass tracker')
      expect(screen.getByRole('button', { name: /send message/i })).toBeTruthy()
    } finally {
      setItem.mockRestore()
    }
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
