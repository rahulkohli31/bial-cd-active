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
import { StrictMode } from 'react'
import { render, screen, fireEvent, waitFor, act, cleanup } from '@testing-library/react'
import { MemoryRouter, Routes, Route, useParams, useNavigate } from 'react-router-dom'

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
  previewConfigs: [],
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
// Capture what the sandboxed iframe would be configured with on EVERY commit — the
// isolation guard is about a single bad render, not a final state.
vi.mock('../../components/LivePreview', () => ({
  default: ({ config }) => {
    h.previewConfigs.push(config)
    return null
  },
}))
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
  h.previewConfigs.length = 0
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

describe('BuilderPage — provision precedes the first code write', () => {
  it('calls provisionApp BEFORE patchBuildCode — assert ORDER, not merely that both happened', async () => {
    // The backend mirrors a code snapshot into app_registry.current_code only when the app
    // row already exists. PATCH-then-provision therefore loses exactly the FIRST build's
    // code, and the project's NEXT chat seeds from nothing. Every unit test still passes.
    renderHandoff()
    await waitFor(() => expect(h.patchBuildCode).toHaveBeenCalled())

    expect(h.provisionApp).toHaveBeenCalledTimes(1)
    expect(h.provisionApp.mock.invocationCallOrder[0]).toBeLessThan(h.patchBuildCode.mock.invocationCallOrder[0])
  })

  it('provisions with {conversationId, projectId} — never a positional conversation id', async () => {
    renderHandoff()
    await waitFor(() => expect(h.provisionApp).toHaveBeenCalled())
    expect(h.provisionApp.mock.calls[0][0]).toEqual({ conversationId: 'build-X', projectId: 'p1' })
  })

  it('patches the code under the CONVERSATION id, not the app id', async () => {
    renderHandoff()
    await waitFor(() => expect(h.patchBuildCode).toHaveBeenCalled())
    expect(h.patchBuildCode.mock.calls[0][0]).toBe('build-X')
  })

  it('provisions even when the generated code never touches BIALData', async () => {
    // current_code is the PROJECT's durable source, not just the data-app's. A build with
    // no data wiring must still reach it, or the project's next chat starts blank.
    h.sendMessage.mockResolvedValue('```jsx:preview\nfunction PreviewApp(){return <div>view only</div>}\n```')
    renderHandoff()
    await waitFor(() => expect(h.provisionApp).toHaveBeenCalledTimes(1))
  })

  it('does NOT provision when the turn produced no code', async () => {
    h.sendMessage.mockResolvedValue('A few questions first — who will use this?')
    renderHandoff()
    await waitFor(() => expect(h.sendMessage).toHaveBeenCalled())
    await act(async () => { await Promise.resolve() })
    expect(h.provisionApp).not.toHaveBeenCalled()
    expect(h.patchBuildCode).not.toHaveBeenCalled()
  })

  it('reads the app from the project and fires ZERO provision calls when one already exists', async () => {
    h.getBuild.mockResolvedValue({ id: 'build-X', kind: 'builder', messages: [], code: { current: { source: 'x' } } })
    h.getAppStatus.mockResolvedValue({ appId: 'app-existing', status: 'approved', appKey: 'k', loginRequired: false })
    render(
      <MemoryRouter initialEntries={['/chat/build-X']}>
        <Routes>
          <Route path="/chat/:chatId" element={<BuilderPage projectId="p1" projectName="VIP" projectAppId="app-existing" />} />
        </Routes>
      </MemoryRouter>,
    )
    await waitFor(() => expect(h.getAppStatus).toHaveBeenCalledWith('app-existing'))
    expect(h.provisionApp).not.toHaveBeenCalled()
  })

  it('surfaces a 409 (app owned by another user) as a distinct, non-generic message', async () => {
    h.provisionApp.mockRejectedValue(new ApiError('conflict', 409))
    renderHandoff()
    expect(await screen.findByText(/belongs to another user/i)).toBeTruthy()
  })
})

