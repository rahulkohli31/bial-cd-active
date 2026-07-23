/**
 * The build outcome, portal side (003-U5).
 *
 * The DURABLE write is the server's (`services/build_sessions/outcome.py`) — builds take minutes,
 * users close tabs, and an in-memory session is evicted 5 minutes after its terminal, so a
 * portal-written record would be missing for exactly the users a permanent record serves. This
 * page renders the same outcome locally so a watching user sees it immediately.
 *
 * SEQ IS THE SERVER'S, NOT OURS. This page does not predict which slot the server's outcome took —
 * it cannot, because the server writes while this tab may be reloading or closed, and a wrong
 * guess is not a visible error but a lost message. It re-seeds `seqRef` from what each append
 * reports it actually stored; the allocation itself is pinned in
 * `backend/tests/api/v1/conversations/test_seq_allocation.py`.
 *
 * The test with teeth here is the sessionId DEDUPE: after a reload the transcript already holds
 * the server's row, and a replayed terminal would stack a second copy on top of it.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act, cleanup } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import {
  FakeEventSource, makeClient, primeClient, planReply, primeTurn, turnStreaming, PREVIEW, PREVIEW_URL, ENDED, STEP,
} from './_builderSession.jsx'

const h = vi.hoisted(() => ({
  loadBuilds: vi.fn(), newBuild: vi.fn(), createBuild: vi.fn(), getBuild: vi.fn(),
  deleteBuild: vi.fn(), listProjectConversations: vi.fn(), buildUserParts: vi.fn(),
  sendMessage: vi.fn(),
  startTurn: vi.fn(), readTurnStream: vi.fn(), buildFromPlan: vi.fn(),
  switchMode: vi.fn(), resolvePlanOptions: vi.fn(),
  start: vi.fn(), relaunchPreview: vi.fn(), stop: vi.fn(), getStatus: vi.fn(), forceEnd: vi.fn(),
  acquireLock: vi.fn(), renewLock: vi.fn(), releaseLock: vi.fn(), heartbeat: vi.fn(),
}))

vi.mock('../../utils/builderHistory', () => ({
  loadBuilds: h.loadBuilds, newBuild: h.newBuild, createBuild: h.createBuild,
  getBuild: h.getBuild, deleteBuild: h.deleteBuild, deriveTitle: (t) => (t || '').slice(0, 40),
}))
vi.mock('../../utils/conversationApi', () => ({ listProjectConversations: h.listProjectConversations }))
vi.mock('../../utils/chatHistory', () => ({ relativeTime: () => 'now' }))
vi.mock('../../components/layout/Navbar', () => ({ default: () => null }))
vi.mock('../../components/LivePreview', () => ({ default: () => null }))
vi.mock('../../components/AttachmentChips', () => ({ default: () => null }))
vi.mock('../../utils/attachmentStore', async (orig) => ({ ...(await orig()), buildUserParts: h.buildUserParts }))
vi.mock('../../utils/turnStreamApi', async (orig) => ({
  ...(await orig()),
  startTurn: (...a) => h.startTurn(...a),
  readTurnStream: (...a) => h.readTurnStream(...a),
  buildFromPlan: (...a) => h.buildFromPlan(...a),
  switchMode: (...a) => h.switchMode(...a),
  resolvePlanOptions: (...a) => h.resolvePlanOptions(...a),
}))

import BuilderPage from '../BuilderPage'

function renderThread(chatId = 'thread-1') {
  const fake = new FakeEventSource(chatId)
  const deps = { client: makeClient(h), eventSourceFactory: () => fake }
  const view = render(
    <MemoryRouter initialEntries={[`/chat/${chatId}`]}>
      <Routes>
        <Route path="/chat/:chatId" element={<BuilderPage projectId="p1" buildSessionDeps={deps} />} />
      </Routes>
    </MemoryRouter>,
  )
  return { ...view, fake }
}

const composer = () => screen.getByPlaceholderText(/describe what you need/i)
function send(text) {
  fireEvent.change(composer(), { target: { value: text } })
  fireEvent.keyDown(composer(), { key: 'Enter' })
}

/** Drive a build to running: send → confirm the card → open the feed. */
async function runBuild(fake) {
  send('a visitor app')
  fireEvent.click(await screen.findByRole('button', { name: /^Build it$/ }))
  await waitFor(() => expect(h.buildFromPlan).toHaveBeenCalled())
  await waitFor(() => expect(h.getStatus).toHaveBeenCalled()) // the page joined the session
  act(() => { fake.open(); fake.emitEnvelope(STEP(1)) })
}

