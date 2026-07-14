/**
 * Regression carried over from the single-file era: the assistant's build turn must be visible
 * WITHOUT a page refresh. Re-expressed against the session model — the build narrative is the
 * activity feed + a live status line, pushed to visible React state up front (never a remount);
 * and while the loop keeps iterating AFTER preview_ready, the live preview is NOT blanked (KTD-8b).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, waitFor, act, cleanup } from '@testing-library/react'
import {
  FakeEventSource, PREVIEW_URL, makeClient, primeClient, renderBuilder, STEP, PREVIEW,
} from './_builderSession.jsx'

const h = vi.hoisted(() => ({
  loadBuilds: vi.fn(), newBuild: vi.fn(), appendBuilderMessage: vi.fn(), getBuild: vi.fn(),
  deleteBuild: vi.fn(), listProjectConversations: vi.fn(), buildUserParts: vi.fn(),
  start: vi.fn(), stop: vi.fn(), getStatus: vi.fn(), forceEnd: vi.fn(),
  acquireLock: vi.fn(), renewLock: vi.fn(), releaseLock: vi.fn(), heartbeat: vi.fn(),
}))

vi.mock('../../utils/builderHistory', () => ({
  loadBuilds: h.loadBuilds, newBuild: h.newBuild, appendBuilderMessage: h.appendBuilderMessage,
  getBuild: h.getBuild, deleteBuild: h.deleteBuild, deriveTitle: (t) => (t || '').slice(0, 40),
}))
vi.mock('../../utils/conversationApi', () => ({ listProjectConversations: h.listProjectConversations }))
vi.mock('../../utils/chatHistory', () => ({ relativeTime: () => 'now' }))
vi.mock('../../components/layout/Navbar', () => ({ default: () => null }))
vi.mock('../../utils/attachmentStore', async (orig) => ({ ...(await orig()), buildUserParts: h.buildUserParts }))

function deps() {
  const fake = new FakeEventSource('x')
  return { fake, deps: { client: makeClient(h), eventSourceFactory: () => fake } }
}

async function send(text = 'build me a tool') {
  const textarea = await screen.findByPlaceholderText(/Type instructions/i)
  fireEvent.change(textarea, { target: { value: text } })
  fireEvent.keyDown(textarea, { key: 'Enter' })
  await waitFor(() => expect(h.start).toHaveBeenCalled())
}

beforeEach(() => {
  vi.clearAllMocks()
  Element.prototype.scrollIntoView = vi.fn()
  primeClient(h)
  h.newBuild.mockReturnValue('build-Y')
  h.appendBuilderMessage.mockResolvedValue({ ok: true })
  h.getBuild.mockResolvedValue(null)
  h.loadBuilds.mockResolvedValue([])
  h.listProjectConversations.mockResolvedValue([{ id: 'build-X', kind: 'builder', title: 'My build', updatedAt: new Date().toISOString() }])
  h.buildUserParts.mockImplementation(async (text) => [{ type: 'text', text }])
})
afterEach(() => cleanup())

describe('BuilderPage — build turn visible without a refresh', () => {
  it('shows the live status line immediately on Send, and the feed as envelopes arrive — no remount', async () => {
    const d = deps()
    renderBuilder({ deps: d.deps })
    await send()

    // The assistant side is on screen at once (optimistic-visible-state), not after a re-hydration.
    expect(await screen.findByText(/Building your app/i)).toBeTruthy()
    expect(h.getBuild).toHaveBeenCalledTimes(1) // the single mount-time adopt — no second hydration

    act(() => { d.fake.open(); d.fake.emitEnvelope(STEP(1)) })
    expect(await screen.findByText(/Scaffolding your app/i)).toBeTruthy() // feed row in the DOM
  })

  it('flips the status line to "preview is live" once preview_ready arrives', async () => {
    const d = deps()
    renderBuilder({ deps: d.deps })
    await send()
    act(() => { d.fake.open(); d.fake.emitEnvelope(PREVIEW(3)) })
    expect(await screen.findByText(/preview is live/i)).toBeTruthy()
  })

  it('does NOT blank the live preview while the loop keeps iterating after preview_ready (KTD-8b)', async () => {
    const d = deps()
    renderBuilder({ deps: d.deps })
    await send()
    act(() => { d.fake.open(); d.fake.emitEnvelope(PREVIEW(3)) })
    await waitFor(() => expect(document.querySelector('iframe')?.getAttribute('src')).toBe(PREVIEW_URL))

    // A self-heal step AFTER the preview came up: the frame stays (same URL), the overlay shows.
    act(() => { d.fake.emitEnvelope(STEP(4)) })
    expect(document.querySelector('iframe')?.getAttribute('src')).toBe(PREVIEW_URL) // NOT blanked
    expect(await screen.findByText(/still iterating/i)).toBeTruthy()
  })
})