describe('BuilderPage — cross-app isolation across a chat navigation', () => {
  // React Router reuses this component across /chat/{A} → /chat/{B}: no remount. `config`
  // feeds the sandboxed iframe's appId/appKey, and the preview issues data-service calls on
  // mount. A record written while B's code sits inside A's appKey lands in A's store, which
  // `.claude/rules/security.md` forbids outright.
  const PROPS = {
    'chat-A': { projectId: 'pA', projectName: 'A', projectAppId: 'app-A' },
    'chat-B': { projectId: 'pB', projectName: 'B', projectAppId: 'app-B' },
  }

  /** Stands in for ChatRoute: re-derives the page's props from the routed chat id. */
  function BuilderHost() {
    const { chatId } = useParams()
    return <BuilderPage chatId={chatId} {...PROPS[chatId]} />
  }

  function GoToB() {
    const navigate = useNavigate()
    return <button onClick={() => navigate('/chat/chat-B')}>go to B</button>
  }

  function renderTwoChats() {
    return render(
      <MemoryRouter initialEntries={['/chat/chat-A']}>
        <GoToB />
        <Routes>
          <Route path="/chat/:chatId" element={<BuilderHost />} />
        </Routes>
      </MemoryRouter>,
    )
  }

  beforeEach(() => {
    h.getBuild.mockImplementation(async (id) => ({ id, kind: 'builder', messages: [], code: { current: { source: `code-for-${id}` } } }))
  })

  it('never hands project B’s chat a config bearing project A’s appKey', async () => {
    h.getAppStatus.mockImplementation(async (appId) => ({ appId, status: 'draft', appKey: `key-${appId}`, loginRequired: false }))
    renderTwoChats()
    await waitFor(() => expect(h.previewConfigs.some((c) => c?.appKey === 'key-app-A')).toBe(true))

    h.previewConfigs.length = 0
    fireEvent.click(screen.getByText('go to B'))

    await waitFor(() => expect(h.previewConfigs.some((c) => c?.appKey === 'key-app-B')).toBe(true))
    // Not one render in between may have carried A's identity.
    expect(h.previewConfigs.some((c) => c?.appKey === 'key-app-A')).toBe(false)
    expect(h.previewConfigs.some((c) => c?.appId === 'app-A')).toBe(false)
  })

  it('renders config === undefined between the chat change and the resolved status fetch', async () => {
    // B's status fetch never resolves, so the ONLY thing that can clear A's config is the
    // render-time guard — evaluated before any effect runs.
    h.getAppStatus.mockImplementation(async (appId) =>
      appId === 'app-A'
        ? { appId, status: 'draft', appKey: 'key-app-A', loginRequired: false }
        : new Promise(() => {}),
    )
    renderTwoChats()
    await waitFor(() => expect(h.previewConfigs.at(-1)?.appKey).toBe('key-app-A'))

    fireEvent.click(screen.getByText('go to B'))

    // B's fetch is still pending; the iframe must be holding nothing at all.
    expect(h.previewConfigs.at(-1)).toBeUndefined()
    expect(h.previewConfigs.some((c) => c?.appKey === 'key-app-A' && h.previewConfigs.indexOf(c) > 0)).toBeDefined()
  })
})

describe('BuilderPage — the composer is not shared across a chat navigation', () => {
  function BuilderHost() {
    const { chatId } = useParams()
    return <BuilderPage chatId={chatId} projectId="p1" projectName="P" />
  }
  function GoToB() {
    const navigate = useNavigate()
    return <button onClick={() => navigate('/chat/chat-B')}>go to B</button>
  }

  it('drops a typed draft when the same instance adopts /chat/A → /chat/B', async () => {
    // There is no key={chatId} remount, so whatever the adopt effect fails to reset is chat B
    // wearing chat A's clothes. A leaked draft would fire into B on the next Enter.
    h.getBuild.mockResolvedValue(null) // both fresh: welcome only, a usable empty composer
    render(
      <MemoryRouter initialEntries={['/chat/chat-A']}>
        <GoToB />
        <Routes>
          <Route path="/chat/:chatId" element={<BuilderHost />} />
        </Routes>
      </MemoryRouter>,
    )
    const composer = await screen.findByPlaceholderText(/Type instructions/i)
    fireEvent.change(composer, { target: { value: 'a draft meant only for chat A' } })
    expect(composer.value).toBe('a draft meant only for chat A')

    fireEvent.click(screen.getByText('go to B'))

    // Same component instance, now showing chat B — the composer must be empty.
    await waitFor(() => {
      expect(screen.getByPlaceholderText(/Type instructions/i).value).toBe('')
    })
  })
})

