/**
 * The advisory build lock, seen from the page (KTD-7).
 *
 * `buildLock` is now the FAST cross-tab UX pre-check only — the authoritative one-build-per-user
 * barrier is C3 start's 409 (tested in BuilderPage-session.test.jsx). Here we pin that BuilderPage
 * still wires `blockedBy` into Send (a second builder chat in the same project is warned before it
 * starts), that a different project is not blocked, that the claim is released when the build ends,
 * and that a planning chat is never blocked. Each BuilderPage owns its own manager over the shared
 * BroadcastChannel, so these two-page tests genuinely travel the wire.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act, cleanup, within } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { FakeEventSource, makeClient, primeClient, statusResp, ENDED } from './_builderSession.jsx'
import { BuildSessionAlreadyActiveError } from '../../utils/buildSessionApi'

const h = vi.hoisted(() => ({
  sendMessage: vi.fn(),
  loadBuilds: vi.fn(), newBuild: vi.fn(), appendBuilderMessage: vi.fn(), getBuild: vi.fn(),
  deleteBuild: vi.fn(), listProjectConversations: vi.fn(), buildUserParts: vi.fn(),
  planLoadHistory: vi.fn(), planNewConversation: vi.fn(), planAppendMessage: vi.fn(),
  planGetConversation: vi.fn(), planDeleteConversation: vi.fn(),
  start: vi.fn(), stop: vi.fn(), getStatus: vi.fn(), forceEnd: vi.fn(),
  acquireLock: vi.fn(), renewLock: vi.fn(), releaseLock: vi.fn(), heartbeat: vi.fn(),
}))

// useClaudeAPI is mocked ONLY for ChatPage (the planning chat below) — BuilderPage no longer uses it.
vi.mock('../../hooks/useClaudeAPI', () => ({
  useClaudeAPI: () => ({ sendMessage: h.sendMessage, error: null }),
  getContextLimits: () => ({ soft: 1e9, hard: 1e9 }),
  estimateConversationTokens: () => 0,
}))
vi.mock('../../utils/builderHistory', () => ({
  loadBuilds: h.loadBuilds, newBuild: h.newBuild, appendBuilderMessage: h.appendBuilderMessage,
  getBuild: h.getBuild, deleteBuild: h.deleteBuild, deriveTitle: (t) => (t || '').slice(0, 40),
}))
vi.mock('../../utils/conversationApi', () => ({ listProjectConversations: h.listProjectConversations }))
vi.mock('../../utils/chatHistory', () => ({
  relativeTime: () => 'now', deriveTitle: (t) => (t || '').slice(0, 40),
  loadHistory: h.planLoadHistory, newConversation: h.planNewConversation,
  appendMessage: h.planAppendMessage, getConversation: h.planGetConversation, deleteConversation: h.planDeleteConversation,
}))
vi.mock('../../components/layout/Navbar', () => ({ default: () => null }))
vi.mock('../../components/LivePreview', () => ({ default: () => null }))
vi.mock('../../utils/attachmentStore', async (orig) => ({ ...(await orig()), buildUserParts: h.buildUserParts }))

import BuilderPage from '../BuilderPage'
import ChatPage from '../ChatPage'

function renderBuilder(chatId, projectId = 'p1') {
  const fake = new FakeEventSource(chatId)
  const deps = { client: makeClient(h), eventSourceFactory: () => fake }
  const view = render(
    <MemoryRouter initialEntries={[`/chat/${chatId}`]}>
      <Routes>
        <Route path="/chat/:chatId" element={<BuilderPage projectId={projectId} projectName="VIP Movement" buildSessionDeps={deps} />} />
      </Routes>
    </MemoryRouter>,
  )
  return { ...view, fake }
}

async function sendFrom(container, text = 'make it blue') {
  const textarea = within(container).getByPlaceholderText(/Type instructions/i)
  fireEvent.change(textarea, { target: { value: text } })
  fireEvent.keyDown(textarea, { key: 'Enter' })
}

// BroadcastChannel delivery is queued on a task. A newly-mounted manager posts a `poll`; the holder
// answers with an `announce`; only after that round-trip does the new tab's `blockedBy` see the
// claim. Drain a few ticks so that handshake completes before the next send.
const flushChannel = () => act(async () => { for (let i = 0; i < 6; i += 1) await new Promise((r) => setTimeout(r, 0)) })

beforeEach(() => {
  vi.clearAllMocks()
  Element.prototype.scrollIntoView = vi.fn()
  primeClient(h)
  h.newBuild.mockReturnValue('build-N')
  h.appendBuilderMessage.mockResolvedValue({ ok: true })
  h.loadBuilds.mockResolvedValue([])
  h.getBuild.mockImplementation(async (id) => ({ id, kind: 'builder', messages: [] }))
  h.buildUserParts.mockImplementation(async (text) => [{ type: 'text', text }])
  h.planLoadHistory.mockResolvedValue([])
  h.planGetConversation.mockResolvedValue(null)
  h.planAppendMessage.mockResolvedValue({ ok: true })
  h.planNewConversation.mockReturnValue('plan-N')
  h.listProjectConversations.mockResolvedValue([
    { id: 'build-A', kind: 'builder', title: 'First build', updatedAt: new Date().toISOString() },
    { id: 'build-B', kind: 'builder', title: 'Second build', updatedAt: new Date().toISOString() },
  ])
})
afterEach(() => cleanup())

describe('BuilderPage — one build at a time, per project (advisory pre-check)', () => {
  it('warns a second builder chat in the SAME project before it starts, naming the holder', async () => {
    const a = renderBuilder('build-A')
    await sendFrom(a.container)
    await within(a.container).findByText(/Building your app/i) // A's session is live → claim held

    const b = renderBuilder('build-B')
    await within(b.container).findByPlaceholderText(/Type instructions/i)
    await flushChannel() // let B learn about A's claim over the channel
    await sendFrom(b.container, 'and add a table')

    expect(await within(b.container).findByText(/already building this project/i)).toBeTruthy()
    expect(within(b.container).getByText(/First build/)).toBeTruthy()
    // B never started a session — only A's start fired.
    expect(h.start).toHaveBeenCalledTimes(1)
  })

  it('does not block a builder chat in a DIFFERENT project', async () => {
    const a = renderBuilder('build-A', 'p1')
    await sendFrom(a.container)
    await within(a.container).findByText(/Building your app/i)

    const b = renderBuilder('build-B', 'p2')
    await within(b.container).findByPlaceholderText(/Type instructions/i)
    await flushChannel()
    await sendFrom(b.container, 'different project')

    await waitFor(() => expect(h.start).toHaveBeenCalledTimes(2)) // both started
  })

  it('a refine (stop+start) RE-ACQUIRES the claim — a second chat stays blocked after the refine (finding #23)', async () => {
    const a = renderBuilder('build-A')
    await sendFrom(a.container, 'build it')
    await within(a.container).findByText(/Building your app/i)
    expect(h.start).toHaveBeenCalledTimes(1)

    // Refine from A: stop()+start(). The stop's terminal (and start's reset) release the
    // claim transitionally — the resolved start must re-assert it.
    await sendFrom(a.container, 'make it dark mode')
    await waitFor(() => expect(h.stop).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(h.start).toHaveBeenCalledTimes(2))
    await within(a.container).findByText(/Building your app/i)

    const b = renderBuilder('build-B')
    await within(b.container).findByPlaceholderText(/Type instructions/i)
    await flushChannel() // let B learn about A's re-acquired claim
    await sendFrom(b.container, 'me too')

    expect(await within(b.container).findByText(/already building this project/i)).toBeTruthy()
    expect(h.start).toHaveBeenCalledTimes(2) // only A's two starts — B never started
  })

  it('a same-project 409 reattach RE-ACQUIRES the claim — a second chat is still warned', async () => {
    // A's start 409s against a live same-project session; A joins it via reattach. The reattach
    // passes through reset() (whose transitional no-session state releases the claim), so the
    // resolved reattach must re-assert it — else A's live session is claim-less and B sails past
    // the advisory pre-check.
    h.start.mockRejectedValue(new BuildSessionAlreadyActiveError('busy', 'other-9'))
    h.getStatus.mockResolvedValue(statusResp({ sessionId: 'other-9', projectId: 'p1', status: 'building' }))
    const a = renderBuilder('build-A')
    await sendFrom(a.container)
    await within(a.container).findByText(/Building your app/i) // reattached → A's session is live

    const b = renderBuilder('build-B')
    await within(b.container).findByPlaceholderText(/Type instructions/i)
    await flushChannel() // let B learn about A's re-acquired claim
    await sendFrom(b.container, 'me too')

    expect(await within(b.container).findByText(/already building this project/i)).toBeTruthy()
    expect(h.start).toHaveBeenCalledTimes(1) // only A's 409'd start — B never started
  })

  it('releases the claim when the build ends, so a blocked second chat can then start', async () => {
    const a = renderBuilder('build-A')
    await sendFrom(a.container)
    await within(a.container).findByText(/Building your app/i)

    const b = renderBuilder('build-B')
    await within(b.container).findByPlaceholderText(/Type instructions/i)
    await flushChannel()
    await sendFrom(b.container, 'wait for me')
    expect(await within(b.container).findByText(/already building this project/i)).toBeTruthy()
    expect(h.start).toHaveBeenCalledTimes(1)

    // A's build ends → its advisory claim retracts across the channel.
    act(() => { a.fake.open(); a.fake.emitEnvelope(ENDED(9)) })
    await within(a.container).findByText(/Build finished/i)
    await flushChannel() // let the retract reach B

    // Now B's send goes through.
    await sendFrom(b.container, 'now me')
    await waitFor(() => expect(h.start).toHaveBeenCalledTimes(2))
  })
})

describe('ChatPage — a planning chat is never blocked by a build', () => {
  it('sends freely while a build session is live in the same project', async () => {
    const a = renderBuilder('build-A')
    await sendFrom(a.container)
    await within(a.container).findByText(/Building your app/i)

    h.listProjectConversations.mockResolvedValue([])
    const plan = render(
      <MemoryRouter initialEntries={['/chat/plan-1?projectId=p1&kind=planning']}>
        <Routes>
          <Route path="/chat/:chatId" element={<ChatPage projectId="p1" projectName="VIP Movement" />} />
        </Routes>
      </MemoryRouter>,
    )
    h.sendMessage.mockResolvedValue('a plan')
    const textarea = await waitFor(() => plan.container.querySelector('textarea[placeholder*="thinking"]'))
    fireEvent.change(textarea, { target: { value: 'what should this do?' } })
    fireEvent.keyDown(textarea, { key: 'Enter' })

    await waitFor(() => expect(h.sendMessage).toHaveBeenCalled()) // the planning turn never consulted the lock
  })
})