/** The outcome cards on screen. */
const outcomeCards = () => screen.queryAllByTestId('build-outcome')

/** Any `build` part the page tried to PERSIST — it must never write one; the server does. */
const persistedOutcomes = () =>
  h.createBuild.mock.calls
    .flatMap(([, message]) => message.parts || [])
    .filter((p) => p?.type === 'build')

/** The seqs the page sent to the append API, in order. */

beforeEach(() => {
  vi.clearAllMocks()
  Element.prototype.scrollIntoView = vi.fn()
  primeClient(h)
  h.createBuild.mockResolvedValue({ ok: true })
  h.getBuild.mockResolvedValue(null)
  h.loadBuilds.mockResolvedValue([])
  h.listProjectConversations.mockResolvedValue([])
  h.buildUserParts.mockImplementation(async (text) => [{ type: 'text', text }])
  primeTurn(h)
})
afterEach(cleanup)

describe('showing the outcome', () => {
  it('does NOT present a dead preview link on the ended-build card (F4)', async () => {
    const { fake } = renderThread()
    await runBuild(fake)

    act(() => { fake.emitEnvelope(PREVIEW(3)); fake.emitEnvelope(ENDED(9)) })

    const card = await screen.findByTestId('build-outcome')
    expect(card.textContent).toMatch(/build finished/i)
    // The per-session preview URL died with its sandbox the moment the build ended, so the card —
    // a permanent historical record — must never surface it as a working link (F4). The live
    // "Relaunch preview" affordance lives in the preview pane, not on this card.
    expect(card.querySelector(`a[href="${PREVIEW_URL}"]`)).toBeNull()
  })

  it('never writes the outcome itself — that is the server’s job', async () => {
    const { fake } = renderThread()
    await runBuild(fake)

    act(() => { fake.emitEnvelope(PREVIEW(3)); fake.emitEnvelope(ENDED(9)) })
    await screen.findByTestId('build-outcome')

    // Two writers would mean two records for one build (the server's row and this one), and the
    // server's is the one that survives a closed tab.
    expect(persistedOutcomes()).toHaveLength(0)
  })

  it('shows a failed build with its reason', async () => {
    const { fake } = renderThread()
    await runBuild(fake)

    act(() => { fake.emitEnvelope(ENDED(9, 'failed', 'tsc failed after 3 attempts')) })

    const card = await screen.findByTestId('build-outcome')
    expect(card.textContent).toMatch(/build failed/i)
    // The reason is what the user can act on — surface it, don't bury it in the feed.
    expect(card.textContent).toMatch(/tsc failed after 3 attempts/i)
  })

  it('warns when a build ran but its code was not saved', async () => {
    const { fake } = renderThread()
    await runBuild(fake)

    act(() => { fake.emitEnvelope({ ...ENDED(9), snapshot_committed: false }) })

    // A build that did not save is not a success: the next build will not start from it, and the
    // user has to know that before building on top of it.
    expect(await screen.findByText(/wasn’t saved/i)).toBeTruthy()
  })

  it('a stopped build does not claim the code was thrown away', async () => {
    // UNKNOWN IS NOT FALSE, and a graceful stop is where the difference bites. `stop()` calls
    // `finishSession('ended')` the moment the HTTP call resolves, which closes the feed — so the
    // real `ended` frame (snapshot_committed:true, because `_do_finalize` DOES snapshot unless the
    // end was forced) may never be dispatched here. There is then no local `ended` envelope at
    // all, and collapsing that silence into `false` told the user their work was binned about a
    // build that saved it. The server's row carries the real answer and replaces this card on
    // reload; until then, saying nothing is the only honest option.
    const { fake } = renderThread()
    await runBuild(fake)

    fireEvent.click(await screen.findByRole('button', { name: /^stop$/i }))
    await waitFor(() => expect(h.stop).toHaveBeenCalledTimes(1))
    await screen.findByTestId('build-outcome')

    expect(screen.queryByText(/wasn’t saved/i)).toBeNull()
  })

  it('still warns when the terminal explicitly says the snapshot did not commit', async () => {
    // The other half: `false` from the server is a real answer and must keep warning. Only the
    // ABSENCE of an answer is what stops being read as one.
    const { fake } = renderThread()
    await runBuild(fake)

    act(() => { fake.emitEnvelope({ ...ENDED(9), snapshot_committed: false }) })

    expect(await screen.findByText(/wasn’t saved/i)).toBeTruthy()
  })

  it('shows nothing while the build is still running', async () => {
    const { fake } = renderThread()
    await runBuild(fake)

    act(() => { fake.emitEnvelope(PREVIEW(3)) })

    await waitFor(() => expect(screen.getByText(/preview is live/i)).toBeTruthy())
    expect(outcomeCards()).toHaveLength(0)
  })
})

