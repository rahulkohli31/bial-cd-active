/**
 * Regression carried over from the single-file era: the assistant's build turn must be visible
 * WITHOUT a page refresh. Re-expressed against the session model — the build narrative is the
 * activity feed + a live status line, pushed to visible React state up front (never a remount);
 * and while the loop keeps iterating AFTER preview_ready, the live preview is NOT blanked (KTD-8b).
 *
 * The build now begins when the user confirms the model's brief card (003-U4), not on Send — so
 * that click is the moment these tests measure immediacy from. Everything after it is unchanged:
 * the status line and feed must appear on the spot, off the live session's own state.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor, act, cleanup } from '@testing-library/react'
import {
  FakeEventSource, PREVIEW_URL, makeClient, primeClient, renderBuilder, STEP, PREVIEW,
  primeTurn, sendAndConfirm,
} from './_builderSession.jsx'

const h = vi.hoisted(() => ({
  loadBuilds: vi.fn(), newBuild: vi.fn(), createBuild: vi.fn(), getBuild: vi.fn(),
  deleteBuild: vi.fn(), listProjectConversations: vi.fn(), buildUserParts: vi.fn(),
  sendMessage: vi.fn(),
  startTurn: vi.fn(), readTurnStream: vi.fn(), buildFromPlan: vi.fn(),
  switchMode: vi.fn(), resolvePlanOptions: vi.fn(),
  start: vi.fn(), stop: vi.fn(), getStatus: vi.fn(), forceEnd: vi.fn(),
  acquireLock: vi.fn(), renewLock: vi.fn(), releaseLock: vi.fn(), heartbeat: vi.fn(),
}))

vi.mock('../../utils/builderHistory', () => ({
  loadBuilds: h.loadBuilds, newBuild: h.newBuild, createBuild: h.createBuild,
  getBuild: h.getBuild, deleteBuild: h.deleteBuild, deriveTitle: (t) => (t || '').slice(0, 40),
}))
vi.mock('../../utils/conversationApi', () => ({ listProjectConversations: h.listProjectConversations }))
vi.mock('../../utils/chatHistory', () => ({ relativeTime: () => 'now' }))
vi.mock('../../components/layout/Navbar', () => ({ default: () => null }))
vi.mock('../../utils/attachmentStore', async (orig) => ({ ...(await orig()), buildUserParts: h.buildUserParts }))
vi.mock('../../utils/turnStreamApi', async (orig) => ({
  ...(await orig()),
  startTurn: (...a) => h.startTurn(...a),
  readTurnStream: (...a) => h.readTurnStream(...a),
  buildFromPlan: (...a) => h.buildFromPlan(...a),
  switchMode: (...a) => h.switchMode(...a),
  resolvePlanOptions: (...a) => h.resolvePlanOptions(...a),
}))

function deps() {
  const fake = new FakeEventSource('x')
  return { fake, deps: { client: makeClient(h), eventSourceFactory: () => fake } }
}

/** Send a turn, confirm the brief the relay answers with, and wait until the build is underway. */
async function startBuild(text = 'build me a tool') {
  await sendAndConfirm(text)
  await waitFor(() => expect(h.buildFromPlan).toHaveBeenCalled())
}

beforeEach(() => {
  vi.clearAllMocks()
  Element.prototype.scrollIntoView = vi.fn()
  primeClient(h)
  h.newBuild.mockReturnValue('build-Y')
  h.createBuild.mockResolvedValue({ ok: true })
  h.getBuild.mockResolvedValue(null)
  h.loadBuilds.mockResolvedValue([])
  h.listProjectConversations.mockResolvedValue([{ id: 'build-X', kind: 'builder', title: 'My build', updatedAt: new Date().toISOString() }])
  h.buildUserParts.mockImplementation(async (text) => [{ type: 'text', text }])
  // A scripted relay that always answers with a ready-to-build brief, so a single send reaches the
  // card these suites confirm. Whether the model asks or briefs is pinned server-side.
  primeTurn(h)
})
afterEach(() => cleanup())

describe('BuilderPage — build turn visible without a refresh', () => {
  it('shows the live status line immediately on confirming the brief, and the feed as envelopes arrive — no remount', async () => {
    const d = deps()
    renderBuilder({ deps: d.deps })
    await startBuild()

    // The assistant side is on screen at once (optimistic-visible-state), not after a re-hydration.
    expect(await screen.findByText(/Building your app/i)).toBeTruthy()
    expect(h.getBuild).toHaveBeenCalledTimes(1) // the single mount-time adopt — no second hydration

    act(() => { d.fake.open(); d.fake.emitEnvelope(STEP(1)) })
    expect(await screen.findByText(/Scaffolding your app/i)).toBeTruthy() // feed row in the DOM
  })

  it('flips the status line to "preview is live" once preview_ready arrives', async () => {
    const d = deps()
    renderBuilder({ deps: d.deps })
    await startBuild()
    act(() => { d.fake.open(); d.fake.emitEnvelope(PREVIEW(3)) })
    expect(await screen.findByText(/preview is live/i)).toBeTruthy()
  })

  it('does NOT blank the live preview while the loop keeps iterating after preview_ready (KTD-8b)', async () => {
    const d = deps()
    renderBuilder({ deps: d.deps })
    await startBuild()
    act(() => { d.fake.open(); d.fake.emitEnvelope(PREVIEW(3)) })
    await waitFor(() => expect(document.querySelector('iframe')?.getAttribute('src')).toBe(PREVIEW_URL))

    // A self-heal step AFTER the preview came up: the frame stays (same URL), the overlay shows.
    act(() => { d.fake.emitEnvelope(STEP(4)) })
    expect(document.querySelector('iframe')?.getAttribute('src')).toBe(PREVIEW_URL) // NOT blanked
    expect(await screen.findByText(/still iterating/i)).toBeTruthy()
  })
})
