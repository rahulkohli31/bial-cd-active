/**
 * Pins the memo contract review round 2's fix 1 restores: `ChatMessageRow` (React.memo,
 * BuilderPage.tsx) must actually bail out for a historical bubble when something UNRELATED to
 * that message changes. Before the fix, `handleBuildIt`'s `useCallback` depended on
 * `buildBlockedMessage`/`watchBuildTurn` — two plain in-body functions recreated every render —
 * so `handleBuildIt` (and therefore every `ChatMessageRow`'s `onBuildIt` prop) got a new identity
 * on every BuilderPage render, defeating the memo for every row, always. Nothing in the portal
 * suite asserted this before — a future revert of either `useCallback` would be silent.
 *
 * Typing in the composer is the trigger: it's a plain `useState` write local to BuilderPage,
 * touching nothing about any historical message, session, or plan card — the exact "unrelated
 * render" a working memo should absorb entirely before it reaches a historical row.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

const h = vi.hoisted(() => ({
  loadBuilds: vi.fn(), newBuild: vi.fn(), createBuild: vi.fn(), getBuild: vi.fn(),
  deleteBuild: vi.fn(), listProjectConversations: vi.fn(), buildUserParts: vi.fn(),
  startTurn: vi.fn(), readTurnStream: vi.fn(), buildFromPlan: vi.fn(),
  resolvePlanOptions: vi.fn(),
  start: vi.fn(), stop: vi.fn(), getStatus: vi.fn(), forceEnd: vi.fn(), relaunchPreview: vi.fn(),
  notifyUsageChanged: vi.fn(),
  messageContentRender: vi.fn(),
}))

vi.mock('../../utils/usage', () => ({ notifyUsageChanged: h.notifyUsageChanged }))
vi.mock('../../utils/builderHistory', () => ({
  loadBuilds: h.loadBuilds, newBuild: h.newBuild, createBuild: h.createBuild,
  getBuild: h.getBuild, deleteBuild: h.deleteBuild, deriveTitle: (t) => (t || '').slice(0, 40),
}))
vi.mock('../../utils/conversationApi', () => ({ listProjectConversations: h.listProjectConversations }))
vi.mock('../../utils/chatHistory', () => ({ relativeTime: () => 'now' }))
vi.mock('../../components/layout/Navbar', () => ({ default: () => null }))
vi.mock('../../utils/attachmentStore', async (orig) => ({ ...(await orig()), buildUserParts: h.buildUserParts }))
vi.mock('../../utils/turnStreamApi', async (orig) => ({
  ...(await orig()),
  startTurn: (...a) => h.startTurn(...a),
  readTurnStream: (...a) => h.readTurnStream(...a),
  buildFromPlan: (...a) => h.buildFromPlan(...a),
  resolvePlanOptions: (...a) => h.resolvePlanOptions(...a),
}))
// A spy standing in for the real renderer — records one call per actual render of the row it's
// mounted in, keyed on `parts` (unique per historical message here), so a memo bail-out shows up
// as the call count for that message staying at 1 no matter what else re-renders around it.
vi.mock('../../components/chat/MessageContent', () => ({
  default: (props) => {
    h.messageContentRender(props)
    return null
  },
}))

import BuilderPage from '../BuilderPage'
import { FakeEventSource, makeClient, primeClient, primeTurn } from './_builderSession.jsx'

function renderAt(chatId, sessionDeps, projectId = 'p1') {
  return render(
    <MemoryRouter initialEntries={['/x']}>
      <BuilderPage chatId={chatId} projectId={projectId} projectName="VIP Movement" buildSessionDeps={sessionDeps} />
    </MemoryRouter>,
  )
}

const deps = () => {
  const fake = new FakeEventSource('x')
  return { fake, deps: { client: makeClient(h), eventSourceFactory: () => fake } }
}

const composer = () => screen.getByPlaceholderText(/describe what you need/i)

/** How many times MessageContent was invoked for the row carrying this exact text. */
const rendersFor = (text) =>
  h.messageContentRender.mock.calls.filter(([props]) => {
    const parts = Array.isArray(props.parts) ? props.parts : []
    return parts.some((p) => p?.type === 'text' && p.text === text)
  }).length

beforeEach(() => {
  vi.clearAllMocks()
  Element.prototype.scrollIntoView = vi.fn()
  primeClient(h)
  h.newBuild.mockReturnValue('build-X')
  h.createBuild.mockResolvedValue({ ok: true })
  h.loadBuilds.mockResolvedValue([])
  h.listProjectConversations.mockResolvedValue([])
  h.buildUserParts.mockImplementation(async (text) => [{ type: 'text', text }])
  primeTurn(h)
})
afterEach(() => cleanup())

describe('ChatMessageRow actually memoizes (review round 2, fix 1)', () => {
  it('typing in the composer does not re-render an unrelated historical bubble', async () => {
    h.getBuild.mockResolvedValue({
      id: 'build-X',
      kind: 'builder',
      mode: 'plan',
      messages: [
        { id: 'm0', role: 'user', seq: 0, parts: [{ type: 'text', text: 'first message' }] },
        { id: 'm1', role: 'assistant', seq: 1, parts: [{ type: 'text', text: 'first reply, unrelated to anything typed next' }] },
      ],
    })
    const { deps: d } = deps()
    renderAt('build-X', d)

    // MessageContent is mocked to render nothing observable, so wait on the spy itself rather
    // than on DOM text.
    await waitFor(() => expect(rendersFor('first reply, unrelated to anything typed next')).toBeGreaterThan(0))
    const before = rendersFor('first reply, unrelated to anything typed next')

    // Composer text is local `useState` in BuilderPage — touches no message, session, or plan
    // state. A memo that actually bails out must absorb this entirely before the historical row.
    fireEvent.change(composer(), { target: { value: 'typing should not disturb history' } })

    const after = rendersFor('first reply, unrelated to anything typed next')
    expect(after).toBe(before)
  })
})
