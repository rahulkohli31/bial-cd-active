/**
 * The build lock, seen from the page.
 *
 * Each BuilderPage owns its OWN lock manager over its own BroadcastChannel — there is no
 * module-level map for two instances to short-circuit through. So these two-page tests do
 * travel the channel (verified: severing `addEventListener` turns the blocking test red),
 * which makes them a genuine, if indirect, cross-tab check.
 *
 * The direct guarantee still lives in buildLock.test.ts, which drives two independent
 * managers over two real channels and asserts announce/retract/expiry on the wire.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act, cleanup } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'

const h = vi.hoisted(() => ({
  sendMessage: vi.fn(),
  loadBuilds: vi.fn(),
  newBuild: vi.fn(),
  appendBuilderMessage: vi.fn(),
  getBuild: vi.fn(),
  deleteBuild: vi.fn(),
  patchBuildCode: vi.fn(),
  listProjectConversations: vi.fn(),
  provisionApp: vi.fn(),
  getAppStatus: vi.fn(),
  planLoadHistory: vi.fn(),
  planNewConversation: vi.fn(),
  planAppendMessage: vi.fn(),
  planGetConversation: vi.fn(),
  planDeleteConversation: vi.fn(),
}))

vi.mock('../../hooks/useClaudeAPI', () => ({
  useClaudeAPI: () => ({ sendMessage: h.sendMessage, error: null }),
  buildSystemPrompt: () => 'sys',
  getContextLimits: () => ({ soft: 1e9, hard: 1e9 }),
  estimateConversationTokens: () => 0,
}))
vi.mock('../../utils/builderHistory', () => ({
  loadBuilds: h.loadBuilds,
  newBuild: h.newBuild,
  appendBuilderMessage: h.appendBuilderMessage,
  getBuild: h.getBuild,
  deleteBuild: h.deleteBuild,
  patchBuildCode: h.patchBuildCode,
  deriveTitle: (t) => (t || '').slice(0, 40),
}))
vi.mock('../../utils/conversationApi', () => ({ listProjectConversations: h.listProjectConversations }))
// ChatPage (the planning chat below) reaches for the whole planning store, not just
// relativeTime — an unmocked export would fall through to the network.
vi.mock('../../utils/chatHistory', () => ({
  relativeTime: () => 'now',
  deriveTitle: (t) => (t || '').slice(0, 40),
  loadHistory: h.planLoadHistory,
  newConversation: h.planNewConversation,
  appendMessage: h.planAppendMessage,
  getConversation: h.planGetConversation,
  deleteConversation: h.planDeleteConversation,
}))
vi.mock('../../utils/appRegistryApi', () => ({
  provisionApp: h.provisionApp,
  getAppStatus: h.getAppStatus,
  submitApp: vi.fn(),
}))
vi.mock('../../components/layout/Navbar', () => ({ default: () => null }))
vi.mock('../../components/LivePreview', () => ({ default: () => null }))

import BuilderPage from '../BuilderPage'
import ChatPage from '../ChatPage'

const CODE_RESULT = '```jsx:preview\nexport default function PreviewApp(){return null}\n```'

/** sendMessage stays pending until the returned release() is called. */
function deferredSend() {
  let resolveFn
  h.sendMessage.mockImplementation(() => new Promise((res) => { resolveFn = res }))
  return (result) => act(async () => { resolveFn(result); await Promise.resolve() })
}

function renderBuilder(chatId, projectId = 'p1') {
  return render(
    <MemoryRouter initialEntries={[`/chat/${chatId}`]}>
      <Routes>
        <Route path="/chat/:chatId" element={<BuilderPage projectId={projectId} projectName="VIP Movement" />} />
      </Routes>
    </MemoryRouter>,
  )
}

async function sendFrom(container, text = 'make it blue') {
  const textarea = container.querySelector('textarea[placeholder*="refine"]')
  fireEvent.change(textarea, { target: { value: text } })
  fireEvent.keyDown(textarea, { key: 'Enter' })
}

