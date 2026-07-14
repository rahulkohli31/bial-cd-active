/**
 * Shared, MOCK-FREE harness for the BuilderPage build-session suites (U5). It only exports plain
 * fixtures + a render helper; each test file declares its OWN vi.hoisted mocks + vi.mock (those are
 * hoisted per-file), then feeds the C3 mock client + a FakeEventSource into BuilderPage via the
 * `buildSessionDeps` prop — the "inject the mock via the deps bag" idiom (KTD-6). The REAL
 * useBuildSession hook + LivePreview + ActivityFeed + SessionControls run, so the tests assert the
 * rendered DOM, not a stubbed marker.
 *
 * Not a `*.test.*` file → the runner never collects it.
 */
import { render } from '@testing-library/react'
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
