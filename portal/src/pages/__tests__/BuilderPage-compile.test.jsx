/**
 * The compile signal's last mile, portal side (U11/U12).
 *
 * The supervisor derives the state and the turn engine emits it; this file covers the only part
 * neither of those can: that a `compile` frame arriving on the turn stream actually reaches the
 * preview pane as a prop, that a catch-up `snapshot` carries it for a tab that reloaded
 * mid-build, and that a new turn does not start by claiming the app is fine.
 *
 * LivePreview is stubbed to RECORD ITS PROPS rather than render. The pane's own behaviour on each
 * value is pinned in `components/__tests__/LivePreview.test.jsx`; what is unproven without this
 * file is the wiring between the two, which is exactly where a frame gets dropped silently.
 *
 * CHAT-KIND MIGRATION (sfw-002). `ConversationMode`/`ConversationKind` collapsed into one
 * two-valued `ChatKind` fixed at creation, and this page now renders ONLY a `build` chat — every
 * composer send holds the write toolset and runs directly against the sandbox (BuilderPage.tsx's
 * own routing-rule docblock). There is no more "send → plan card → Build it" detour to drive a
 * build through on this page: `handleBuildIt`'s whole mechanism now creates a SECOND, different
 * chat and navigates there, so a card press can prove nothing about the compile signal this file
 * exists to test. `runBuild` below drives the honest new path instead — an ordinary composer send
 * IS a build turn, full stop — which is also simpler than the card dance it replaces.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup, act } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import {
  FakeEventSource, makeClient, primeClient,
  waitForGateOpen, T_STEP, T_WORKSPACE, T_PREVIEW, T_BUILD_END, T_DELTA, PREVIEW_URL,
  inWorkspace,
} from './_builderSession.jsx'

const h = vi.hoisted(() => ({
  loadBuilds: vi.fn(), newBuild: vi.fn(), createBuild: vi.fn(), getBuild: vi.fn(),
  deleteBuild: vi.fn(), listProjectConversations: vi.fn(), buildUserParts: vi.fn(),
  sendMessage: vi.fn(),
  startTurn: vi.fn(), readTurnStream: vi.fn(), buildFromPlan: vi.fn(), stopTurn: vi.fn(),
  resolvePlanOptions: vi.fn(),
  start: vi.fn(), relaunchPreview: vi.fn(), stop: vi.fn(), getStatus: vi.fn(), forceEnd: vi.fn(),
  fetchPreviewState: vi.fn(), fetchCompileState: vi.fn(), fetchSaveState: vi.fn(),
  checkWorkspace: vi.fn(),
}))

/** Every `compileState` the pane has been handed, in order. */
const seen = []
/** Every `workspaceLost` the pane has been handed, in order (U4). */
const lostSeen = []

vi.mock('../../utils/builderHistory', () => ({
  loadBuilds: h.loadBuilds, newBuild: h.newBuild, createBuild: h.createBuild,
  getBuild: h.getBuild, deleteBuild: h.deleteBuild, deriveTitle: (t) => (t || '').slice(0, 40),
}))
vi.mock('../../utils/conversationApi', () => ({ listProjectConversations: h.listProjectConversations }))
vi.mock('../../utils/chatHistory', () => ({ relativeTime: () => 'now' }))
vi.mock('../../components/layout/Navbar', () => ({ default: () => null }))
vi.mock('../../components/LivePreview', () => ({
  default: (props) => {
    seen.push(props.compileState)
    // U4 — the retraction reaches the pane as its own prop, so a test can watch it arrive without
    // rendering the real component's whole cover machinery.
    lostSeen.push(props.workspaceLost)
    return null
  },
}))
vi.mock('../../components/AttachmentChips', () => ({ default: () => null }))
vi.mock('../../utils/attachmentStore', async (orig) => ({
  ...(await orig()), buildUserParts: h.buildUserParts,
}))
vi.mock('../../utils/chatErrors', async (orig) => await orig())
vi.mock('../../utils/buildSessionApi', async (orig) => ({
  ...(await orig()),
  fetchPreviewState: (...a) => h.fetchPreviewState(...a),
  fetchCompileState: (...a) => h.fetchCompileState(...a),
  fetchSaveState: (...a) => h.fetchSaveState(...a),
  checkWorkspace: (...a) => h.checkWorkspace(...a),
}))
// `switchMode` is GONE — a chat's kind is fixed at creation, so there is no per-thread setting
// left to switch. A mock factory that still listed it would be mocking an export the real module
// no longer has; `resolvePlanOptions` is real but unused here (this suite never renders a
// plan offer), kept only because the surface reaches for it when an offer is answered, so a stray
// render must not hit the network.
vi.mock('../../utils/turnStreamApi', async (orig) => ({
  ...(await orig()),
  startTurn: (...a) => h.startTurn(...a),
  readTurnStream: (...a) => h.readTurnStream(...a),
  buildFromPlan: (...a) => h.buildFromPlan(...a),
  stopTurn: (...a) => h.stopTurn(...a),
  resolvePlanOptions: (...a) => h.resolvePlanOptions(...a),
}))

