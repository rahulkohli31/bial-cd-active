/**
 * Shared, MOCK-FREE harness for the BuilderPage build-session suites (U5→U13). It only exports
 * plain fixtures + a render helper; each test file declares its OWN vi.hoisted mocks + vi.mock
 * (those are hoisted per-file), then feeds the C3 mock client + a FakeEventSource into BuilderPage
 * via the `buildSessionDeps` prop — the "inject the mock via the deps bag" idiom (KTD-6). The REAL
 * useBuildSession hook + LivePreview + ActivityFeed + SessionControls run, so the tests assert the
 * rendered DOM, not a stubbed marker.
 *
 * U13 CHANGED THE TRANSPORT AND THE TRIGGER. A composer send is a TURN (POST /turns + the frame
 * stream); the plan streams as text and `present_plan_options` renders the card; a build starts
 * only through the atomic Build-it transition. So a suite that wants a build must (a) mock
 * `../../utils/turnStreamApi` onto its `h` bag (startTurn / readTurnStream / buildFromPlan /
 * switchMode / resolvePlanOptions / stopTurn), (b) prime it with `primeTurn(h)`, and (c) drive
 * `sendAndConfirm()`. `turnStreaming` scripts the frame feed; `planReply()` is the standard
 * text-plus-card turn.
 *
 * U5 CHANGED WHAT BUILD-IT STARTS. It is no longer a C3 build SESSION — it is a WRITE TURN on the
 * same conversation, so `buildFromPlan` hands back a `turnId` (never a `sessionId`) and the page
 * subscribes to it with the very same `readTurnStream` an ordinary send uses. A build therefore
 * narrates itself through `workspace` / `step` / `preview` / `diagnostic` / `quota` / `turn_ended`
 * TURN FRAMES, not C7 envelopes: `scriptBuildTurn()` below is how a suite drives one, and the
 * FakeEventSource is now only for the LEGACY session path (the reload-mid-build reattach).
 *
 * Not a `*.test.*` file → the runner never collects it.
 */
import { act, fireEvent, screen, render, waitFor } from '@testing-library/react'
import { expect } from 'vitest'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import BuilderPage from '../BuilderPage'

export { FakeEventSource } from '../../utils/buildSessionMock'

export const PREVIEW_URL = 'https://app-xyz.example.azurecontainerapps.io/'

// C3 response builders (camelCase). `over` lets a test tweak one field.
export const startResp = (over = {}) => ({ sessionId: 's1', projectId: 'p1', appId: 'a1', status: 'provisioning', previewUrl: null, createdAt: 'c', ...over })
export const statusResp = (over = {}) => ({ sessionId: 's1', projectId: 'p1', appId: 'a1', status: 'provisioning', previewUrl: null, lastSeq: null, createdAt: 'c', updatedAt: 'u', ...over })
export const LOCK = { sessionId: 's1', held: true, ownerUserId: 'u', ttlSeconds: 900, expiresAt: 'e' }
export const RELEASE = { sessionId: 's1', released: true }
export const ENDED_RESP = { sessionId: 's1', status: 'ended' }

/** Assemble a BuildSessionClient from a per-file `h` bag of vi.fn()s. */
export function makeClient(h) {
  return {
    start: h.start,
    relaunchPreview: h.relaunchPreview,
    stop: h.stop,
    getStatus: h.getStatus,
    forceEnd: h.forceEnd,
    acquireLock: h.acquireLock,
    releaseLock: h.releaseLock,
  }
}

/** Give the per-file `h` bag its default happy resolutions (call inside beforeEach). */
export function primeClient(h) {
  h.start.mockResolvedValue(startResp())
  h.stop.mockResolvedValue(ENDED_RESP)
  h.getStatus.mockResolvedValue(statusResp())
  h.forceEnd.mockResolvedValue(ENDED_RESP)
  h.acquireLock.mockResolvedValue(LOCK)
  h.releaseLock.mockResolvedValue(RELEASE)
}

// ─── U13: the turn half (streamed plan + the options card) ───────────────────

