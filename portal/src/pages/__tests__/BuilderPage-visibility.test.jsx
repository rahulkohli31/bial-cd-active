/**
 * Regression carried over from the single-file era: the assistant's build turn must be visible
 * WITHOUT a page refresh. Re-expressed against the TURN model (U5) — a build is a Write turn, so
 * its narrative is the `workspace` / `step` / `preview` frames of that turn, pushed to visible
 * React state as they arrive (never a remount); and while the agent keeps working AFTER the
 * preview frames, the live preview is NOT blanked (KTD-8b).
 *
 * The build begins when the user confirms the model's brief card (003-U4), not on Send — so that
 * click is the moment these tests measure immediacy from. What changed underneath it is only the
 * source: the frames come off the turn stream the click subscribes to, not a C7 session feed.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor, cleanup, within } from '@testing-library/react'
import {
  FakeEventSource, PREVIEW_URL, makeClient, primeClient, renderBuilder,
  primeTurn, sendAndConfirm, scriptBuildTurn, T_STEP, T_PREVIEW,
} from './_builderSession.jsx'

const h = vi.hoisted(() => ({
  loadBuilds: vi.fn(), newBuild: vi.fn(), createBuild: vi.fn(), getBuild: vi.fn(),
  deleteBuild: vi.fn(), listProjectConversations: vi.fn(), buildUserParts: vi.fn(),
  sendMessage: vi.fn(),
  startTurn: vi.fn(), readTurnStream: vi.fn(), buildFromPlan: vi.fn(),
  switchMode: vi.fn(), resolvePlanOptions: vi.fn(),
  start: vi.fn(), stop: vi.fn(), getStatus: vi.fn(), forceEnd: vi.fn(),
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

/**
 * Send a turn, confirm the brief the relay answers with, and wait until the build turn is
 * genuinely subscribed — `readTurnStream` called WITH a turnId is what "the build is underway"
 * means now, and it is the socket every frame below is pushed into.
 */
async function startBuild(text = 'build me a tool') {
  await sendAndConfirm(text)
  await waitFor(() => expect(h.buildFromPlan).toHaveBeenCalled())
  await waitFor(() =>
    expect(h.readTurnStream).toHaveBeenCalledWith(expect.objectContaining({ turnId: expect.any(String) })),
  )
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
  it('shows the live status line immediately on confirming the brief, and the feed as frames arrive — no remount', async () => {
    const turn = scriptBuildTurn()
    h.readTurnStream.mockImplementation(turn.impl)
    renderBuilder({ deps: deps().deps })
    await startBuild()

    // The assistant side is on screen at once (optimistic-visible-state), not after a re-hydration.
    // The `workspace` frame is what the bubble hangs off — the turn's sandbox is the build's start.
    expect(await screen.findByTestId('build-progress')).toBeTruthy()
    expect(h.getBuild).toHaveBeenCalledTimes(1) // the single mount-time adopt — no second hydration

    await turn.frame(T_STEP('Scaffolding your app…'))
    // SCOPED TO THE VISIBLE ROW (U17). The label renders twice now — once in the feed row a
    // person reads, once in the sr-only live region that paces what a screen reader hears — so
    // an unscoped `findByText` is ambiguous rather than wrong.
    const row = await screen.findByTestId('build-activity')
    expect(await within(row).findByText(/Scaffolding your app/i)).toBeTruthy() // feed row in the DOM
  })

  it('flips the status line to "preview is live" once the preview frame arrives', async () => {
    const turn = scriptBuildTurn()
    h.readTurnStream.mockImplementation(turn.impl)
    renderBuilder({ deps: deps().deps })
    await startBuild()

    await turn.frame(T_PREVIEW())
    expect(await screen.findByText(/preview is live/i)).toBeTruthy()
  })

  it('does NOT blank the live preview while the agent keeps working after the preview frames (KTD-8b)', async () => {
    const turn = scriptBuildTurn()
    h.readTurnStream.mockImplementation(turn.impl)
    renderBuilder({ deps: deps().deps })
    await startBuild()

    await turn.frame(T_PREVIEW())
    await waitFor(() => expect(document.querySelector('iframe')?.getAttribute('src')).toBe(PREVIEW_URL))

    // A self-heal step AFTER the preview came up. The defect is the BLANKING: the frame must keep
    // the same URL (same DOM node, no reload) while the loop runs on. The "still working" line
    // rides the chat bubble now rather than a preview-pane overlay — one narrative, one place —
    // so that is where the reassurance is asserted.
    await turn.frame(T_STEP('Fixing the type error', { id: 'call-2', seq: 4 }))
    expect(document.querySelector('iframe')?.getAttribute('src')).toBe(PREVIEW_URL) // NOT blanked
    expect(await screen.findByText(/still working on your app/i)).toBeTruthy()
  })
})