import ConversationSurface from '../../components/chat/ConversationSurface'

function renderThread(chatId = 'thread-1') {
  const fake = new FakeEventSource(chatId)
  const deps = { client: makeClient(h), eventSourceFactory: () => fake }
  return render(
    <MemoryRouter initialEntries={[`/chat/${chatId}`]}>
      <Routes>
        {inWorkspace(<Route path="/chat/:chatId" element={<ConversationSurface projectId="p1" buildSessionDeps={deps} />} />)}
      </Routes>
    </MemoryRouter>,
  )
}

const composer = () => screen.getByPlaceholderText(/ask for another change/i)

async function send(text = 'a visitor app') {
  await waitForGateOpen()
  fireEvent.change(composer(), { target: { value: text } })
  fireEvent.keyDown(composer(), { key: 'Enter' })
}

/** The consolidating snapshot every subscribe gets FIRST, turn or no turn, gap-free replay or
 *  none (`backend/src/api/v1/conversations/turns.py`'s own docstring: "emit the first frame
 *  BEFORE any model byte — the snapshot serves that role"). It is what tells this page which
 *  turn is live (`liveTurnIdRef.current`), which the compile-probe gate below reads to decide
 *  whether the stream is already the authority — so a scripted turn that skips it is lying about
 *  the one fact every real subscribe leads with. */
const T_SNAPSHOT = (turnId = 't1', seq = 1) => ({
  type: 'snapshot', seq, turnId, turnStatus: 'running', items: [], parts: [], working: false,
})

/**
 * Script an ordinary send's own turn stream as an OPEN socket a test can push frames into by
 * hand. Not `_builderSession.jsx`'s `scriptBuildTurn` — that helper still branches on whether
 * `readTurnStream` was called WITH a `turnId`, which was how the old Build-it watch (subscribing
 * to a turn already known to be a build) told itself apart from an ordinary send (subscribing
 * with none, and getting back a streamed plan). That distinction is gone: `fireRelayTurn` never
 * passes a `turnId` at all any more, or asks whether this chat's kind happens to be Write —
 * every send on this BUILD-chat page opens the SAME plain subscription, and it is that turn
 * which narrates the whole build. One shape, not two.
 */
function scriptTurn(opening = [T_SNAPSHOT(), T_WORKSPACE(undefined, 2)]) {
  const live = { emit: null, close: null }
  const impl = async ({ onFrame }) => {
    live.emit = onFrame
    for (const frame of opening) onFrame(frame)
    return new Promise((resolve) => { live.close = resolve })
  }
  return {
    impl,
    /** Push more frames into the open turn (wrapped in act, so effects flush between). */
    frame: async (...frames) => {
      await act(async () => { for (const frame of frames) live.emit?.(frame) })
    },
    /** Close the socket. The TRANSPORT outcome only; the frames decide the semantic one. */
    end: async (outcome = 'completed') => {
      await act(async () => { live.close?.(outcome); await Promise.resolve() })
    },
  }
}

/**
 * Send an ordinary message and wait for its turn to be genuinely open — no plan text, no card,
 * no `Build it` press. A build IS the turn a plain composer send starts on this page now, so
 * there is nothing left to confirm.
 */
async function runBuild(turn, text = 'a visitor app') {
  await send(text)
  await waitFor(() => expect(h.readTurnStream).toHaveBeenCalled())
  await turn.frame(T_STEP('Scaffolding your app…'))
}

const COMPILE = (state, seq = 5) => ({ type: 'compile', seq, state })

beforeEach(() => {
  seen.length = 0
  lostSeen.length = 0
  vi.clearAllMocks()
  Element.prototype.scrollIntoView = vi.fn()
  primeClient(h)
  h.createBuild.mockResolvedValue({ ok: true })
  h.getBuild.mockResolvedValue(null)
  h.loadBuilds.mockResolvedValue([])
  h.listProjectConversations.mockResolvedValue([])
  h.buildUserParts.mockImplementation(async (text) => [{ type: 'text', text }])
  h.startTurn.mockResolvedValue({ turnId: 't1' })
  h.fetchCompileState.mockResolvedValue('unknown')
  h.checkWorkspace.mockResolvedValue(false)
  h.fetchSaveState.mockResolvedValue({ appId: null, dirty: null, containerHead: null, savedHead: null, recoveryAt: null })
  h.fetchPreviewState.mockResolvedValue({
    state: 'unknown', alive: false, previewUrl: null, occupyingProjectName: null, restorable: null,
  })
})

