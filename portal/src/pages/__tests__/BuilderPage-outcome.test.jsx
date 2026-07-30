/**
 * The build outcome, portal side (003-U5).
 *
 * The DURABLE write is the server's — builds take minutes and users close tabs, so a
 * portal-written record would be missing for exactly the users a permanent record serves. This
 * page renders the same outcome locally so a watching user sees it immediately.
 *
 * SEQ IS THE SERVER'S, NOT OURS. This page does not predict which slot the server's outcome took —
 * it cannot, because the server writes while this tab may be reloading or closed, and a wrong
 * guess is not a visible error but a lost message. It re-seeds `seqRef` from what each append
 * reports it actually stored; the allocation itself is pinned in
 * `backend/tests/api/v1/conversations/test_seq_allocation.py`.
 *
 * WHAT THE OUTCOME IS DERIVED FROM CHANGED (U5). A build is a Write TURN, so the terminal that
 * produces this card is a `turn_ended` FRAME — its `status`, `reason` and tri-state
 * `snapshotCommitted` — not a C7 `ended` envelope off a build session. The identity a record is
 * keyed by moved with it: a build IS its turn, so `turnId` is what distinguishes one record from
 * the next and what a reload's stored row has to be matched against.
 *
 * The test with teeth here is that DEDUPE: after a reload the transcript already holds the
 * server's row, and a replayed terminal would stack a second copy on top of it.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import {
  FakeEventSource, makeClient, primeClient, planReply, primeTurn, PREVIEW_URL,
  waitForGateOpen, scriptBuildTurn, BUILD_TURN_ID, T_STEP, T_PREVIEW, T_BUILD_END,
} from './_builderSession.jsx'

const h = vi.hoisted(() => ({
  loadBuilds: vi.fn(), newBuild: vi.fn(), createBuild: vi.fn(), getBuild: vi.fn(),
  deleteBuild: vi.fn(), listProjectConversations: vi.fn(), buildUserParts: vi.fn(),
  sendMessage: vi.fn(),
  startTurn: vi.fn(), readTurnStream: vi.fn(), buildFromPlan: vi.fn(), stopTurn: vi.fn(),
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
  // A build's Stop is the TURN stop now — there is no session-level stop left to reach for.
  stopTurn: (...a) => h.stopTurn(...a),
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
async function send(text) {
  await waitForGateOpen()
  fireEvent.change(composer(), { target: { value: text } })
  fireEvent.keyDown(composer(), { key: 'Enter' })
}

/**
 * Drive a build to running: send → confirm the card → the page joins the WRITE TURN the
 * transition started. `readTurnStream` called with a turnId is what "joined" means now; the
 * returned script is the open socket every frame below is pushed into.
 */
async function runBuild(turn) {
  await send('a visitor app')
  fireEvent.click(await screen.findByRole('button', { name: /^Build it$/ }))
  await waitFor(() => expect(h.buildFromPlan).toHaveBeenCalled())
  await waitFor(() =>
    expect(h.readTurnStream).toHaveBeenCalledWith(expect.objectContaining({ turnId: BUILD_TURN_ID })),
  )
  await turn.frame(T_STEP('Scaffolding your app…'))
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
    const turn = scriptBuildTurn()
    h.readTurnStream.mockImplementation(turn.impl)
    renderThread()
    await runBuild(turn)

    await turn.frame(T_PREVIEW()) // the url the record will carry
    await turn.frame(T_BUILD_END())
    await turn.end()

    const card = await screen.findByTestId('build-outcome')
    expect(card.textContent).toMatch(/build finished/i)
    // The per-build preview URL died with its sandbox the moment the build ended, so the card —
    // a permanent historical record — must never surface it as a working link (F4). The live
    // "Relaunch preview" affordance lives in the preview pane, not on this card.
    expect(card.querySelector(`a[href="${PREVIEW_URL}"]`)).toBeNull()
  })

  it('never writes the outcome itself — that is the server’s job', async () => {
    const turn = scriptBuildTurn()
    h.readTurnStream.mockImplementation(turn.impl)
    renderThread()
    await runBuild(turn)

    await turn.frame(T_PREVIEW(), T_BUILD_END())
    await turn.end()
    await screen.findByTestId('build-outcome')

    // Two writers would mean two records for one build (the server's row and this one), and the
    // server's is the one that survives a closed tab.
    expect(persistedOutcomes()).toHaveLength(0)
  })

  it('shows a failed build with its reason', async () => {
    const turn = scriptBuildTurn()
    h.readTurnStream.mockImplementation(turn.impl)
    renderThread()
    await runBuild(turn)

    // The terminal's own `reason` — `self_heal_budget_exhausted`, `sandbox_gone`, and the rest
    // ride the frame now rather than a C7 envelope.
    await turn.frame(T_BUILD_END({ status: 'failed', reason: 'tsc failed after 3 attempts' }))
    await turn.end()

    const card = await screen.findByTestId('build-outcome')
    expect(card.textContent).toMatch(/build failed/i)
    // The reason is what the user can act on — surface it, don't bury it in the feed.
    expect(card.textContent).toMatch(/tsc failed after 3 attempts/i)
  })

  it('warns when a build ran but its code was not saved', async () => {
    const turn = scriptBuildTurn()
    h.readTurnStream.mockImplementation(turn.impl)
    renderThread()
    await runBuild(turn)

    await turn.frame(T_BUILD_END({ snapshotCommitted: false }))
    await turn.end()

    // A build that did not save is not a success: the next build will not start from it, and the
    // user has to know that before building on top of it.
    expect(await screen.findByText(/wasn’t saved/i)).toBeTruthy()
  })

  it('a terminal that never reports the save does not claim the code was thrown away', async () => {
    // UNKNOWN IS NOT FALSE, and `snapshotCommitted` is deliberately tri-state on the wire for
    // exactly this: `null`/absent means the terminal never reached the save (or never spoke about
    // it), `false` means the save RAN and did not land. Collapsing the two told a citizen their
    // work was binned about a build that almost certainly saved it. The server's durable row
    // carries the real answer and replaces this card on reload; until then, saying nothing is the
    // only honest option.
    const turn = scriptBuildTurn()
    h.readTurnStream.mockImplementation(turn.impl)
    renderThread()
    await runBuild(turn)

    await turn.frame(T_BUILD_END()) // completed, and silent about the snapshot
    await turn.end()
    await screen.findByTestId('build-outcome')

    expect(screen.queryByText(/wasn’t saved/i)).toBeNull()
  })

  it('a user Stop stops the TURN, and its terminal is still recorded', async () => {
    // A build has no session-level stop any more: one working indicator, one way to interrupt it,
    // and it is the same `stopTurn` an ordinary reply uses. The stop is a REQUEST — the terminal
    // still arrives as a frame, and it is that frame the record is written from.
    const turn = scriptBuildTurn()
    h.readTurnStream.mockImplementation(turn.impl)
    renderThread()
    await runBuild(turn)

    fireEvent.click(await screen.findByRole('button', { name: /^stop$/i }))
    await waitFor(() => expect(h.stopTurn).toHaveBeenCalledWith('thread-1', BUILD_TURN_ID))
    expect(h.stop).not.toHaveBeenCalled() // never the C3 session stop

    await turn.frame(T_BUILD_END({ status: 'stopped', reason: 'stopped_by_user' }))
    await turn.end('completed')
    expect(await screen.findByTestId('build-outcome')).toBeTruthy()
  })

  it('still warns when the terminal explicitly says the snapshot did not commit', async () => {
    // The other half of the tri-state: `false` from the server is a real answer and must keep
    // warning. Only the ABSENCE of an answer is what stops being read as one.
    const turn = scriptBuildTurn()
    h.readTurnStream.mockImplementation(turn.impl)
    renderThread()
    await runBuild(turn)

    await turn.frame(T_BUILD_END({ snapshotCommitted: false }))
    await turn.end()

    expect(await screen.findByText(/wasn’t saved/i)).toBeTruthy()
  })

  it('shows nothing while the build is still running', async () => {
    const turn = scriptBuildTurn()
    h.readTurnStream.mockImplementation(turn.impl)
    renderThread()
    await runBuild(turn)

    await turn.frame(T_PREVIEW())

    await waitFor(() => expect(screen.getByText(/preview is live/i)).toBeTruthy())
    expect(outcomeCards()).toHaveLength(0)
  })
})

