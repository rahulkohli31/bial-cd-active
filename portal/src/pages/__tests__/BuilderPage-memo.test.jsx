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

import ConversationSurface from '../../components/chat/ConversationSurface'
import { FakeEventSource, makeClient, primeClient, primeTurn } from './_builderSession.jsx'

function renderAt(chatId, sessionDeps, projectId = 'p1') {
  return render(
    <MemoryRouter initialEntries={['/x']}>
      <ConversationSurface chatId={chatId} projectId={projectId} projectName="VIP Movement" buildSessionDeps={sessionDeps} />
    </MemoryRouter>,
  )
}

const deps = () => {
  const fake = new FakeEventSource('x')
  return { fake, deps: { client: makeClient(h), eventSourceFactory: () => fake } }
}

const composer = () => screen.getByPlaceholderText(/ask for another change/i)

/**
 * How many times MessageContent was invoked for the row carrying this exact text.
 *
 * IT ACCEPTS BOTH SHAPES because the caller changed (Plan D U17). The deleted row component handed
 * `MessageContent` a whole `parts` ARRAY; the thread's text-part slot hands it the already-
 * assembled STRING of one part. Reading both is what lets this test keep asserting the same
 * property across the rewrite instead of being deleted with the component it was named after.
 */
const rendersFor = (text) =>
  h.messageContentRender.mock.calls.filter(([props]) => {
    if (typeof props.parts === 'string') return props.parts === text
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

// RENAMED WITH ITS SUBJECT (Plan D U17). `ChatMessageRow` — the hand-rolled, hand-memoised
// transcript row — is deleted. The property it was written to defend is not: typing must not
// re-render history. It is now defended by construction rather than by a `memo()` call, because
// the composer's text is local state inside `Composer` and never reaches the transcript at all —
// which is a stronger guarantee than a memo whose prop list one careless addition could defeat.
describe('typing never re-renders history', () => {
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

    // Composer text is local `useState` INSIDE THE COMPOSER — it touches no message, session or
    // offer state, and the surface above it does not re-render at all. Nothing has to bail out.
    fireEvent.change(composer(), { target: { value: 'typing should not disturb history' } })

    const after = rendersFor('first reply, unrelated to anything typed next')
    expect(after).toBe(before)
  })
})