afterEach(cleanup)

describe('BuilderPage — the compile signal reaches the preview pane', () => {
  it('asks the idle route when a preview is framed and no turn is running', async () => {
    // ★ THE RELOAD HOLE. The turn stream's producer stops at the terminal, so a tab that
    // reloads after a red turn has no signal at all — it comes up uncovered, showing the
    // framework's error screen under a live-preview label. This route is the producer that
    // outlives the turn, and it is asked on the preview probe's own tick.
    h.fetchCompileState.mockResolvedValue('failed')
    h.fetchPreviewState.mockResolvedValue({
      state: 'alive', alive: true, previewUrl: PREVIEW_URL,
      occupyingProjectName: null, restorable: true,
    })
    const turn = scriptTurn()
    h.readTurnStream.mockImplementation(turn.impl)
    renderThread()
    await runBuild(turn)
    // A framed preview, and then a turn that ENDS — which is precisely the moment the stream's
    // producer goes away and the reload hole opens.
    await turn.frame(T_PREVIEW())
    await turn.frame(T_BUILD_END())
    await turn.end('completed')
    h.fetchCompileState.mockClear()

    // Tabbing back — a deliberate human act the probe listens for, and the realistic moment a
    // citizen returns to a preview whose turn ended while they were elsewhere. On a genuine
    // reload the same probe runs at mount instead; both reach this branch the same way.
    await act(async () => {
      window.dispatchEvent(new Event('focus'))
      await Promise.resolve()
    })

    await waitFor(() => expect(h.fetchCompileState).toHaveBeenCalledWith('p1'))
    await waitFor(() => expect(seen.at(-1)).toBe('failed'))
  })

  it('does not ask while a turn is running — the stream is the better authority', async () => {
    h.fetchPreviewState.mockResolvedValue({
      state: 'alive', alive: true, previewUrl: PREVIEW_URL,
      occupyingProjectName: null, restorable: true,
    })
    const turn = scriptTurn()
    h.readTurnStream.mockImplementation(turn.impl)
    renderThread()
    await runBuild(turn)
    // Frame a preview so the probe CAN run — otherwise the absence below proves nothing.
    await turn.frame(T_PREVIEW())
    await turn.frame(COMPILE('building', 5))
    h.fetchCompileState.mockClear()

    // Force a probe while the turn is still open. Without this the assertion would pass on a
    // probe that simply never fired, which is the classic way an absence test proves nothing.
    await act(async () => {
      window.dispatchEvent(new Event('focus'))
      await Promise.resolve()
    })
    await waitFor(() => expect(h.fetchPreviewState).toHaveBeenCalled())

    // ABSENCE: the probe ran and deliberately did NOT ask about compilation…
    expect(h.fetchCompileState).not.toHaveBeenCalled()
    // …and LIVENESS: the live frame is still what the pane is holding, unmoved by the probe.
    expect(seen.at(-1)).toBe('building')
  })

  it('starts with no claim at all, rather than claiming the app compiles', async () => {
    const turn = scriptTurn()
    h.readTurnStream.mockImplementation(turn.impl)
    renderThread()
    await runBuild(turn)

    // `null`, never `'clean'`. A default of clean would uncover a pane on the strength of a
    // signal nothing has sent — which is the one behaviour this whole mechanism forbids.
    expect(seen.every((v) => v === null)).toBe(true)
  })

  it('hands each live compile frame straight through', async () => {
    const turn = scriptTurn()
    h.readTurnStream.mockImplementation(turn.impl)
    renderThread()
    await runBuild(turn)

    await turn.frame(COMPILE('building', 5))
    expect(seen.at(-1)).toBe('building')

    await turn.frame(COMPILE('failed', 6))
    expect(seen.at(-1)).toBe('failed')

    await turn.frame(COMPILE('clean', 7))
    expect(seen.at(-1)).toBe('clean')
  })

  it('passes `unknown` through as itself rather than dropping it', async () => {
    // The pane HOLDS its cover on `unknown`, so the value has to arrive to mean anything. A
    // dropped frame would look to the pane exactly like "nothing changed since the last clean".
    const turn = scriptTurn()
    h.readTurnStream.mockImplementation(turn.impl)
    renderThread()
    await runBuild(turn)

    await turn.frame(COMPILE('clean', 5))
    await turn.frame(COMPILE('unknown', 6))

    expect(seen.at(-1)).toBe('unknown')
  })

  it('recovers the state from a catch-up snapshot, so a reload mid-build lands covered', async () => {
    // Compile frames are emitted ON CHANGE, so a tab that reloads while the app is sitting broken
    // learns nothing until the next change. The snapshot is what closes that window.
    const turn = scriptTurn()
    h.readTurnStream.mockImplementation(turn.impl)
    renderThread()
    await runBuild(turn)

    await turn.frame({
      type: 'snapshot', seq: 4, turnId: 't1', turnStatus: 'running',
      items: [], parts: [], working: false, compileState: 'failed',
    })

    expect(seen.at(-1)).toBe('failed')
  })

  it('does not let a snapshot without a compile fact overwrite what the tail established', async () => {
    const turn = scriptTurn()
    h.readTurnStream.mockImplementation(turn.impl)
    renderThread()
    await runBuild(turn)

    await turn.frame(COMPILE('failed', 5))
    await turn.frame({
      type: 'snapshot', seq: 6, turnId: 't1', turnStatus: 'running',
      items: [], parts: [], working: false,
    })

    expect(seen.at(-1)).toBe('failed')
  })
})