describe('BuilderPage — the StrictMode load strand (U7)', () => {
  // React StrictMode dev-mounts each effect twice (mount → cleanup → remount). The load
  // effect must re-fetch on the remount and APPLY the result; the pre-fix guard keyed the
  // early-return on a ref set when the fetch STARTED, so the remount short-circuited and the
  // discarded first fetch left the transcript blank.
  const SAVED = {
    id: 'build-X',
    kind: 'builder',
    messages: [{ id: 'm0', role: 'assistant', parts: [{ type: 'text', text: 'SAVED TRANSCRIPT LINE' }], seq: 0 }],
    code: { current: { source: 'x' } },
  }

  it('renders a saved transcript under <StrictMode> (getBuild resolves on a real tick)', async () => {
    h.getBuild.mockResolvedValue(SAVED)
    render(
      <StrictMode>
        <MemoryRouter initialEntries={['/chat/build-X']}>
          <Routes>
            <Route path="/chat/:chatId" element={<BuilderPage projectId="p1" projectName="P" />} />
          </Routes>
        </MemoryRouter>
      </StrictMode>,
    )
    expect((await screen.findAllByText('SAVED TRANSCRIPT LINE')).length).toBeGreaterThan(0)
  })

  it('fires the handoff seed exactly once under <StrictMode> (no double-generation)', async () => {
    h.getBuild.mockResolvedValue(null) // a fresh chat: seed from the handoff prompt
    render(
      <StrictMode>
        <MemoryRouter
          initialEntries={[
            { pathname: '/chat/build-X', search: '?projectId=p1&kind=builder', state: { prompt: 'build me a gate tracker', theme: 'bial' } },
          ]}
        >
          <Routes>
            <Route path="/chat/:chatId" element={<BuilderPage projectId="p1" projectName="VIP" />} />
          </Routes>
        </MemoryRouter>
      </StrictMode>,
    )
    await waitFor(() => expect(h.sendMessage).toHaveBeenCalled())
    await act(async () => { await Promise.resolve() })
    expect(h.sendMessage).toHaveBeenCalledTimes(1)
  })

  it('non-StrictMode single mount still renders the saved transcript (no regression)', async () => {
    h.getBuild.mockResolvedValue({ ...SAVED, messages: [{ id: 'm0', role: 'assistant', parts: [{ type: 'text', text: 'SINGLE MOUNT LINE' }], seq: 0 }] })
    render(
      <MemoryRouter initialEntries={['/chat/build-X']}>
        <Routes>
          <Route path="/chat/:chatId" element={<BuilderPage projectId="p1" projectName="P" />} />
        </Routes>
      </MemoryRouter>,
    )
    expect((await screen.findAllByText('SINGLE MOUNT LINE')).length).toBeGreaterThan(0)
  })
})

describe('BuilderPage — a send blocked by an in-flight turn explains itself', () => {
  it('toasts instead of silently dropping the click while a prior turn is still streaming', async () => {
    // sendingRef flips true synchronously and only clears in generate()'s finally. Hang the
    // first send inside buildUserParts so sendingRef stays true AND generating stays false —
    // the exact window where the Send button still looks enabled (it doesn't reflect the ref).
    h.getBuild.mockResolvedValue({
      id: 'build-X',
      kind: 'builder',
      messages: [{ id: 'm0', role: 'user', parts: [{ type: 'text', text: 'hi' }], seq: 0 }],
      code: { current: { source: 'x' } },
    })
    h.buildUserParts.mockReturnValue(new Promise(() => {})) // first send never completes

    render(
      <MemoryRouter initialEntries={['/chat/build-X']}>
        <Routes>
          <Route path="/chat/:chatId" element={<BuilderPage projectId="p1" projectName="P" />} />
        </Routes>
      </MemoryRouter>,
    )
    const composer = await screen.findByPlaceholderText(/Type instructions/i)
    fireEvent.change(composer, { target: { value: 'first' } })
    fireEvent.keyDown(composer, { key: 'Enter' })
    await waitFor(() => expect(h.buildUserParts).toHaveBeenCalledTimes(1))

    // The first send cleared the composer; type a second message and try again.
    fireEvent.change(composer, { target: { value: 'second' } })
    fireEvent.keyDown(composer, { key: 'Enter' })

    // The user gets a reason, not silence — and the blocked send never re-enters the pipeline.
    expect(await screen.findByText(/wait for the current build to finish/i)).toBeTruthy()
    expect(h.buildUserParts).toHaveBeenCalledTimes(1)
  })
})
