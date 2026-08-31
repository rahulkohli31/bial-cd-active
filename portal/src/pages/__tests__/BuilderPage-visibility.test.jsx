/**
 * Regression carried over from the single-file era: the assistant's build turn must be visible
 * WITHOUT a page refresh. Re-expressed against the TURN model (U5) — a build is a Write turn, so
 * its narrative is the `workspace` / `step` / `preview` frames of that turn, pushed to visible
 * React state as they arrive (never a remount); and while the agent keeps working AFTER the
 * preview frames, the live preview is NOT blanked (KTD-8b).
 *
 * CHAT-KIND MIGRATION (sfw-002). The build used to begin when the user confirmed the model's
 * brief card (003-U4), and that click was the moment these tests measured immediacy from. It is
 * not any more: this page renders ONLY a `build` chat (a chat's kind is fixed at creation), so
 * EVERY composer send already holds the write toolset and runs directly against the sandbox —
 * there is no card-confirm gate in front of it (BuilderPage.tsx's routing-rule docblock).
 * `handleBuildIt`'s plan-options card still exists, but pressing it now creates a SECOND,
 * different build chat and navigates away to it — it cannot be what these tests drive a build
 * through any more, since the turn it starts never touches this page. `startBuild` below
 * measures immediacy from the ordinary send instead, which is both the honest trigger and a
 * simpler one.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor, cleanup, within, act, fireEvent } from '@testing-library/react'
import {
  FakeEventSource, PREVIEW_URL, makeClient, primeClient, renderBuilder,
  waitForGateOpen, composer, T_STEP, T_WORKSPACE, T_PREVIEW,
} from './_builderSession.jsx'

const h = vi.hoisted(() => ({
  loadBuilds: vi.fn(), newBuild: vi.fn(), createBuild: vi.fn(), getBuild: vi.fn(),
  deleteBuild: vi.fn(), listProjectConversations: vi.fn(), buildUserParts: vi.fn(),
  sendMessage: vi.fn(),
  startTurn: vi.fn(), readTurnStream: vi.fn(), buildFromPlan: vi.fn(),
  resolvePlanOptions: vi.fn(),
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
// `switchMode` is GONE — a chat's kind is fixed at creation, so there is nothing left for a
// per-thread setting to switch. `resolvePlanOptions` is a real export, kept mocked only because
// `PlanOptionsCard` (still imported by BuilderPage.tsx) reaches for it — never exercised here,
// since this suite never renders that card.
vi.mock('../../utils/turnStreamApi', async (orig) => ({
  ...(await orig()),
  startTurn: (...a) => h.startTurn(...a),
  readTurnStream: (...a) => h.readTurnStream(...a),
  buildFromPlan: (...a) => h.buildFromPlan(...a),
  resolvePlanOptions: (...a) => h.resolvePlanOptions(...a),
}))

function deps() {
  const fake = new FakeEventSource('x')
  return { fake, deps: { client: makeClient(h), eventSourceFactory: () => fake } }
}

/**
 * Script an ordinary send's own turn stream as an OPEN socket a test can push frames into by
 * hand. `_builderSession.jsx`'s `scriptBuildTurn` still branches on whether `readTurnStream` was
 * called WITH a `turnId` — the old Build-it watch's way of telling itself apart from an ordinary
 * send. That distinction is gone: `fireRelayTurn` never passes a `turnId`, and never asks the
 * chat's kind either — every send on this BUILD-chat page opens the one plain subscription, and
 * that IS the build. The opening snapshot mirrors what every real subscribe gets first
 * (`backend/src/api/v1/conversations/turns.py`): the consolidating frame before any model byte,
 * carrying the `turnId` this page uses to know a turn is live.
 */
function scriptTurn(opening = [{ type: 'snapshot', seq: 1, turnId: 't1', turnStatus: 'running', items: [], textSoFar: '', steps: [] }, T_WORKSPACE(undefined, 2)]) {
  const live = { emit: null, close: null }
  const impl = async ({ onFrame }) => {
    live.emit = onFrame
    for (const frame of opening) onFrame(frame)
    return new Promise((resolve) => { live.close = resolve })
  }
  return {
    impl,
    frame: async (...frames) => {
      await act(async () => { for (const frame of frames) live.emit?.(frame) })
    },
    end: async (outcome = 'completed') => {
      await act(async () => { live.close?.(outcome); await Promise.resolve() })
    },
  }
}

/** Type into the composer and send — no plan, no card, no `Build it` press. */
async function send(text = 'a visitor app') {
  await waitForGateOpen()
  fireEvent.change(composer(), { target: { value: text } })
  fireEvent.keyDown(composer(), { key: 'Enter' })
}

/**
 * Send an ordinary message and wait until its turn is genuinely open — `readTurnStream` having
 * been called is what "the build is underway" means now, and it is the socket every frame below
 * is pushed into.
 */
async function startBuild(text = 'build me a tool') {
  await send(text)
  await waitFor(() => expect(h.readTurnStream).toHaveBeenCalled())
}

beforeEach(() => {
  vi.clearAllMocks()
  Element.prototype.scrollIntoView = vi.fn()
  primeClient(h)
  h.newBuild.mockReturnValue('build-Y')
  h.createBuild.mockResolvedValue({ ok: true })
  h.getBuild.mockResolvedValue(null)
  h.loadBuilds.mockResolvedValue([])
  h.listProjectConversations.mockResolvedValue([{ id: 'build-X', kind: 'build', title: 'My build', updatedAt: new Date().toISOString() }])
  h.buildUserParts.mockImplementation(async (text) => [{ type: 'text', text }])
  h.startTurn.mockResolvedValue({ turnId: 't1' })
})
afterEach(() => cleanup())

describe('BuilderPage — build turn visible without a refresh', () => {
  it('shows the live status line immediately on sending, and the feed as frames arrive — no remount', async () => {
    const turn = scriptTurn()
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
    const turn = scriptTurn()
    h.readTurnStream.mockImplementation(turn.impl)
    renderBuilder({ deps: deps().deps })
    await startBuild()

    await turn.frame(T_PREVIEW())
    expect(await screen.findByText(/preview is live/i)).toBeTruthy()
  })

  it('does NOT blank the live preview while the agent keeps working after the preview frames (KTD-8b)', async () => {
    const turn = scriptTurn()
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