// U4/R7 — the reversion that happens while nobody is sending messages.
describe('BuilderPage — a workspace lost while the tab sat idle', () => {
  /** A framed preview, no live turn, and a standing completion claim in the transcript.
   *
   *  THE CLAIM IS WHAT GATES THE PROBE, and it has to be a real one. This costs a container exec
   *  and can raise an operational alarm, so a project that has never been told its app is
   *  finished has nothing to be wrong about and is not worth asking. Loaded from the stored
   *  transcript rather than produced by a live turn, because the case being tested is precisely
   *  the tab that is NOT running one. */
  async function idleOverAFinishedBuild() {
    h.fetchPreviewState.mockResolvedValue({
      state: 'alive', alive: true, previewUrl: PREVIEW_URL,
      occupyingProjectName: null, restorable: true,
    })
    const turn = scriptTurn()
    h.readTurnStream.mockImplementation(turn.impl)
    renderThread()
    await runBuild(turn)
    // A framed preview (which is what arms the poll at all) and an assistant message claiming the
    // app is finished (which is what makes the check worth its container exec).
    await turn.frame(T_PREVIEW(), T_DELTA('Build complete — your app is live below.', 6), T_BUILD_END())
    await turn.end()
    await screen.findByText(/Build complete/i)
  }

  /** One probe cycle, driven the way a returning tab drives one.
   *
   *  The mount probe fires before the stored transcript has loaded, so at that moment there is no
   *  standing claim yet and the check is correctly skipped. A real tab gets its next look from the
   *  45-second interval or from coming back to the foreground; this is the second of those, and it
   *  is the one a test can drive without owning the clock. */
  async function lookAgain() {
    await act(async () => { document.dispatchEvent(new Event('visibilitychange')) })
    await act(async () => { await Promise.resolve() })
  }

  // ★ THE TURN MAY NEVER COME. Every other integrity check runs at the start of a turn; a citizen
  // who is reading, or in another tab, or at lunch gets none of them, and the completion claim
  // above their preview goes on being displayed over a dead app until they happen to send
  // something. The preview poll is the only place this can be caught.
  //
  // Mutation check: drop the `checkWorkspace` call from the probe and this goes red.
  it('tells the pane, so the cover can retract the claim', async () => {
    h.checkWorkspace.mockResolvedValue(true)
    await idleOverAFinishedBuild()

    await lookAgain()

    await waitFor(() => expect(h.checkWorkspace).toHaveBeenCalled())
    await waitFor(() => expect(lostSeen.at(-1)).toBe(true))
  })

  // It costs a container exec and can raise an operational alarm, so it must not fire while the
  // turn stream is already the authority — and it must not fire twice for one answer.
  it('asks once and then stops asking', async () => {
    h.checkWorkspace.mockResolvedValue(true)
    await idleOverAFinishedBuild()

    await lookAgain()
    await waitFor(() => expect(h.checkWorkspace).toHaveBeenCalled())
    await waitFor(() => expect(lostSeen.at(-1)).toBe(true))
    const afterFirst = h.checkWorkspace.mock.calls.length
    await lookAgain()
    await lookAgain()

    expect(afterFirst).toBeGreaterThan(0)
    expect(h.checkWorkspace.mock.calls.length).toBe(afterFirst)
  })

  // The ordinary case stays quiet. A pane that retracted on every poll would train the citizen to
  // ignore the one retraction that mattered.
  it('says nothing about a healthy workspace', async () => {
    h.checkWorkspace.mockResolvedValue(false)
    await idleOverAFinishedBuild()

    await lookAgain()
    await waitFor(() => expect(h.checkWorkspace).toHaveBeenCalled())

    // LIVENESS: the probe really ran, so the `false` below is an answer rather than a no-op.
    expect(lostSeen.every((v) => v !== true)).toBe(true)
  })
})
