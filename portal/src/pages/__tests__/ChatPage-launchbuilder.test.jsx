/**
 * The planning chat's "Launch Builder" handoff MINTS A FRESH conversation (U13).
 *
 * The canonical-thread model this test used to pin is retired with the unified chat:
 * every launch now creates a new Plan-mode builder chat, carrying the summarized brief as
 * its first message. What matters now: the minted id is fresh (never a reused thread),
 * the query names the project + kind, and the state carries `{ prompt, mode: 'plan' }`.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react'
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom'

const h = vi.hoisted(() => ({
  sendMessage: vi.fn(),
  loadHistory: vi.fn(),
  newConversation: vi.fn(),
  appendMessage: vi.fn(),
  getConversation: vi.fn(),
  deleteConversation: vi.fn(),
  listProjectConversations: vi.fn(),
  uuidv7: vi.fn(),
}))

const FIXED_UUID = '01900000-0000-7000-8000-000000000000'

vi.mock('../../hooks/useClaudeAPI', () => ({
  useClaudeAPI: () => ({ sendMessage: h.sendMessage, error: null, clearError: vi.fn(), abort: vi.fn() }),
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
// `uuidv7` is the shared mint the Launch-Builder handoff now calls (ADR-0006: the client-minted
// id becomes the conversation's primary key, so it must be a v7, not `crypto.randomUUID`'s v4).
vi.mock('../../utils/conversationApi', () => ({
  listProjectConversations: h.listProjectConversations,
  uuidv7: h.uuidv7,
}))
vi.mock('../../components/layout/Navbar', () => ({ default: () => null }))
vi.mock('../../components/chat/MessageContent', () => ({ default: () => null }))

import ChatPage from '../ChatPage'

function LocationProbe() {
  const loc = useLocation()
  return (
    <>
      <div data-testid="location">{loc.pathname + loc.search}</div>
      <div data-testid="state">{JSON.stringify(loc.state)}</div>
    </>
  )
}

const renderChat = () =>
  render(
    <MemoryRouter initialEntries={['/chat/plan-1']}>
      <Routes>
        <Route path="/chat/plan-1" element={<ChatPage chatId="plan-1" projectId="p1" />} />
        <Route path="*" element={<LocationProbe />} />
      </Routes>
    </MemoryRouter>,
  )

async function openBuilderPrompt() {
  fireEvent.click(await screen.findByRole('button', { name: /build this app/i }))
  const inModal = await screen.findByText(/ready to build this app\?/i)
  const modal = inModal.closest('div.bg-white')
  fireEvent.click(within(modal).getByRole('button', { name: /build this app/i }))
  await screen.findByRole('button', { name: /launch builder/i })
}

beforeEach(() => {
  Element.prototype.scrollIntoView = vi.fn() // jsdom has no layout (suite-wide convention)
  Object.values(h).forEach((fn) => fn.mockReset())
  h.loadHistory.mockResolvedValue([])
  h.listProjectConversations.mockResolvedValue([])
  h.uuidv7.mockReturnValue(FIXED_UUID)
  h.getConversation.mockResolvedValue({
    id: 'plan-1',
    messages: [{ id: 'm1', role: 'user', parts: [{ type: 'text', text: 'a visitor app' }], seq: 0 }],
  })
  h.sendMessage.mockImplementation(async (_m, onChunk) => {
    onChunk('Build an application for BIAL that tracks visitors.')
    return 'Build an application for BIAL that tracks visitors.'
  })
})
afterEach(cleanup)

describe('ChatPage → Launch Builder', () => {
  it('mints a FRESH Plan-mode builder chat under the project', async () => {
    renderChat()
    await openBuilderPrompt()

    fireEvent.click(screen.getByRole('button', { name: /launch builder/i }))

    await waitFor(() => expect(screen.queryByTestId('location')).toBeTruthy())
    const location = screen.getByTestId('location').textContent
    // A fresh id from the SHARED mint — never the planning chat's own id, never a resolved old
    // thread, and never an inline `crypto.randomUUID()` (which the stubbed mint cannot produce).
    expect(location).toBe(`/chat/${FIXED_UUID}?projectId=p1&kind=builder`)
    expect(location).not.toContain('plan-1')
  })

  it('stages the summarized brief + plan mode in the handoff state', async () => {
    renderChat()
    await openBuilderPrompt()

    fireEvent.click(screen.getByRole('button', { name: /launch builder/i }))

    await waitFor(() => expect(screen.queryByTestId('state')).toBeTruthy())
    const state = JSON.parse(screen.getByTestId('state').textContent)
    expect(state).toMatchObject({
      prompt: 'Build an application for BIAL that tracks visitors.',
      mode: 'plan',
      // ChatRoute skips this chat's guaranteed-404 GET only because the marker is here.
      freshlyMinted: true,
    })
    // toMatchObject passes on EXTRA keys, so it cannot see `theme` coming back. The consumer
    // side (ProjectBuilder) asserts its absence explicitly; this is the producer, and the pair
    // is only symmetric with this line (#157 B1 / review).
    expect(state.theme).toBeUndefined()
  })
})

describe('ChatPage → New Chat', () => {
  it('marks the minted chat freshlyMinted so ChatRoute skips its guaranteed 404', async () => {
    // The sidebar's New Chat is the THIRD mint site. Its row does not exist until the send path
    // creates it either, so it needs the same marker — and the marker rides in router state,
    // which dies on reload, so a later shared link still resolves against the server.
    h.newConversation.mockReturnValue('fresh-plan-id')
    renderChat()

    fireEvent.click(await screen.findByRole('button', { name: /new chat/i }))

    await waitFor(() => expect(screen.queryByTestId('location')).toBeTruthy())
    expect(screen.getByTestId('location').textContent).toBe('/chat/fresh-plan-id?projectId=p1&kind=planning')
    expect(JSON.parse(screen.getByTestId('state').textContent)).toEqual({ freshlyMinted: true })
  })
})