// (The 'seq follows the server' suite is retired with U7: the client persists nothing, so
// there is no seq to negotiate — the server owns transcript ordering outright.)

describe('dedupe on sessionId', () => {
  it('does not double-show a replayed terminal envelope', async () => {
    const { fake } = renderThread()
    await runBuild(fake)

    act(() => { fake.emitEnvelope(ENDED(9)) })
    await screen.findByTestId('build-outcome')
    act(() => { fake.emitEnvelope(ENDED(9)) }) // a reattach replays the terminal

    await waitFor(() => expect(outcomeCards()).toHaveLength(1))
  })

  it('does not re-show after a reload, where the server’s row is already in the transcript', async () => {
    // The case an `_id`/seq guard cannot catch: both are fresh after a reload, so only matching
    // on the SESSION tells us this outcome is already recorded.
    h.getBuild.mockResolvedValue({
      id: 'thread-1',
      messages: [
        { id: 'm0', role: 'user', parts: [{ type: 'text', text: 'a visitor app' }], seq: 0 },
        {
          id: 'm1',
          role: 'assistant',
          seq: 1,
          parts: [
            { type: 'text', text: 'Build finished.' },
            { type: 'build', status: 'ended', sessionId: 's1', previewUrl: PREVIEW_URL },
          ],
        },
      ],
    })
    const { fake } = renderThread()
    await screen.findByTestId('build-outcome')
    await runBuild(fake)

    act(() => { fake.emitEnvelope(ENDED(9)) })

    await waitFor(() => expect(h.buildFromPlan).toHaveBeenCalled())
    expect(outcomeCards()).toHaveLength(1)
  })

  it('shows a SECOND build separately — dedupe is per session, not per thread', async () => {
    const { fake } = renderThread()
    await runBuild(fake)
    act(() => { fake.emitEnvelope(ENDED(9)) })
    await screen.findByTestId('build-outcome')

    // An iteration is a new session, and its outcome is its own record.
    h.buildFromPlan.mockResolvedValue({ outcome: 'started', sessionId: 's2', appId: 'a1' })
    h.getStatus.mockResolvedValue({ sessionId: 's2', projectId: 'p1', appId: 'a1', status: 'provisioning', previewUrl: null, lastSeq: null, createdAt: 'c', updatedAt: 'u' })
    h.readTurnStream.mockImplementation(turnStreaming(planReply('Build it, now with a chart.', 'opt-2')))
    send('add a chart')
    fireEvent.click(await screen.findByRole('button', { name: /^Build it$/ }))
    await waitFor(() => expect(h.buildFromPlan).toHaveBeenCalledTimes(2))
    act(() => { fake.emitEnvelope(ENDED(10)) })

    await waitFor(() => expect(outcomeCards()).toHaveLength(2))
  })
})
