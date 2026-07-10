/**
 * Project-first guards for the builder.
 *
 * Three things this file exists to pin, each of which fails SILENTLY otherwise:
 *
 *  1. The seed turn of a handed-off prompt is filed under a project (`header.projectId`),
 *     and an append failure ABORTS the turn. The old code called generate() from inside
 *     the append's catch — streaming a billed turn against a conversation row the server
 *     could not find, which quietly stripped the project description and code seed.
 *  2. `POST /v1/claude` is given the conversationId. Without it the request is a 400, and
 *     back when it was optional it silently produced a context-less answer.
 *  3. Navigating between two chats never renders one chat's transcript under the other's.
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
  buildUserParts: vi.fn(),
  getAppStatus: vi.fn(),
  provisionApp: vi.fn(),
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
vi.mock('../../utils/chatHistory', () => ({ relativeTime: () => 'now' }))
vi.mock('../../utils/appRegistryApi', () => ({
  getAppStatus: h.getAppStatus,
  provisionApp: h.provisionApp,
  submitApp: vi.fn(),
}))
vi.mock('../../components/layout/Navbar', () => ({ default: () => null }))
vi.mock('../../components/LivePreview', () => ({ default: () => null }))
vi.mock('../../utils/attachmentStore', async (importOriginal) => {
  const actual = await importOriginal()
  return { ...actual, buildUserParts: h.buildUserParts }
})

import BuilderPage from '../BuilderPage'
import { ApiError } from '../../utils/apiError'

const CODE_RESULT = '```jsx:preview\nexport default function PreviewApp(){return null}\n```'

/** Render a fresh build chat that arrived with a handed-off prompt in router state. */
function renderHandoff({ chatId = 'build-X', prompt = 'build me a gate tracker' } = {}) {
  return render(
    <MemoryRouter initialEntries={[{ pathname: `/chat/${chatId}`, search: '?projectId=p1&kind=builder', state: { prompt, theme: 'bial' } }]}>
      <Routes>
        <Route path="/chat/:chatId" element={<BuilderPage projectId="p1" projectName="VIP Movement" />} />
        <Route path="/projects/:projectId" element={<div>project home</div>} />
        <Route path="/projects" element={<div data-testid="projects-index">projects index</div>} />
      </Routes>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  Element.prototype.scrollIntoView = vi.fn()
  h.newBuild.mockReturnValue('build-N')
  h.appendBuilderMessage.mockResolvedValue({ ok: true })
  h.patchBuildCode.mockResolvedValue({ ok: true })
  h.getBuild.mockResolvedValue(null) // a fresh chat: no row until the first append
  h.loadBuilds.mockResolvedValue([])
  h.listProjectConversations.mockResolvedValue([])
  h.getAppStatus.mockResolvedValue({ status: null })
  h.provisionApp.mockResolvedValue({ appId: 'app-1', appKey: 'k', status: 'draft', loginRequired: false })
  h.sendMessage.mockResolvedValue(CODE_RESULT)
  h.buildUserParts.mockImplementation(async (text) => [{ type: 'text', text }])
})
afterEach(() => cleanup())

describe('BuilderPage — the seed turn is filed under a project', () => {
  it('sends header.projectId on the create branch', async () => {
    renderHandoff()
    await waitFor(() => expect(h.appendBuilderMessage).toHaveBeenCalled())
    const [id, message, header] = h.appendBuilderMessage.mock.calls[0]
    expect(id).toBe('build-X')
    expect(message.role).toBe('user')
    expect(header.projectId).toBe('p1')
    expect(header.title).toBeTruthy()
  })

  it('passes the conversationId to POST /claude so the server can fold in project context', async () => {
    renderHandoff()
    await waitFor(() => expect(h.sendMessage).toHaveBeenCalled())
    expect(h.sendMessage.mock.calls[0][3]).toBe('build-X')
  })

  it('persists the user turn BEFORE it streams — the row must exist when /claude resolves it', async () => {
    renderHandoff()
    await waitFor(() => expect(h.sendMessage).toHaveBeenCalled())
    const appendOrder = h.appendBuilderMessage.mock.invocationCallOrder[0]
    const sendOrder = h.sendMessage.mock.invocationCallOrder[0]
    expect(appendOrder).toBeLessThan(sendOrder)
  })
})

describe('BuilderPage — an append failure aborts the turn', () => {
  it('never streams a turn the server has no row for', async () => {
    // The original bug: generate() was called from inside the append's catch, so a failed
    // persist still billed a stream whose conversation the server could not resolve.
    h.appendBuilderMessage.mockRejectedValue(new Error('network down'))
    renderHandoff()

    expect(await screen.findByText(/Could not save this build/i)).toBeTruthy()
    await act(async () => { await Promise.resolve() })
    expect(h.sendMessage).not.toHaveBeenCalled()
  })

  it('leaves for /projects when the append 404s because the project was deleted', async () => {
    h.appendBuilderMessage.mockRejectedValue(new ApiError('Project not found.', 404))
    renderHandoff()

    expect(await screen.findByTestId('projects-index')).toBeTruthy()
    expect(h.sendMessage).not.toHaveBeenCalled()
  })

  it('shows the server\'s own message on a 400 rather than blaming the connection', async () => {
    h.appendBuilderMessage.mockRejectedValue(new ApiError('header.projectId is required', 400))
    renderHandoff()

    expect(await screen.findByText('header.projectId is required')).toBeTruthy()
    expect(h.sendMessage).not.toHaveBeenCalled()
  })
})

describe('BuilderPage — the project breadcrumb', () => {
  it('links back to the project, the only way out of a flat chat URL', async () => {
    renderHandoff()
    const link = await screen.findByRole('link', { name: /VIP Movement/i })
    expect(link.getAttribute('href')).toBe('/projects/p1')
  })
})

describe('BuilderPage — a refine turn', () => {
  it('sends projectId and the conversationId on every subsequent turn too', async () => {
    h.getBuild.mockResolvedValue({
      id: 'build-X',
      kind: 'builder',
      messages: [{ id: 'm0', role: 'user', parts: [{ type: 'text', text: 'hi' }], seq: 0 }],
      code: { current: { source: 'x' } },
    })
    render(
      <MemoryRouter initialEntries={['/chat/build-X']}>
        <Routes>
          <Route path="/chat/:chatId" element={<BuilderPage projectId="p1" projectName="VIP Movement" />} />
        </Routes>
      </MemoryRouter>,
    )

    const textarea = await screen.findByPlaceholderText(/Type instructions/i)
    fireEvent.change(textarea, { target: { value: 'make it blue' } })
    fireEvent.keyDown(textarea, { key: 'Enter' })

    await waitFor(() => expect(h.sendMessage).toHaveBeenCalled())
    const header = h.appendBuilderMessage.mock.calls[0][2]
    expect(header.projectId).toBe('p1')
    expect(header.title).toBeUndefined() // not the first turn — no re-titling
    expect(h.sendMessage.mock.calls[0][3]).toBe('build-X')
  })
})
