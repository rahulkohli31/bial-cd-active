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

  it('keeps the composer draft when the panel is hidden and re-shown', async () => {
    // NOTE: this alone does NOT prove CSS-hide over unmount — `input` is BuilderPage's own
    // state and the composer is a controlled textarea, so a conditionally-unmounted panel
    // would pass this identically (React repopulates `value` from the same state on
    // remount). The tests below are the ones that actually discriminate the two.
    await renderReady()

    fireEvent.change(composer(), { target: { value: 'a visitor pass tracker' } })
    expect(composer().value).toBe('a visitor pass tracker')

    fireEvent.click(screen.getByRole('button', { name: /hide chat panel/i })) // hide
    fireEvent.click(screen.getByRole('button', { name: /show chat panel/i })) // show

    expect(composer().value).toBe('a visitor pass tracker')
  })

  it('the panel itself CSS-collapses (its width class swaps), not just the toggle label', async () => {
    await renderReady()
    const panel = screen.getByTestId('chat-panel')
    expect(panel.className).toMatch(/w-72/)

    fireEvent.click(screen.getByRole('button', { name: /hide chat panel/i }))
    expect(panel.className).toMatch(/w-0/)
    expect(panel.className).not.toMatch(/w-72/)

    fireEvent.click(screen.getByRole('button', { name: /show chat panel/i }))
    expect(panel.className).toMatch(/w-72/)
  })

  it('keeps scroll position across a hide/show cycle — the property that actually discriminates CSS-hide from unmount (mutation-checked: fails against a conditionally-unmounted panel, since a freshly mounted node has no prior scrollTop)', async () => {
    await renderReady()
    const before = screen.getByTestId('chat-messages')
    before.scrollTop = 40

    fireEvent.click(screen.getByRole('button', { name: /hide chat panel/i }))
    fireEvent.click(screen.getByRole('button', { name: /show chat panel/i }))

    // Re-query rather than reuse `before` — reusing it would still read 40 even if the panel
    // WAS unmounted (the stale, detached node keeps whatever was last set on it), which is
    // exactly the false-pass the composer-draft test above has.
    const after = screen.getByTestId('chat-messages')
    expect(after.scrollTop).toBe(40)
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