beforeEach(() => {
  vi.clearAllMocks()
  Element.prototype.scrollIntoView = vi.fn()
  h.newBuild.mockReturnValue('build-N')
  h.appendBuilderMessage.mockResolvedValue({ ok: true })
  h.patchBuildCode.mockResolvedValue({ ok: true })
  h.loadBuilds.mockResolvedValue([])
  h.provisionApp.mockResolvedValue({ appId: 'app-1', appKey: 'k', status: 'draft', loginRequired: false })
  h.getAppStatus.mockResolvedValue({ status: null })
  h.getBuild.mockImplementation(async (id) => ({ id, kind: 'builder', messages: [], code: { current: { source: 'x' } } }))
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

describe('BuilderPage — one build at a time, per project (same-tab path)', () => {
  it('blocks a second builder chat in the SAME project while the first streams', async () => {
    const release = deferredSend()
    const a = renderBuilder('build-A')
    await screen.findAllByPlaceholderText(/Type instructions/i)
    await sendFrom(a.container)
    await waitFor(() => expect(h.sendMessage).toHaveBeenCalledTimes(1))

    // A second builder chat in the same project mounts and tries to send.
    const b = renderBuilder('build-B')
    await waitFor(() => expect(b.container.querySelector('textarea[placeholder*="refine"]')).toBeTruthy())
    await sendFrom(b.container, 'and add a table')

    // Blocked, and the message names the chat that holds the lock.
    expect(await screen.findByText(/already building this project/i)).toBeTruthy()
    expect(await screen.findByText(/First build/)).toBeTruthy()
    expect(h.sendMessage).toHaveBeenCalledTimes(1) // B never streamed

    await release(CODE_RESULT)
  })

  it('does not block a builder chat in a DIFFERENT project', async () => {
    const release = deferredSend()
    const a = renderBuilder('build-A', 'p1')
    await screen.findAllByPlaceholderText(/Type instructions/i)
    await sendFrom(a.container)
    await waitFor(() => expect(h.sendMessage).toHaveBeenCalledTimes(1))

    const b = renderBuilder('build-B', 'p2')
    await waitFor(() => expect(b.container.querySelector('textarea[placeholder*="refine"]')).toBeTruthy())
    await sendFrom(b.container, 'different project')

    await waitFor(() => expect(h.sendMessage).toHaveBeenCalledTimes(2))
    await release(CODE_RESULT)
  })

  it('releases the lock when the build turn fails, so the project is not wedged', async () => {
    // useClaudeAPI catches a failed send and resolves null — that is the real "it broke" signal.
    h.sendMessage.mockResolvedValueOnce(null)
    const a = renderBuilder('build-A')
    await screen.findAllByPlaceholderText(/Type instructions/i)
    await sendFrom(a.container)
    await waitFor(() => expect(h.sendMessage).toHaveBeenCalledTimes(1))

    // The lock is given back in a `finally`, so the very next send goes through.
    h.sendMessage.mockResolvedValue(CODE_RESULT)
    await sendFrom(a.container, 'try again')
    await waitFor(() => expect(h.sendMessage).toHaveBeenCalledTimes(2))
    expect(screen.queryByText(/already building this project/i)).toBeNull()
  })

  it('releases the lock when the build turn completes', async () => {
    const release = deferredSend()
    const a = renderBuilder('build-A')
    await screen.findAllByPlaceholderText(/Type instructions/i)
    await sendFrom(a.container)
    await waitFor(() => expect(h.sendMessage).toHaveBeenCalledTimes(1))
    await release(CODE_RESULT)

    const b = renderBuilder('build-B')
    await waitFor(() => expect(b.container.querySelector('textarea[placeholder*="refine"]')).toBeTruthy())
    h.sendMessage.mockResolvedValue(CODE_RESULT)
    await sendFrom(b.container, 'now me')
    await waitFor(() => expect(h.sendMessage).toHaveBeenCalledTimes(2))
  })
})

describe('ChatPage — a planning chat is never blocked by a build', () => {
  it('sends freely while a build streams in the same project', async () => {
    const release = deferredSend()
    const a = renderBuilder('build-A')
    await screen.findAllByPlaceholderText(/Type instructions/i)
    await sendFrom(a.container)
    await waitFor(() => expect(h.sendMessage).toHaveBeenCalledTimes(1))

    // Planning chats never consult the lock — only builds write code.
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

    await waitFor(() => expect(h.sendMessage).toHaveBeenCalledTimes(2))
    await release(CODE_RESULT)
  })
})
