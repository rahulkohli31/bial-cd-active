/**
 * The chat panel can be hidden so the preview can take the full cockpit width (#42 chat-collapse).
 * The toggle lives in the cockpit bar — not inside the chat panel it controls — so it stays
 * reachable even while that panel is invisible; these tests pin both halves of that contract, plus
 * that the panel is CSS-hidden (not unmounted), so the composer draft survives a hide/show cycle.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, cleanup, fireEvent } from '@testing-library/react'
import { renderBuilder, composer } from './_builderSession.jsx'

const h = vi.hoisted(() => ({
  loadBuilds: vi.fn(), appendBuilderMessage: vi.fn(), getBuild: vi.fn(),
  deleteBuild: vi.fn(), listProjectConversations: vi.fn(), buildUserParts: vi.fn(),
  sendMessage: vi.fn(),
}))

vi.mock('../../utils/builderHistory', () => ({
  loadBuilds: h.loadBuilds, appendBuilderMessage: h.appendBuilderMessage,
  getBuild: h.getBuild, deleteBuild: h.deleteBuild, deriveTitle: (t) => (t || '').slice(0, 40),
}))
vi.mock('../../utils/conversationApi', () => ({ listProjectConversations: h.listProjectConversations }))
vi.mock('../../utils/chatHistory', () => ({ relativeTime: () => 'now' }))
vi.mock('../../components/layout/Navbar', () => ({ default: () => null }))
vi.mock('../../utils/attachmentStore', async (orig) => ({ ...(await orig()), buildUserParts: h.buildUserParts }))
vi.mock('../../hooks/useClaudeAPI', () => ({
  useClaudeAPI: () => ({ sendMessage: h.sendMessage, error: null, clearError: vi.fn() }),
}))

beforeEach(() => {
  vi.clearAllMocks()
  Element.prototype.scrollIntoView = vi.fn()
  h.appendBuilderMessage.mockResolvedValue({ ok: true })
  h.getBuild.mockResolvedValue(null)
  h.loadBuilds.mockResolvedValue([])
  h.listProjectConversations.mockResolvedValue([])
  h.buildUserParts.mockImplementation(async (text) => [{ type: 'text', text }])
})
afterEach(() => cleanup())

async function renderReady() {
  renderBuilder()
  await screen.findByRole('button', { name: /hide chat panel/i }) // panel has rendered
}

describe('BuilderPage — hideable chat panel (#42)', () => {
  it('hides the chat panel on toggle and flips the button label, then shows it again', async () => {
    await renderReady()

    fireEvent.click(screen.getByRole('button', { name: /hide chat panel/i }))
    expect(screen.getByRole('button', { name: /show chat panel/i })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /hide chat panel/i })).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: /show chat panel/i }))
    expect(screen.getByRole('button', { name: /hide chat panel/i })).toBeTruthy()
  })

  it('keeps the composer draft when the panel is hidden and re-shown (CSS-hide, not unmount)', async () => {
    await renderReady()

    fireEvent.change(composer(), { target: { value: 'a visitor pass tracker' } })
    expect(composer().value).toBe('a visitor pass tracker')

    fireEvent.click(screen.getByRole('button', { name: /hide chat panel/i })) // hide
    fireEvent.click(screen.getByRole('button', { name: /show chat panel/i })) // show

    expect(composer().value).toBe('a visitor pass tracker')
  })

  it('the toggle stays reachable while the panel is hidden', async () => {
    await renderReady()

    fireEvent.click(screen.getByRole('button', { name: /hide chat panel/i }))
    const shown = screen.getByRole('button', { name: /show chat panel/i })
    expect(shown).toBeTruthy()

    fireEvent.click(shown)
    expect(screen.getByRole('button', { name: /hide chat panel/i })).toBeTruthy()
  })
})