// (The 'seq follows the server' suite is retired with U7: the client persists nothing, so
// there is no seq to negotiate — the server owns transcript ordering outright.)

describe('dedupe on the build TURN', () => {
  it('does not double-show a replayed terminal frame', async () => {
    const turn = scriptBuildTurn()
    h.readTurnStream.mockImplementation(turn.impl)
    renderThread()
    await runBuild(turn)

    // A resubscribe (resume-once on a dropped socket) re-delivers the terminal it already saw.
    await turn.frame(T_BUILD_END(), T_BUILD_END())
    await turn.end()

    await screen.findByTestId('build-outcome')
    await waitFor(() => expect(outcomeCards()).toHaveLength(1))
  })

  it('does not re-show after a reload, where the server’s row is already in the transcript', async () => {
    // The case an `_id`/seq guard cannot catch: both are fresh after a reload, so only matching on
    // the BUILD TURN tells us this outcome is already recorded. (Under the session model this was
    // `sessionId`; a build is its turn now, so that is the identity the stored row carries.)
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
            { type: 'build', status: 'ended', turnId: BUILD_TURN_ID, previewUrl: PREVIEW_URL },
          ],
        },
      ],
    })
    const turn = scriptBuildTurn()
    h.readTurnStream.mockImplementation(turn.impl)
    renderThread()
    await screen.findByTestId('build-outcome')
    await runBuild(turn)

    await turn.frame(T_BUILD_END())
    await turn.end()

    await waitFor(() => expect(h.buildFromPlan).toHaveBeenCalled())
    expect(outcomeCards()).toHaveLength(1)
  })

  it('shows a SECOND build separately — dedupe is per build turn, not per thread', async () => {
    const first = scriptBuildTurn()
    h.readTurnStream.mockImplementation(first.impl)
    renderThread()
    await runBuild(first)
    await first.frame(T_BUILD_END())
    await first.end()
    await screen.findByTestId('build-outcome')

    // An iteration is a NEW turn, and its outcome is its own record — the whole reason the record
    // is keyed by the build rather than by the thread.
    const second = scriptBuildTurn({ plan: planReply('Build it, now with a chart.', 'opt-2') })
    h.readTurnStream.mockImplementation(second.impl)
    h.buildFromPlan.mockResolvedValue({ outcome: 'started', turnId: 'bt-2', appId: 'a1' })
    await send('add a chart')
    fireEvent.click(await screen.findByRole('button', { name: /^Build it$/ }))
    await waitFor(() => expect(h.buildFromPlan).toHaveBeenCalledTimes(2))
    await second.frame(T_BUILD_END({ turnId: 'bt-2' }))
    await second.end()

    await waitFor(() => expect(outcomeCards()).toHaveLength(2))
  })
})
