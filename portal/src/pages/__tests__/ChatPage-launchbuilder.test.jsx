/**
 * The planning chat's "Launch Builder" handoff lands in the CANONICAL thread (003-U1).
 *
 * THE REGRESSION THIS PINS. This handoff used to mint a NEW builder conversation. Once a
 * project's thread is "the newest builder-kind row", that mint silently HIJACKS it: the fresh
 * empty chat becomes the project's thread, and the transcript the user had already built up —
 * interview turns, briefs, build outcomes — is orphaned in a row nothing routes to. Nothing
 * errors, nothing looks broken; the work is just gone. So the test that matters is not "does it
 * navigate" but "does it navigate WITHOUT minting".
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
  resolveBuilderThread: vi.fn(),
}))

vi.mock('../../hooks/useClaudeAPI', () => ({
  useClaudeAPI: () => ({ sendMessage: h.sendMessage, error: null }),
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
vi.mock('../../utils/builderThreadApi', () => ({ resolveBuilderThread: h.resolveBuilderThread }))
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

/**
 * Drive the modal chain: toolbar "Build This App" → suggestion modal's "Build This App"
 * (which fires the summarize round-trip) → the builder-prompt modal.
 *
 * The toolbar and the modal share a button name, so the second click is disambiguated by
 * position — the modal's is the LAST match once the modal is open.
 */
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
  h.newConversation.mockReturnValue('SHOULD-NOT-BE-MINTED')
  // The summarize round-trip that fills the builder-prompt textarea.
  h.sendMessage.mockImplementation(async (_m, onChunk) => {
    onChunk('Build an application for BIAL that tracks visitors.')
    return 'Build an application for BIAL that tracks visitors.'
  })
})
afterEach(cleanup)

describe('ChatPage → Launch Builder', () => {
  it('lands in the canonical thread and mints NO new builder conversation', async () => {
    h.resolveBuilderThread.mockResolvedValue({ id: 'thread-1', projectId: 'p1', kind: 'builder' })
    renderChat()
    await openBuilderPrompt()

    fireEvent.click(screen.getByRole('button', { name: /launch builder/i }))

    await waitFor(() => expect(screen.getByTestId('location').textContent).toBe('/chat/thread-1'))
    expect(h.resolveBuilderThread).toHaveBeenCalledWith('p1')
    // The hijack guard: no fresh id was minted for a builder chat.
    expect(h.newConversation).not.toHaveBeenCalled()
    expect(screen.getByTestId('location').textContent).not.toContain('SHOULD-NOT-BE-MINTED')
  })

  it('stages the summarized brief as a draft for the thread to confirm', async () => {
    h.resolveBuilderThread.mockResolvedValue({ id: 'thread-1', projectId: 'p1', kind: 'builder' })
    renderChat()
    await openBuilderPrompt()

    fireEvent.click(screen.getByRole('button', { name: /launch builder/i }))

    await waitFor(() => expect(screen.getByTestId('location').textContent).toBe('/chat/thread-1'))
    expect(JSON.parse(screen.getByTestId('state').textContent)).toMatchObject({
      prompt: 'Build an application for BIAL that tracks visitors.',
    })
  })

  it('keeps the brief on screen when the handoff fails', async () => {
    h.resolveBuilderThread.mockRejectedValue(new Error('network'))
    renderChat()
    await openBuilderPrompt()

    fireEvent.click(screen.getByRole('button', { name: /launch builder/i }))

    expect(await screen.findByText(/could not open this project’s build chat/i)).toBeTruthy()
    // The brief cost a model call to produce — a failed hop must not discard it. The modal
    // stays open with the text intact and the button usable again.
    expect(screen.getByDisplayValue(/build an application for bial/i)).toBeTruthy()
    expect(screen.getByRole('button', { name: /launch builder/i }).disabled).toBe(false)
    // And we never left the planning chat — no navigation happened.
    expect(screen.queryByTestId('location')).toBeNull()
  })
})
