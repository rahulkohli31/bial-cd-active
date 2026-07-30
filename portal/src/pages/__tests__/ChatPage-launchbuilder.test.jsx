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
}))

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
vi.mock('../../utils/conversationApi', () => ({ listProjectConversations: h.listProjectConversations }))
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
    // A fresh UUID path — never the planning chat's own id, never a resolved old thread.
    expect(location).toMatch(/^\/chat\/[0-9a-f-]{36}\?projectId=p1&kind=builder$/)
    expect(location).not.toContain('plan-1')
  })

  it('stages the summarized brief + plan mode in the handoff state', async () => {
    renderChat()
    await openBuilderPrompt()

    fireEvent.click(screen.getByRole('button', { name: /launch builder/i }))

    await waitFor(() => expect(screen.queryByTestId('state')).toBeTruthy())
    expect(JSON.parse(screen.getByTestId('state').textContent)).toMatchObject({
      prompt: 'Build an application for BIAL that tracks visitors.',
      mode: 'plan',
    })
  })
})
