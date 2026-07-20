/**
 * Shared, MOCK-FREE harness for the BuilderPage build-session suites (U5). It only exports plain
 * fixtures + a render helper; each test file declares its OWN vi.hoisted mocks + vi.mock (those are
 * hoisted per-file), then feeds the C3 mock client + a FakeEventSource into BuilderPage via the
 * `buildSessionDeps` prop — the "inject the mock via the deps bag" idiom (KTD-6). The REAL
 * useBuildSession hook + LivePreview + ActivityFeed + SessionControls run, so the tests assert the
 * rendered DOM, not a stubbed marker.
 *
 * 003-U4 CHANGED HOW A BUILD IS TRIGGERED. A composer send is now a CHAT turn; the build starts
 * only when the user confirms the brief card the model returns. So a suite that wants a build must
 * (a) mock `useClaudeAPI` so the relay returns a brief fence, and (b) drive send → confirm. The
 * `briefReply` / `sendAndConfirm` helpers below are that path — use them instead of clicking Send
 * and expecting `start` to have been called.
 *
 * Not a `*.test.*` file → the runner never collects it.
 */
import { fireEvent, screen, render } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { BUILD_BRIEF_FENCE_TAG } from '../../utils/buildBrief'
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

// ─── 003-U4: the relay half (interview turns + the brief card) ───────────────

/** The refined brief a scripted relay reply proposes — what `start` should be called with. */
export const BRIEF = 'Build an application for BIAL that tracks visitor passes.'

/** An assistant reply carrying a well-formed brief fence (→ the thread renders a build card). */
export const briefReply = (brief = BRIEF) =>
  `Here's what I'll build:\n\n\`\`\`${BUILD_BRIEF_FENCE_TAG}\n${brief}\n\`\`\``

/**
 * A `sendMessage` implementation that streams `text` back, mimicking `useClaudeAPI` (deltas via
 * `onChunk`, the full text as the return value). Pass it to a suite's mocked `useClaudeAPI`.
 */
export const relayReplying = (text) => async (_messages, onChunk) => {
  onChunk(text)
  return text
}

/** The thread composer. */
export const composer = () => screen.getByPlaceholderText(/describe what you need/i)

/** Type into the thread composer and send (Enter — the send button is icon-only, so unnamed). */
export function send(text = 'a visitor app') {
  fireEvent.change(composer(), { target: { value: text } })
  fireEvent.keyDown(composer(), { key: 'Enter' })
}

/**
 * The full trigger path: send a turn, wait for the model's brief card, confirm it.
 *
 * This is what a build looks like from the user's side now, so it is what the suites drive. A
 * test that only sends is asserting an interview turn, not a build.
 */
export async function sendAndConfirm(text = 'a visitor app') {
  send(text)
  const build = await screen.findByRole('button', { name: /build this|rebuild with these changes/i })
  fireEvent.click(build)
  return build
}

/** Wait until a brief card is on screen (without confirming it). */
export const findBriefCard = () => screen.findByTestId('build-brief-card')

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
