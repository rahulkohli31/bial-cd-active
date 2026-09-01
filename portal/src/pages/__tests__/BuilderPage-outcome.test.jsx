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
 *
 * CHAT-KIND MIGRATION (sfw-002). This page now renders ONLY a `build` chat, fixed at creation, so
 * `handleBuildIt`'s plan-options card no longer runs its build here — it creates a SECOND,
 * different build chat and navigates there. Every ordinary composer send on THIS page already
 * holds the write toolset (BuilderPage.tsx's routing-rule docblock), so `runBuild` below drives
 * the outcome through a plain send rather than a card confirm — simpler, and the honest route now
 * that the card can't be it.
 *
 * That migration briefly cost this file its whole subject: deleting the old Build-it watcher
 * (`watchBuildTurn`) took its `showBuildOutcome` call with it, and nothing on the new send path
 * replaced it — an ordinary send's terminal reached `sink.terminal`/`sink.reason`/
 * `sink.snapshotCommitted` and then just... stopped, never handing them to the card. Reported and
 * fixed in `BuilderPage.tsx`: both turn watchers on this page — `fireRelayTurn` (an ordinary
 * send) and `reattachToTurn` (a reload mid-turn, or the arrival after a Build-it handoff) — now
 * call `showBuildOutcome` once their stream settles, keyed on `sink.turnId` (set from the
 * `snapshot` frame's `turnId` first, the `turn_ended` frame's as a fallback). The tests below
 * assert that fixed behaviour directly.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup, act } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { FakeEventSource, makeClient, primeClient, waitForGateOpen, PREVIEW_URL, T_STEP, T_WORKSPACE, T_PREVIEW, T_BUILD_END } from './_builderSession.jsx'

