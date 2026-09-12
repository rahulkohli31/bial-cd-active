/**
 * The assistant's build turn must be visible WITHOUT a page refresh. A build is a Write turn:
 * its narrative is the `workspace` / `step` / `preview` frames of that turn, pushed to visible
 * React state as they arrive (never a remount), and the live preview must not blank while the
 * agent keeps working after the preview frames land.
 *
 * `startBuild` below triggers via an ordinary composer send, not a "Build it" press: a chat's
 * kind is fixed at creation, so every send on a build chat already runs the write toolset
 * directly — there is no card-confirm gate in front of it.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor, cleanup, within, act, fireEvent } from '@testing-library/react'
import {
  FakeEventSource, PREVIEW_URL, makeClient, primeClient, renderBuilder,
  waitForGateOpen, composer, T_STEP, T_WORKSPACE, T_PREVIEW,
} from './_builderSession.jsx'

const h = vi.hoisted(() => ({
  loadBuilds: vi.fn(), getBuild: vi.fn(),
  listProjectConversations: vi.fn(), buildUserParts: vi.fn(),
  startTurn: vi.fn(), readTurnStream: vi.fn(), buildFromPlan: vi.fn(),
  resolvePlanOptions: vi.fn(),
  stop: vi.fn(), getStatus: vi.fn(),
}))

vi.mock('../../utils/builderHistory', () => ({
  loadBuilds: h.loadBuilds, getBuild: h.getBuild, deriveTitle: (t) => (t || '').slice(0, 40),
}))
vi.mock('../../utils/conversationApi', () => ({
  // The send path creates the chat before its first upload; stubbed so no network is reached.
  createConversation: async () => ({ id: 'conv-created' }),
  listProjectConversations: h.listProjectConversations,
}))
vi.mock('../../components/layout/Navbar', () => ({ default: () => null }))
vi.mock('../../utils/attachmentStore', async (orig) => ({ ...(await orig()), buildUserParts: h.buildUserParts }))
// `switchMode` no longer exists — a chat's kind is fixed at creation. `resolvePlanOptions` stays
// mocked even though this suite never exercises it: the surface reaches for it whenever a plan
// offer is answered.
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
 * An ordinary send's turn stream as an OPEN socket a test can push frames into by hand. The
 * opening snapshot mirrors what a real subscribe gets first (`turns.py`): the consolidating
 * frame before any model byte, carrying the `turnId` this page needs to know a turn is live.
 */
function scriptTurn(opening = [{ type: 'snapshot', seq: 1, turnId: 't1', turnStatus: 'running', items: [], parts: [], working: false }, T_WORKSPACE(undefined, 2)]) {
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

/** Send, then wait until the turn is genuinely open — `readTurnStream` having been called is
 *  what "the build is underway" means now. */
async function startBuild(text = 'build me a tool') {
  await send(text)
  await waitFor(() => expect(h.readTurnStream).toHaveBeenCalled())
}

beforeEach(() => {
  vi.clearAllMocks()
  Element.prototype.scrollIntoView = vi.fn()
  primeClient(h)
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

    expect(await screen.findByTestId('stop-turn')).toBeTruthy()
    expect(h.getBuild).toHaveBeenCalledTimes(1) // the single mount-time adopt — no second hydration

    await turn.frame(T_STEP('Scaffolding your app…'))
    const group = await screen.findByTestId('activity-group')
    await waitFor(() => expect(group.textContent).toMatch(/Scaffolding your app/i))
  })

  it('frames the preview as soon as its frame arrives', async () => {
    const turn = scriptTurn()
    h.readTurnStream.mockImplementation(turn.impl)
    renderBuilder({ deps: deps().deps })
    await startBuild()

    await turn.frame(T_PREVIEW())
    await waitFor(() => expect(document.querySelector('iframe')?.getAttribute('src')).toBe(PREVIEW_URL))
    // The app pane frames the URL directly, without a reload or waiting for the turn to end —
    // "preview is live" was the old progress card's status line and does not reappear in chat.
    expect(within(screen.getByTestId('chat-panel')).queryByText(/preview is live/i)).toBeNull()
  })

  it('does NOT blank the live preview while the agent keeps working after the preview frames', async () => {
    const turn = scriptTurn()
    h.readTurnStream.mockImplementation(turn.impl)
    renderBuilder({ deps: deps().deps })
    await startBuild()

    await turn.frame(T_PREVIEW())
    await waitFor(() => expect(document.querySelector('iframe')?.getAttribute('src')).toBe(PREVIEW_URL))

    await turn.frame(T_STEP('Fixing the type error', { id: 'call-2', seq: 4 }))
    expect(document.querySelector('iframe')?.getAttribute('src')).toBe(PREVIEW_URL) // NOT blanked
    await waitFor(() =>
      expect(screen.getByTestId('activity-group').textContent).toMatch(/Fixing the type error/i),
    )
  })
})
