/**
 * The plan chat's "Launch Builder" handoff MINTS A FRESH conversation (U13).
 *
 * The canonical-thread model this test used to pin is retired with the unified chat:
 * every launch now creates a new build chat (`?kind=build`), carrying the summarized brief
 * as its first message. What matters now: the minted id is fresh (never a reused thread),
 * the query names the project + kind, and the state carries `{ prompt, uploadedFiles,
 * freshlyMinted }`. `mode` does NOT ride along any more — `ConversationMode` (ask/plan/write)
 * was a second axis the composer switched independently of the stored kind; U19 deleted it
 * along with the composer's mode-switching control, since a chat's kind is now fixed once, at
 * the mint.
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
  // The context guardrail's two inputs. `warn` and `full` are the only states in which the
  // planning surface still offers a new-chat control at all, now that the sidebar's is gone.
  contextLimits: { soft: 1e9, hard: 1e9 },
  conversationTokens: 0,
}))

const FIXED_UUID = '01900000-0000-7000-8000-000000000000'

vi.mock('../../hooks/useClaudeAPI', () => ({
  useClaudeAPI: () => ({ sendMessage: h.sendMessage, error: null, clearError: vi.fn(), abort: vi.fn() }),
  getContextLimits: () => h.contextLimits,
  estimateConversationTokens: () => h.conversationTokens,
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
  // Guarded: `h` now also carries two plain guardrail knobs, which have no mockReset.
  Object.values(h).forEach((fn) => { if (typeof fn?.mockReset === 'function') fn.mockReset() })
  h.contextLimits = { soft: 1e9, hard: 1e9 }
  h.conversationTokens = 0
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
  it('mints a FRESH build chat under the project', async () => {
    renderChat()
    await openBuilderPrompt()

    fireEvent.click(screen.getByRole('button', { name: /launch builder/i }))

    await waitFor(() => expect(screen.queryByTestId('location')).toBeTruthy())
    const location = screen.getByTestId('location').textContent
    // A fresh id from the SHARED mint — never the plan chat's own id, never a resolved old
    // thread, and never an inline `crypto.randomUUID()` (which the stubbed mint cannot produce).
    expect(location).toBe(`/chat/${FIXED_UUID}?projectId=p1&kind=build`)
    expect(location).not.toContain('plan-1')
  })

  it('stages the summarized brief in the handoff state — no mode field rides along', async () => {
    renderChat()
    await openBuilderPrompt()

    fireEvent.click(screen.getByRole('button', { name: /launch builder/i }))

    await waitFor(() => expect(screen.queryByTestId('state')).toBeTruthy())
    const state = JSON.parse(screen.getByTestId('state').textContent)
    expect(state).toMatchObject({
      prompt: 'Build an application for BIAL that tracks visitors.',
      // ChatRoute skips this chat's guaranteed-404 GET only because the marker is here.
      freshlyMinted: true,
    })
    // `mode` USED TO ride here too (`{ prompt, mode: 'plan' }`) — it is gone from the payload
    // entirely, not renamed: ConversationMode was a second axis (ask/plan/write) the composer
    // switched independently of the stored kind, and U19 retired it along with the control that
    // switched it. The chat's kind is fixed once, at the mint, via `?kind=build` alone.
    expect(state.mode).toBeUndefined()
    // toMatchObject passes on EXTRA keys, so it cannot see `theme` coming back. The consumer
    // side (ProjectBuilder) asserts its absence explicitly; this is the producer, and the pair
    // is only symmetric with this line (#157 B1 / review).
    expect(state.theme).toBeUndefined()
  })
})

describe('ChatPage → New Chat, now minted from the context guardrail (R54)', () => {
  it('the sidebar\'s New Chat is gone, and no other new-chat control stands in an ordinary chat', async () => {
    // The first half of the removal, as an inertness guard. What is NOT gone is the handler: it is
    // the third mint site and the only producer of planning conversations, and it moved from
    // sidebar chrome to the one place it is genuinely load-bearing.
    renderChat()
    await screen.findByPlaceholderText(/Describe what you're thinking/i)

    expect(screen.queryByRole('button', { name: /new chat/i })).toBeNull()
  })

  it('marks the minted chat freshlyMinted so ChatRoute skips its guaranteed 404', async () => {
    // RE-POINTED, NOT DELETED. The mint is the same handler; only its trigger moved. Driving it
    // from the guardrail is the stronger test anyway: this is the state in which a conversation
    // has NO other way forward, so the marker mattering here is what stops a dead end becoming a
    // dead end with a broken exit.
    h.contextLimits = { soft: 10, hard: 20 }
    h.conversationTokens = 999
    h.newConversation.mockReturnValue('fresh-plan-id')
    renderChat()

    fireEvent.click(await screen.findByRole('button', { name: /start new chat/i }))

    await waitFor(() => expect(screen.queryByTestId('location')).toBeTruthy())
    expect(screen.getByTestId('location').textContent).toBe('/chat/fresh-plan-id?projectId=p1&kind=plan')
    expect(JSON.parse(screen.getByTestId('state').textContent)).toEqual({ freshlyMinted: true })
  })
})
