/**
 * Project-first guards for the builder, re-expressed against the C3 build session (U5).
 *
 * Preserved invariants (each fails SILENTLY otherwise):
 *  1. The seed turn is filed under a project (`header.projectId`), and an append failure ABORTS the
 *     turn — the build is never STARTED against a conversation row the server cannot find.
 *  2. The user turn is PERSISTED before the build starts (BRAIN reads project/attachment context
 *     server-side, C3 §2.1).
 *  3. Navigating between two chats never leaks one chat's composer draft into the other.
 *  4. INERTNESS: the builder feeds the preview NO app credentials (`config`/`appKey`/`accessToken`)
 *     — the app gets its data credentials server-side at provision (C9), and provisionApp is no
 *     longer called from the build path at all.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { StrictMode } from 'react'
import { render, screen, fireEvent, waitFor, act, cleanup } from '@testing-library/react'
import { MemoryRouter, Routes, Route, useParams, useNavigate } from 'react-router-dom'
import { FakeEventSource, makeClient, primeClient } from './_builderSession.jsx'

const h = vi.hoisted(() => ({
  loadBuilds: vi.fn(), newBuild: vi.fn(), appendBuilderMessage: vi.fn(), getBuild: vi.fn(),
  deleteBuild: vi.fn(), listProjectConversations: vi.fn(), buildUserParts: vi.fn(),
  previewProps: [],
  start: vi.fn(), stop: vi.fn(), getStatus: vi.fn(), forceEnd: vi.fn(),
  acquireLock: vi.fn(), renewLock: vi.fn(), releaseLock: vi.fn(), heartbeat: vi.fn(),
}))

vi.mock('../../utils/builderHistory', () => ({
  loadBuilds: h.loadBuilds, newBuild: h.newBuild, appendBuilderMessage: h.appendBuilderMessage,
  getBuild: h.getBuild, deleteBuild: h.deleteBuild, deriveTitle: (t) => (t || '').slice(0, 40),
}))
vi.mock('../../utils/conversationApi', () => ({ listProjectConversations: h.listProjectConversations }))
vi.mock('../../utils/chatHistory', () => ({ relativeTime: () => 'now' }))
vi.mock('../../components/layout/Navbar', () => ({ default: () => null }))
// Capture EVERY prop the preview is handed — the isolation assertion is about what it is fed.
vi.mock('../../components/LivePreview', () => ({ default: (props) => { h.previewProps.push(props); return null } }))
vi.mock('../../utils/attachmentStore', async (orig) => ({ ...(await orig()), buildUserParts: h.buildUserParts }))

import BuilderPage from '../BuilderPage'
import { ApiError } from '../../utils/apiError'

function makeDeps() {
  const fake = new FakeEventSource('x')
  return { client: makeClient(h), eventSourceFactory: () => fake }
}

function renderHandoff({ chatId = 'build-X', prompt = 'build me a gate tracker' } = {}) {
  return render(
    <MemoryRouter initialEntries={[{ pathname: `/chat/${chatId}`, search: '?projectId=p1&kind=builder', state: { prompt, theme: 'bial' } }]}>
      <Routes>
        <Route path="/chat/:chatId" element={<BuilderPage projectId="p1" projectName="VIP Movement" buildSessionDeps={makeDeps()} />} />
        <Route path="/projects/:projectId" element={<div>project home</div>} />
        <Route path="/projects" element={<div data-testid="projects-index">projects index</div>} />
      </Routes>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  h.previewProps.length = 0
  Element.prototype.scrollIntoView = vi.fn()
  primeClient(h)
  h.newBuild.mockReturnValue('build-N')
  h.appendBuilderMessage.mockResolvedValue({ ok: true })
  h.getBuild.mockResolvedValue(null)
  h.loadBuilds.mockResolvedValue([])
  h.listProjectConversations.mockResolvedValue([])
  h.buildUserParts.mockImplementation(async (text) => [{ type: 'text', text }])
})
afterEach(() => cleanup())

describe('BuilderPage — the seed turn is filed under a project', () => {
  it('sends header.projectId + title on the create branch, then starts the build', async () => {
    renderHandoff()
    await waitFor(() => expect(h.appendBuilderMessage).toHaveBeenCalled())
    const [id, message, header] = h.appendBuilderMessage.mock.calls[0]
    expect(id).toBe('build-X')
    expect(message.role).toBe('user')
    expect(header.projectId).toBe('p1')
    expect(header.title).toBeTruthy()
    await waitFor(() => expect(h.start).toHaveBeenCalledWith({ projectId: 'p1', prompt: 'build me a gate tracker', conversationId: 'build-X' }))
  })

  it('persists the user turn BEFORE the build starts — the row must exist when BRAIN reads context', async () => {
    renderHandoff()
    await waitFor(() => expect(h.start).toHaveBeenCalled())
    expect(h.appendBuilderMessage.mock.invocationCallOrder[0]).toBeLessThan(h.start.mock.invocationCallOrder[0])
  })
})

describe('BuilderPage — an append failure aborts the turn', () => {
  it('never starts a build the server has no row for (network error)', async () => {
    h.appendBuilderMessage.mockRejectedValue(new Error('network down'))
    renderHandoff()
    expect(await screen.findByText(/Could not save this build/i)).toBeTruthy()
    await act(async () => { await Promise.resolve() })
    expect(h.start).not.toHaveBeenCalled()
  })

  it('ABORTS the seeded send when the attachment upload fails — never a text-only build (R3)', async () => {
    // This path used to swallow the failure and build "from your description only": the user
    // handed off a prompt + a spreadsheet from the project page, the upload failed, and a build
    // ran that never saw the file. Silently ignoring an attachment is the exact bug R3 deletes,
    // so the seed must abort exactly like the send path does.
    h.buildUserParts.mockRejectedValue(new Error('Attachment storage is full.'))
    renderHandoff()

    expect(await screen.findByText(/Attachment storage is full./i)).toBeTruthy()
    await act(async () => { await Promise.resolve() })
    expect(h.start).not.toHaveBeenCalled()
    expect(h.appendBuilderMessage).not.toHaveBeenCalled()
  })

  it('releases the send gate after a seed abort, so the composer still works (R3)', async () => {
    // The gate is instance-wide: leaving `sendingRef` stuck true would wedge the composer and
    // force a reload — the toast would tell them to retry something they cannot retry.
    h.buildUserParts.mockRejectedValueOnce(new Error('Attachment storage is full.'))
    renderHandoff()
    await screen.findByText(/Attachment storage is full./i)

    h.buildUserParts.mockImplementation(async (text) => [{ type: 'text', text }])
    const textarea = await screen.findByPlaceholderText(/Type instructions/i)
    fireEvent.change(textarea, { target: { value: 'try again without the file' } })
    fireEvent.keyDown(textarea, { key: 'Enter' })

    await waitFor(() => expect(h.start).toHaveBeenCalledWith({ projectId: 'p1', prompt: 'try again without the file', conversationId: 'build-X' }))
  })

  it('leaves for /projects when the append 404s (project deleted)', async () => {
    h.appendBuilderMessage.mockRejectedValue(new ApiError('Project not found.', 404))
    renderHandoff()
    expect(await screen.findByTestId('projects-index')).toBeTruthy()
    expect(h.start).not.toHaveBeenCalled()
  })

  it("shows the server's own 400 message rather than blaming the connection", async () => {
    h.appendBuilderMessage.mockRejectedValue(new ApiError('header.projectId is required', 400))
    renderHandoff()
    expect(await screen.findByText('header.projectId is required')).toBeTruthy()
    expect(h.start).not.toHaveBeenCalled()
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
  it('sends projectId (no title) on a subsequent turn and starts with {projectId, prompt}', async () => {
    h.getBuild.mockResolvedValue({ id: 'build-X', kind: 'builder', messages: [{ id: 'm0', role: 'user', parts: [{ type: 'text', text: 'hi' }], seq: 0 }] })
    render(
      <MemoryRouter initialEntries={['/chat/build-X']}>
        <Routes>
          <Route path="/chat/:chatId" element={<BuilderPage projectId="p1" projectName="VIP Movement" buildSessionDeps={makeDeps()} />} />
        </Routes>
      </MemoryRouter>,
    )
    const textarea = await screen.findByPlaceholderText(/Type instructions/i)
    fireEvent.change(textarea, { target: { value: 'make it blue' } })
    fireEvent.keyDown(textarea, { key: 'Enter' })

    await waitFor(() => expect(h.start).toHaveBeenCalled())
    const header = h.appendBuilderMessage.mock.calls[0][2]
    expect(header.projectId).toBe('p1')
    expect(header.title).toBeUndefined()
    expect(h.start).toHaveBeenCalledWith({ projectId: 'p1', prompt: 'make it blue', conversationId: 'build-X' })
  })
})

describe('BuilderPage — the preview is fed NO app credentials (C9 server-side, U5 inertness)', () => {
  it('never hands LivePreview a config / appKey / accessToken / previewCode', async () => {
    renderHandoff()
    await waitFor(() => expect(h.start).toHaveBeenCalled())
    // Provisioning is subsumed by C3 start — the old provisionApp export itself is
    // retired from appRegistryApi (owner surface gone; pinned by appRegistryApi.test.js).
    for (const props of h.previewProps) {
      expect(props.config).toBeUndefined()
      expect(props.appKey).toBeUndefined()
      expect(props.accessToken).toBeUndefined()
      expect(props.previewCode).toBeUndefined()
    }
  })
})

describe('BuilderPage — the composer is not shared across a chat navigation', () => {
  function BuilderHost() {
    const { chatId } = useParams()
    return <BuilderPage chatId={chatId} projectId="p1" projectName="P" buildSessionDeps={makeDeps()} />
  }
  function GoToB() {
    const navigate = useNavigate()
    return <button onClick={() => navigate('/chat/chat-B')}>go to B</button>
  }

  it('a seed upload that fails AFTER a chat switch does not clobber the adopted chat (R3)', async () => {
    // The seed abort rolls the optimistic message back — but `provisional`/`userSeq` describe the
    // chat the seed started in. If the user navigated away while the upload was in flight, writing
    // them would wipe the transcript of the chat now on screen.
    h.getBuild.mockImplementation(async (id) =>
      id === 'chat-B' ? { id: 'chat-B', kind: 'builder', messages: [{ id: 'm0', role: 'assistant', parts: [{ type: 'text', text: 'CHAT B TRANSCRIPT' }], seq: 0 }] } : null,
    )
    let failUpload
    h.buildUserParts.mockReturnValue(new Promise((_resolve, reject) => { failUpload = reject }))
    render(
      <MemoryRouter initialEntries={[{ pathname: '/chat/chat-A', search: '?projectId=p1&kind=builder', state: { prompt: 'seed for A', theme: 'bial' } }]}>
        <GoToB />
        <Routes>
          <Route path="/chat/:chatId" element={<BuilderHost />} />
        </Routes>
      </MemoryRouter>,
    )
    await waitFor(() => expect(h.buildUserParts).toHaveBeenCalled())

    fireEvent.click(screen.getByText('go to B'))
    await screen.findByText('CHAT B TRANSCRIPT')
    await act(async () => { failUpload(new Error('Attachment storage is full.')); await Promise.resolve() })

    expect(screen.getByText('CHAT B TRANSCRIPT')).toBeTruthy() // B's transcript survived
    expect(h.start).not.toHaveBeenCalled()
  })

  it('drops a typed draft when the same instance adopts /chat/A → /chat/B', async () => {
    h.getBuild.mockResolvedValue(null)
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
    await waitFor(() => expect(screen.getByPlaceholderText(/Type instructions/i).value).toBe(''))
  })
})

describe('BuilderPage — the StrictMode load strand (U7)', () => {
  const SAVED = { id: 'build-X', kind: 'builder', messages: [{ id: 'm0', role: 'assistant', parts: [{ type: 'text', text: 'SAVED TRANSCRIPT LINE' }], seq: 0 }] }

  it('renders a saved transcript under <StrictMode>', async () => {
    h.getBuild.mockResolvedValue(SAVED)
    render(
      <StrictMode>
        <MemoryRouter initialEntries={['/chat/build-X']}>
          <Routes>
            <Route path="/chat/:chatId" element={<BuilderPage projectId="p1" projectName="P" buildSessionDeps={makeDeps()} />} />
          </Routes>
        </MemoryRouter>
      </StrictMode>,
    )
    expect((await screen.findAllByText('SAVED TRANSCRIPT LINE')).length).toBeGreaterThan(0)
  })

  it('fires the handoff seed exactly once under <StrictMode> (no double-start)', async () => {
    h.getBuild.mockResolvedValue(null)
    render(
      <StrictMode>
        <MemoryRouter initialEntries={[{ pathname: '/chat/build-X', search: '?projectId=p1&kind=builder', state: { prompt: 'build me a gate tracker', theme: 'bial' } }]}>
          <Routes>
            <Route path="/chat/:chatId" element={<BuilderPage projectId="p1" projectName="VIP" buildSessionDeps={makeDeps()} />} />
          </Routes>
        </MemoryRouter>
      </StrictMode>,
    )
    await waitFor(() => expect(h.start).toHaveBeenCalled())
    await act(async () => { await Promise.resolve() })
    expect(h.start).toHaveBeenCalledTimes(1)
  })
})

describe('BuilderPage — a send blocked by an in-flight turn explains itself', () => {
  it('toasts instead of silently dropping the click while a prior start is still in flight', async () => {
    h.getBuild.mockResolvedValue({ id: 'build-X', kind: 'builder', messages: [{ id: 'm0', role: 'user', parts: [{ type: 'text', text: 'hi' }], seq: 0 }] })
    h.buildUserParts.mockReturnValue(new Promise(() => {})) // first send never completes → sendingRef stays true

    render(
      <MemoryRouter initialEntries={['/chat/build-X']}>
        <Routes>
          <Route path="/chat/:chatId" element={<BuilderPage projectId="p1" projectName="P" buildSessionDeps={makeDeps()} />} />
        </Routes>
      </MemoryRouter>,
    )
    const composer = await screen.findByPlaceholderText(/Type instructions/i)
    fireEvent.change(composer, { target: { value: 'first' } })
    fireEvent.keyDown(composer, { key: 'Enter' })
    await waitFor(() => expect(h.buildUserParts).toHaveBeenCalledTimes(1))

    fireEvent.change(composer, { target: { value: 'second' } })
    fireEvent.keyDown(composer, { key: 'Enter' })

    expect(await screen.findByText(/wait for the current build/i)).toBeTruthy()
    expect(h.buildUserParts).toHaveBeenCalledTimes(1) // the blocked send never re-entered
  })
})