/** The plan text the scripted turn streams — the card's Build-it executes it server-side. */
export const BRIEF = 'Build an application for BIAL that tracks visitor passes.'

export const PLAN_CARD_ID = 'opt-1'

// Turn-stream frame builders (camelCase — the U10 wire).
export const T_DELTA = (text, seq = 1) => ({ type: 'text_delta', seq, text })
export const T_CARD = (toolCallId = PLAN_CARD_ID, seq = 2) => ({
  type: 'plan_options',
  seq,
  item: { type: 'plan_options', seq: 0, mode: 'plan', toolCallId, state: 'pending', reason: null },
})
export const T_END = (status = 'completed', seq = 9) => ({ type: 'turn_ended', seq, turnId: 't1', status })

/** The standard planning turn: streams the plan text, presents the card, completes. */
export const planReply = (text = BRIEF, toolCallId = PLAN_CARD_ID) => [
  T_DELTA(text),
  T_CARD(toolCallId),
  T_END(),
]

/** A text-only turn (an answer / clarifying question — no card). */
export const textReply = (text) => [T_DELTA(text), T_END()]

/** A `readTurnStream` implementation that plays `frames` then resolves `outcome`. */
export const turnStreaming = (frames, outcome = 'completed') =>
  async ({ onFrame }) => {
    for (const frame of frames) onFrame(frame)
    return outcome
  }

// ─── U5: the BUILD half — a Write turn, narrated by TURN FRAMES ───────────────

/** The turn a Build-it starts. `sessionId` is gone from the transition's answer entirely. */
export const BUILD_TURN_ID = 'bt-1'

/** The sandbox lifecycle. `narrativeStatus` returns null until one of these lands, so a build
 *  test that omits it renders no bubble at all — the workspace frame IS the build's beginning. */
export const T_WORKSPACE = (state = 'ready', seq = 1, message = null, notice = null) => ({ type: 'workspace', seq, state, message, notice })

/** One tool call. `pending` is the in-flight state on the wire; the same `toolCallId` arriving a
 *  second time REPLACES the first, which is how a spinner becomes its own result. */
export const T_STEP = (label = 'Scaffolding your app…', { id = 'call-1', state = 'pending', tool = 'write_file', seq = 2, hidden = false } = {}) => ({
  type: 'step',
  seq,
  toolCallId: id,
  phase: state === 'pending' ? 'started' : 'finished',
  item: { type: 'step', seq, mode: 'write', tool, label, state, hidden, detail: {} },
})

export const T_PREVIEW = (url = PREVIEW_URL, state = 'ready', seq = 3) => ({ type: 'preview', seq, state, previewUrl: url })

/** NOT a failure — a repair run follows. It renders as an in-narrative alert row. */
export const T_DIAGNOSTIC = (title = 'Type error in app/page.tsx', seq = 4) => ({
  type: 'diagnostic', seq, source: 'tsc', title, cleanedStack: 'app/page.tsx:12:5',
})

export const T_QUOTA = (seq = 5) => ({ type: 'quota', seq, limit: 1_000_000, used: 1_000_000, resetsAt: '2026-07-15T18:30:00Z' })

/** The build's terminal. `snapshotCommitted` is TRI-STATE — omit it to mean UNKNOWN. */
export const T_BUILD_END = (over = {}) => ({
  type: 'turn_ended', seq: 9, turnId: BUILD_TURN_ID, status: 'completed', reason: null, ...over,
})

/**
 * The two sockets a build now needs, scripted as one `readTurnStream` implementation.
 *
 * An ordinary send subscribes with NO `turnId` and gets `plan` — the streamed brief plus its
 * options card. The Build-it watch subscribes WITH one (`watchBuildTurn` resubscribes at cursor 0),
 * and THAT socket is held OPEN: a running build is precisely an open socket, so a test that wants
 * to assert anything about one mid-flight has to hold it there and push frames in by hand. Close it
 * with `end()` when the build is meant to be over.
 */