const h = vi.hoisted(() => ({
  loadBuilds: vi.fn(), newBuild: vi.fn(), createBuild: vi.fn(), getBuild: vi.fn(),
  deleteBuild: vi.fn(), listProjectConversations: vi.fn(), buildUserParts: vi.fn(),
  sendMessage: vi.fn(),
  startTurn: vi.fn(), readTurnStream: vi.fn(), buildFromPlan: vi.fn(), stopTurn: vi.fn(),
  resolvePlanOptions: vi.fn(),
  start: vi.fn(), relaunchPreview: vi.fn(), stop: vi.fn(), getStatus: vi.fn(), forceEnd: vi.fn(),
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
// `switchMode` is GONE — a chat's kind is fixed at creation, so there is no per-thread setting
// left to switch. `resolvePlanOptions` is a real export, kept mocked only because
// `PlanOptionsCard` (still imported by BuilderPage.tsx) reaches for it — never exercised here,
// since this suite never renders that card.
vi.mock('../../utils/turnStreamApi', async (orig) => ({
  ...(await orig()),
  startTurn: (...a) => h.startTurn(...a),
  readTurnStream: (...a) => h.readTurnStream(...a),
  buildFromPlan: (...a) => h.buildFromPlan(...a),
  // A build's Stop is the TURN stop now — there is no session-level stop left to reach for.
  stopTurn: (...a) => h.stopTurn(...a),
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

/** The consolidating snapshot every subscribe gets FIRST (`backend/.../turns.py`'s own docstring:
 *  "emit the first frame BEFORE any model byte — the snapshot serves that role"), carrying the
 *  `turnId` this page reads into `liveTurnIdRef` AND `sink.turnId` — the fact the Stop test below
 *  depends on (Stop needs `liveTurnIdRef` populated WHILE the turn is still running, not only at
 *  its terminal). */
const T_SNAPSHOT = (turnId, seq = 1) => ({
  type: 'snapshot', seq, turnId, turnStatus: 'running', items: [], textSoFar: '', steps: [],
})

/**
 * Script an ordinary send's own turn stream as an OPEN socket a test can push frames into by
 * hand. Not `_builderSession.jsx`'s `scriptBuildTurn` — that helper still branches on whether
 * `readTurnStream` was called WITH a `turnId`, which was how the old Build-it watch (subscribing
 * to a turn already known to be a build) told itself apart from an ordinary send (subscribing
 * with none, and getting back a streamed plan). That distinction is gone: `fireRelayTurn` never
 * passes a `turnId`, and never asks the chat's kind either — every send on this BUILD-chat page
 * opens the one plain subscription, and that IS the build.
 */
function scriptTurn(turnId, opening) {
  const live = { emit: null, close: null }
  const frames = opening ?? [T_SNAPSHOT(turnId), T_WORKSPACE(undefined, 2)]
  const impl = async ({ onFrame }) => {
    live.emit = onFrame
    for (const frame of frames) onFrame(frame)
    return new Promise((resolve) => { live.close = resolve })
  }
  return {
    impl,
    /** Push more frames into the open turn (wrapped in act, so effects flush between). */
    frame: async (...more) => {
      await act(async () => { for (const frame of more) live.emit?.(frame) })
    },
    /** Close the socket. The TRANSPORT outcome only; the frames decide the semantic one. */
    end: async (outcome = 'completed') => {
      await act(async () => { live.close?.(outcome); await Promise.resolve() })
    },
  }
}

/**
 * Drive a build to running: an ordinary send opens the write turn directly — no plan text, no
 * card, no `Build it` press. `readTurnStream` having been called is what "the build is
 * underway" means now, and it is the socket every frame below is pushed into.
 */
async function runBuild(turn, text = 'a visitor app') {
  await send(text)
  await waitFor(() => expect(h.readTurnStream).toHaveBeenCalled())
  await turn.frame(T_STEP('Scaffolding your app…'))
}

/** The outcome cards on screen. */
const outcomeCards = () => screen.queryAllByTestId('build-outcome')

/** Any `build` part the page tried to PERSIST — it must never write one; the server does. */
const persistedOutcomes = () =>
  h.createBuild.mock.calls
    .flatMap(([, message]) => message.parts || [])
    .filter((p) => p?.type === 'build')

beforeEach(() => {
  vi.clearAllMocks()
  Element.prototype.scrollIntoView = vi.fn()
  primeClient(h)
  h.createBuild.mockResolvedValue({ ok: true })
  h.getBuild.mockResolvedValue(null)
  h.loadBuilds.mockResolvedValue([])
  h.listProjectConversations.mockResolvedValue([])
  h.buildUserParts.mockImplementation(async (text) => [{ type: 'text', text }])
  h.startTurn.mockResolvedValue({ turnId: 't1' })
  h.stopTurn.mockResolvedValue('stopping')
})
afterEach(cleanup)

describe('showing the outcome', () => {
  it('does NOT present a dead preview link on the ended-build card (F4)', async () => {
    const turn = scriptTurn('t1')
    h.readTurnStream.mockImplementation(turn.impl)
    renderThread()
    await runBuild(turn)

    await turn.frame(T_PREVIEW()) // the url the record will carry
    await turn.frame(T_BUILD_END({ turnId: 't1' }))
    await turn.end()

    const card = await screen.findByTestId('build-outcome')
    expect(card.textContent).toMatch(/build finished/i)
    // The per-build preview URL died with its sandbox the moment the build ended, so the card —
    // a permanent historical record — must never surface it as a working link (F4). The live
    // "Relaunch preview" affordance lives in the preview pane, not on this card.
    expect(card.querySelector(`a[href="${PREVIEW_URL}"]`)).toBeNull()
  })

  it('never writes the outcome itself — that is the server’s job', async () => {
    const turn = scriptTurn('t1')
    h.readTurnStream.mockImplementation(turn.impl)
    renderThread()
    await runBuild(turn)

    await turn.frame(T_PREVIEW(), T_BUILD_END({ turnId: 't1' }))
    await turn.end()
    await screen.findByTestId('build-outcome')

    // Two writers would mean two records for one build (the server's row and this one), and the
    // server's is the one that survives a closed tab.
    expect(persistedOutcomes()).toHaveLength(0)
  })

  it('shows a failed build with its reason', async () => {
    const turn = scriptTurn('t1')
    h.readTurnStream.mockImplementation(turn.impl)
    renderThread()
    await runBuild(turn)

    // The terminal's own `reason` — `self_heal_budget_exhausted`, `sandbox_gone`, and the rest
    // ride the frame now rather than a C7 envelope.
    await turn.frame(T_BUILD_END({ turnId: 't1', status: 'failed', reason: 'tsc failed after 3 attempts' }))
    await turn.end()

    const card = await screen.findByTestId('build-outcome')
    expect(card.textContent).toMatch(/build failed/i)
    // The reason is what the user can act on — surface it, don't bury it in the feed.
    expect(card.textContent).toMatch(/tsc failed after 3 attempts/i)
  })

  it('warns when a build ran but its code was not saved', async () => {
    const turn = scriptTurn('t1')
    h.readTurnStream.mockImplementation(turn.impl)
    renderThread()
    await runBuild(turn)

    await turn.frame(T_BUILD_END({ turnId: 't1', snapshotCommitted: false }))
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
    const turn = scriptTurn('t1')
    h.readTurnStream.mockImplementation(turn.impl)
    renderThread()
    await runBuild(turn)

    await turn.frame(T_BUILD_END({ turnId: 't1' })) // completed, and silent about the snapshot
    await turn.end()
    // LIVENESS FIRST: a card has to actually exist for the absence below to mean anything —
    // `queryByText(...).toBeNull()` also passes on a component that silently rendered nothing.
    await screen.findByTestId('build-outcome')

    expect(screen.queryByText(/wasn’t saved/i)).toBeNull()
  })

  it('a user Stop stops the TURN, and its terminal is still recorded', async () => {
    // A build has no session-level stop any more: one working indicator, one way to interrupt it,
    // and it is the same `stopTurn` an ordinary reply uses. The stop is a REQUEST — the terminal
    // still arrives as a frame, and it is that frame the record is written from.
    const turn = scriptTurn('t1')
    h.readTurnStream.mockImplementation(turn.impl)
    renderThread()
    await runBuild(turn)

    fireEvent.click(await screen.findByRole('button', { name: /^stop$/i }))
    await waitFor(() => expect(h.stopTurn).toHaveBeenCalledWith('thread-1', 't1'))
    expect(h.stop).not.toHaveBeenCalled() // never the C3 session stop

    await turn.frame(T_BUILD_END({ turnId: 't1', status: 'stopped', reason: 'stopped_by_user' }))
    await turn.end('completed')
    expect(await screen.findByTestId('build-outcome')).toBeTruthy()
  })

  it('still warns when the terminal explicitly says the snapshot did not commit', async () => {
    // The other half of the tri-state: `false` from the server is a real answer and must keep
    // warning. Only the ABSENCE of an answer is what stops being read as one.
    const turn = scriptTurn('t1')
    h.readTurnStream.mockImplementation(turn.impl)
    renderThread()
    await runBuild(turn)

    await turn.frame(T_BUILD_END({ turnId: 't1', snapshotCommitted: false }))
    await turn.end()

    expect(await screen.findByText(/wasn’t saved/i)).toBeTruthy()
  })

  it('shows nothing while the build is still running', async () => {
    const turn = scriptTurn('t1')
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
    const turn = scriptTurn('t1')
    h.readTurnStream.mockImplementation(turn.impl)
    renderThread()
    await runBuild(turn)

    // A resubscribe (resume-once on a dropped socket) re-delivers the terminal it already saw.
    await turn.frame(T_BUILD_END({ turnId: 't1' }), T_BUILD_END({ turnId: 't1' }))
    await turn.end()

    await screen.findByTestId('build-outcome')
    await waitFor(() => expect(outcomeCards()).toHaveLength(1))
  })

  it('does not re-show after a reload, where the server’s row is already in the transcript', async () => {
    // The case an `_id`/seq guard cannot catch: both are fresh after a reload, so only matching on
    // the BUILD TURN tells us this outcome is already recorded. (Under the session model this was
    // `sessionId`; a build is its turn now, so that is the identity the stored row carries.)
    //
    // Driven through the REATTACH path (`activeTurn`), not a second ordinary send — an ordinary
    // send always mints a brand-new turn id, so it can never reproduce the one case this guard
    // exists for: the read projection still names `t1` as the live turn (a race — the server had
    // not yet cleared it when this GET ran), the transcript ALREADY holds `t1`'s persisted row,
    // and the reattach's own stream then reports the very same turn ending again.
    h.getBuild.mockResolvedValue({
      id: 'thread-1',
      activeTurn: { turnId: 't1', lastSeq: 5 },
      messages: [
        { id: 'm0', role: 'user', parts: [{ type: 'text', text: 'a visitor app' }], seq: 0 },
        {
          id: 'm1',
          role: 'assistant',
          seq: 1,
          parts: [
            { type: 'text', text: 'Build finished.' },
            { type: 'build', status: 'ended', turnId: 't1', previewUrl: PREVIEW_URL },
          ],
        },
      ],
    })
    const turn = scriptTurn('t1', [])
    h.readTurnStream.mockImplementation(turn.impl)
    renderThread()
    // The stored row renders immediately from the seeded transcript, before the reattach's
    // stream says anything at all.
    await screen.findByTestId('build-outcome')

    await waitFor(() => expect(h.readTurnStream).toHaveBeenCalled())
    await turn.frame(T_BUILD_END({ turnId: 't1' }))
    await turn.end()

    expect(outcomeCards()).toHaveLength(1)
  })

  it('shows a SECOND build separately — dedupe is per build turn, not per thread', async () => {
    const first = scriptTurn('t1')
    h.readTurnStream.mockImplementation(first.impl)
    renderThread()
    await runBuild(first)
    await first.frame(T_BUILD_END({ turnId: 't1' }))
    await first.end()
    await screen.findByTestId('build-outcome')

    // An iteration is a NEW turn, and its outcome is its own record — the whole reason the record
    // is keyed by the build rather than by the thread.
    const second = scriptTurn('t2')
    h.readTurnStream.mockImplementation(second.impl)
    await runBuild(second, 'add a chart')
    await second.frame(T_BUILD_END({ turnId: 't2' }))
    await second.end()

    await waitFor(() => expect(outcomeCards()).toHaveLength(2))
  })
})
