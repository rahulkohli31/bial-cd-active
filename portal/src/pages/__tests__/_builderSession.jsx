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
 * switchMode / resolvePlanOptions), (b) prime it with `primeTurn(h)`, and (c) drive
 * `sendAndConfirm()`. `turnStreaming` scripts the frame feed; `planReply()` is the standard
 * text-plus-card turn.
 *
 * Not a `*.test.*` file → the runner never collects it.
 */
import { fireEvent, screen, render, waitFor } from '@testing-library/react'
import { expect } from 'vitest'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import BuilderPage from '../BuilderPage'

export { FakeEventSource } from '../../utils/buildSessionMock'

export const PREVIEW_URL = 'https://app-xyz.example.azurecontainerapps.io/'

// C3 response builders (camelCase). `over` lets a test tweak one field.
export const startResp = (over = {}) => ({ sessionId: 's1', projectId: 'p1', appId: 'a1', status: 'provisioning', previewUrl: null, createdAt: 'c', ...over })
export const statusResp = (over = {}) => ({ sessionId: 's1', projectId: 'p1', appId: 'a1', status: 'provisioning', previewUrl: null, lastSeq: null, createdAt: 'c', updatedAt: 'u', ...over })
export const LOCK = { sessionId: 's1', held: true, ownerUserId: 'u', ttlSeconds: 900, expiresAt: 'e' }
export const HB = { sessionId: 's1', alive: true, cadenceSeconds: 30, heartbeatExpiresAt: 'e' }
export const RELEASE = { sessionId: 's1', released: true }
export const ENDED_RESP = { sessionId: 's1', status: 'ended' }

// C7 envelope builders (snake_case).
export const STEP = (seq = 1) => ({ type: 'step', seq, name: 'scaffold', label: 'Scaffolding your app…', state: 'started' })
export const LOG = (seq = 2, text = 'added 10 packages') => ({ type: 'log', seq, source: 'exec', stream: 'stdout', text })
export const PREVIEW = (seq = 3, url = PREVIEW_URL) => ({ type: 'preview_ready', seq, preview_url: url })
export const ENDED = (seq = 9, status = 'ended', reason = 'completed') => ({ type: 'ended', seq, status, preview_url: null, snapshot_committed: true, reason })
export const QUOTA = (seq = 3) => ({ type: 'quota_exceeded', seq, limit: 1_000_000, used: 1_000_000, resets_at: '2026-07-15T18:30:00Z' })

/** Assemble a BuildSessionClient from a per-file `h` bag of vi.fn()s. */
export function makeClient(h) {
  return {
    start: h.start,
    relaunchPreview: h.relaunchPreview,
    stop: h.stop,
    getStatus: h.getStatus,
    forceEnd: h.forceEnd,
    acquireLock: h.acquireLock,
    renewLock: h.renewLock,
    releaseLock: h.releaseLock,
    heartbeat: h.heartbeat,
  }
}

/** Give the per-file `h` bag its default happy resolutions (call inside beforeEach). */
export function primeClient(h) {
  h.start.mockResolvedValue(startResp())
  h.stop.mockResolvedValue(ENDED_RESP)
  h.getStatus.mockResolvedValue(statusResp())
  h.forceEnd.mockResolvedValue(ENDED_RESP)
  h.acquireLock.mockResolvedValue(LOCK)
  h.renewLock.mockResolvedValue(LOCK)
  h.releaseLock.mockResolvedValue(RELEASE)
  h.heartbeat.mockResolvedValue(HB)
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

/** Give the per-file `h` bag its default TURN resolutions (call inside beforeEach). */
export function primeTurn(h, frames = planReply()) {
  h.startTurn.mockResolvedValue({ turnId: 't1' })
  h.readTurnStream.mockImplementation(turnStreaming(frames))
  h.buildFromPlan.mockResolvedValue({ outcome: 'started', sessionId: 's1', appId: 'a1' })
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

export function renderBuilder({ deps, projectId = 'p1', initialEntries = ['/chat/build-X?projectId=p1&kind=builder'] } = {}) {
  return render(
    <MemoryRouter initialEntries={initialEntries}>
      <Routes>
        <Route path="/chat/:chatId" element={<BuilderPage projectId={projectId} projectName="VIP Movement" buildSessionDeps={deps} />} />
        <Route path="/projects" element={<div>projects index</div>} />
        <Route path="/projects/:pid" element={<div>project page</div>} />
      </Routes>
    </MemoryRouter>,
  )
}