export function scriptBuildTurn({ plan = planReply(), opening = [T_WORKSPACE()] } = {}) {
  const live = { emit: null, close: null }
  const impl = async ({ turnId, onFrame }) => {
    if (!turnId) {
      for (const frame of plan) onFrame(frame)
      return 'completed'
    }
    live.emit = onFrame
    for (const frame of opening) onFrame(frame)
    return new Promise((resolve) => { live.close = resolve })
  }
  return {
    impl,
    /** Push more frames into the open build turn (wrapped in act, so effects flush between). */
    frame: async (...frames) => {
      await act(async () => { for (const frame of frames) live.emit?.(frame) })
    },
    /** Close the socket. The TRANSPORT outcome only; the frames decide the semantic one. */
    end: async (outcome = 'completed') => {
      await act(async () => { live.close?.(outcome); await Promise.resolve() })
    },
  }
}

/** Give the per-file `h` bag its default TURN resolutions (call inside beforeEach). */
export function primeTurn(h, frames = planReply()) {
  h.startTurn.mockResolvedValue({ turnId: 't1' })
  h.readTurnStream.mockImplementation(turnStreaming(frames))
  // U5: the transition starts a WRITE TURN. `turnId` is what the page subscribes to; `sessionId`,
  // `reason` and the `build_failed` / `already_built` outcomes are all gone from the contract.
  h.buildFromPlan.mockResolvedValue({ outcome: 'started', turnId: BUILD_TURN_ID, appId: 'a1' })
  h.stopTurn?.mockResolvedValue('stopping')
}

/** The thread composer. */
export const composer = () => screen.getByPlaceholderText(/describe what you need/i)

/**
 * Wait out the composer gate's OPENING state (G1).
 *
 * Send stays unavailable until the adopt round-trip has answered "is a build still running in this
 * chat?" — opening it over a possibly-live build is the bug the gate exists to prevent, and a real
 * user cannot outrun that round-trip either. Deliberately waits for the CHECKING copy only, not
 * for the note to vanish: several tests send while a build IS running, to assert the refusal.
 */
export const waitForGateOpen = () =>
  waitFor(() => expect(screen.queryByText(/checking whether a build/i)).toBeNull())

/** Type into the thread composer and send (Enter — the send button is icon-only, so unnamed). */
export async function send(text = 'a visitor app') {
  await waitForGateOpen()
  fireEvent.change(composer(), { target: { value: text } })
  fireEvent.keyDown(composer(), { key: 'Enter' })
}

/**
 * The full trigger path: send a turn, wait for the plan-options card, click Build it.
 *
 * This is what a build looks like from the user's side (U11/U12): the plan streams, the card
 * presents, the click runs the atomic transition. A test that only sends is asserting a chat
 * turn, not a build.
 */
export async function sendAndConfirm(text = 'a visitor app') {
  await send(text)
  const build = await screen.findByRole('button', { name: /^Build it$/ })
  fireEvent.click(build)
  return build
}

/** Wait until a plan-options card's Build-it is on screen (without confirming it). */
export const findPlanCard = () => screen.findByRole('button', { name: /^Build it$/ })

/**
 * Annotated because TypeScript suites use this harness too (`*.test.tsx`), and without it TS
 * infers each option's type from its DEFAULT — so `hasSavedBuild` came out as the literal
 * `null` and a suite that passed `false` (a legitimate, load-bearing value: "the server
 * confirmed there is no saved build") failed to typecheck.
 *
 * @param {{
 *   deps?: object,
 *   projectId?: string,
 *   hasSavedBuild?: boolean | null,
 *   initialEntries?: string[],
 * }} [opts]
 */
export function renderBuilder({ deps, projectId = 'p1', hasSavedBuild = null, initialEntries = ['/chat/build-X?projectId=p1&kind=builder'] } = {}) {
  return render(
    <MemoryRouter initialEntries={initialEntries}>
      <Routes>
        <Route path="/chat/:chatId" element={<BuilderPage projectId={projectId} projectName="VIP Movement" projectHasSavedBuild={hasSavedBuild} buildSessionDeps={deps} />} />
        <Route path="/projects" element={<div>projects index</div>} />
        <Route path="/projects/:pid" element={<div>project page</div>} />
      </Routes>
    </MemoryRouter>,
  )
}
