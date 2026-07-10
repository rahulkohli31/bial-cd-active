/**
 * Regression: the assistant's reply must appear in the chat as soon as a
 * generation finishes — WITHOUT a page refresh. The original bug (clarifying
 * questions invisible until reload) was that generate() persisted the assistant
 * turn but never added it to the visible `messages` state, so a no-code reply
 * (questions) rendered nothing until the next mount reloaded it from the server.
 *
 * Harness mirrors BuilderPage-persistence.test.jsx: the component runs through
 * its real UI, the API + history store are mocked at the module boundary, and a
 * deferred sendMessage lets us release the stream and assert the rendered DOM.
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
vi.mock('../../utils/chatHistory', () => ({ relativeTime: () => 'now' }))
vi.mock('../../utils/conversationApi', () => ({ listProjectConversations: h.listProjectConversations }))
vi.mock('../../components/layout/Navbar', () => ({ default: () => null }))
vi.mock('../../components/LivePreview', () => ({ default: () => null }))

import BuilderPage from '../BuilderPage'

// A clarifying-questions reply: prose only, no ```jsx:preview``` block.
const QUESTION_LINE = 'What problem should this tool solve?'
const TEXT_RESULT = ['Happy to help — a few questions first.', QUESTION_LINE, 'Who on your team will use it?'].join('\n')
// A code reply: a jsx:preview block (its prose, if any, is stripped from chat).
const CODE_RESULT = '```jsx:preview\nexport default function PreviewApp(){return null}\n```'
// The canned "build succeeded" bubble — must NOT show when no app was produced.
const READY_RE = /Your app is ready/i

function deferredSend() {
  let resolveFn
  h.sendMessage.mockImplementation(() => new Promise((res) => { resolveFn = res }))
  return (result) => act(async () => { resolveFn(result); await Promise.resolve() })
}

function renderBuilder() {
  return render(
    <MemoryRouter initialEntries={['/chat/build-X?projectId=p1&kind=builder']}>
      <Routes>
        <Route path="/chat/:chatId" element={<BuilderPage projectId="p1" projectName="VIP Movement" />} />
        <Route path="/projects" element={<div>projects index</div>} />
      </Routes>
    </MemoryRouter>,
  )
}

async function startGeneration() {
  const release = deferredSend()
  const textarea = await screen.findByPlaceholderText(/Type instructions/i)
  fireEvent.change(textarea, { target: { value: 'build me a tool' } })
  fireEvent.keyDown(textarea, { key: 'Enter' })
  await waitFor(() => expect(h.sendMessage).toHaveBeenCalledTimes(1))
  return release
}

beforeEach(() => {
  vi.clearAllMocks()
  Element.prototype.scrollIntoView = vi.fn() // jsdom doesn't implement it
  h.newBuild.mockReturnValue('build-X')
  h.appendBuilderMessage.mockResolvedValue({ ok: true })
  h.patchBuildCode.mockResolvedValue({ ok: true })
  h.deleteBuild.mockResolvedValue(true)
  h.getBuild.mockResolvedValue(null) // a fresh chat: no row until the first append
  h.loadBuilds.mockResolvedValue([])
  h.listProjectConversations.mockResolvedValue([{ id: 'build-X', kind: 'builder', title: 'My build', updatedAt: new Date().toISOString() }])
})
afterEach(() => cleanup())

describe('BuilderPage — reply visibility without refresh', () => {
  it('renders a clarifying-questions reply in the chat as soon as generation completes (no remount)', async () => {
    renderBuilder()
    const release = await startGeneration()
    await release(TEXT_RESULT)

    // The actual answer is on screen. The ONE getBuild() is the mount-time adopt that
    // discovers this client-minted chat has no row yet; the reply must not depend on a
    // second hydration to become visible — that was the original bug.
    expect(await screen.findByText(QUESTION_LINE)).toBeTruthy()
    expect(h.getBuild).toHaveBeenCalledTimes(1)
  })

  it('does NOT claim "Your app is ready" when the reply produced no app', async () => {
    renderBuilder()
    const release = await startGeneration()
    await release(TEXT_RESULT)

    expect(await screen.findByText(QUESTION_LINE)).toBeTruthy()
    expect(screen.queryByText(READY_RE)).toBeNull()
    expect(h.patchBuildCode).not.toHaveBeenCalled()
  })

  it('still shows the "app is ready" affordance + patches code when the reply IS an app', async () => {
    renderBuilder()
    const release = await startGeneration()
    await release(CODE_RESULT)

    expect(await screen.findByText(READY_RE)).toBeTruthy()
    expect(h.patchBuildCode).toHaveBeenCalledWith(
      'build-X',
      expect.objectContaining({ source: expect.any(String), entry: 'PreviewApp' }),
    )
  })
})
