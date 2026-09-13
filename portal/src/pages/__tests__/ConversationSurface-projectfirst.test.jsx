/**
 * Project-first guards for the builder: a send is a CHAT turn, and a build starts only
 * when the user confirms the brief card the model replies with.
 *
 * Invariants pinned below (each fails SILENTLY otherwise):
 *  1. The seed turn is filed under a project: the row is CREATED first, then the file uploaded
 *     against it, then the turn posted. A refusal at either of the first two doors ABORTS —
 *     a build never starts against a conversation row the server never created.
 *  2. The user turn is PERSISTED (same call that folds in the project description + the
 *     interview protocol) before the relay reads it.
 *  3. Navigating between two chats never leaks one chat's composer draft into the other.
 *  4. INERTNESS: the preview gets NO app credentials — those arrive server-side at provision.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { StrictMode } from 'react'
import { render, screen, fireEvent, waitFor, act, cleanup } from '@testing-library/react'
import { MemoryRouter, Routes, Route, useParams, useNavigate, useLocation } from 'react-router-dom'
import {
  FakeEventSource, makeClient, primeClient,
  PLAN_CARD_ID, primeTurn, send, sendAndConfirm,
  inWorkspace,
} from './_builderSession.jsx'

const h = vi.hoisted(() => ({
  loadBuilds: vi.fn(), getBuild: vi.fn(),
  listProjectConversations: vi.fn(), createConversation: vi.fn(), buildUserParts: vi.fn(),
  startTurn: vi.fn(), readTurnStream: vi.fn(), buildFromPlan: vi.fn(),
  resolvePlanOptions: vi.fn(),
  previewProps: [],
  authFetch: vi.fn(),
  stop: vi.fn(), getStatus: vi.fn(),
}))

vi.mock('../../utils/builderHistory', () => ({
  loadBuilds: h.loadBuilds, getBuild: h.getBuild, deriveTitle: (t) => (t || '').slice(0, 40),
}))
// SPREAD THE ORIGINAL — a factory naming only `listProjectConversations` would leave every other
// export undefined, including the shared `uuidv7` `handleBuildIt` needs.
vi.mock('../../utils/conversationApi', async (importOriginal) => ({
  ...(await importOriginal()),
  // The send path creates the chat before its first upload; spied so its ORDER against the
  // upload is assertable, and so a refused create can be staged.
  createConversation: (...a) => h.createConversation(...a),
  listProjectConversations: h.listProjectConversations,
}))
// The REAL observe module runs for the reveal test below \u2014 only the transport is replaced, so the
// assertion is about the beacon that actually goes out, not about a mock being called.
vi.mock('../../utils/api', async (orig) => ({ ...(await orig()), authFetch: h.authFetch }))
vi.mock('../../components/layout/Navbar', () => ({ default: () => null }))
// Capture EVERY prop the preview is handed — the isolation assertion is about what it is fed.
vi.mock('../../components/LivePreview', () => ({ default: (props) => { h.previewProps.push(props); return null } }))
vi.mock('../../utils/attachmentStore', async (orig) => ({ ...(await orig()), buildUserParts: h.buildUserParts }))
// `switchMode` is GONE from this list: the route it posted to no longer exists, and a
// chat's kind can't change after creation, so there is nothing left for a mock to intercept.
vi.mock('../../utils/turnStreamApi', async (orig) => ({
  ...(await orig()),
  startTurn: (...a) => h.startTurn(...a),
  readTurnStream: (...a) => h.readTurnStream(...a),
  buildFromPlan: (...a) => h.buildFromPlan(...a),
  resolvePlanOptions: (...a) => h.resolvePlanOptions(...a),
}))

import ConversationSurface from '../../components/chat/ConversationSurface'
import { TurnStartError } from '../../utils/turnStreamApi'

function makeDeps() {
  const fake = new FakeEventSource('x')
  return { client: makeClient(h), eventSourceFactory: () => fake }
}

function renderHandoff({ chatId = 'build-X', prompt = 'build me a gate tracker' } = {}) {
  return render(
    <MemoryRouter initialEntries={[{ pathname: `/chat/${chatId}`, search: '?projectId=p1&kind=build', state: { prompt, theme: 'bial' } }]}>
      <Routes>
        {inWorkspace(<Route path="/chat/:chatId" element={<ConversationSurface projectId="p1" projectName="VIP Movement" buildSessionDeps={makeDeps()} />} />)}
        <Route path="/projects/:projectId" element={<div>project home</div>} />
        <Route path="/projects" element={<div data-testid="projects-index">projects index</div>} />
      </Routes>
    </MemoryRouter>,
  )
}

/** Confirm the brief card the current turn produced — the page's only build trigger. */
async function confirmBrief() {
  fireEvent.click(await screen.findByRole('button', { name: /^Build this plan$/ }))
}

beforeEach(() => {
  vi.clearAllMocks()
  h.previewProps.length = 0
  h.authFetch.mockResolvedValue({ ok: true })
  Element.prototype.scrollIntoView = vi.fn()
  primeClient(h)
  h.getBuild.mockResolvedValue(null)
  h.loadBuilds.mockResolvedValue([])
  h.listProjectConversations.mockResolvedValue([])
  h.createConversation.mockResolvedValue({ id: 'build-X' })
  h.buildUserParts.mockImplementation(async (text) => [{ type: 'text', text }])
  // Every turn answers with a ready-to-build brief, so a single turn reaches the card these
  // guards need. Whether the model asks or briefs is its own judgment, pinned separately at
  // `backend/tests/services/agent/test_mode_prompts.py`.
  primeTurn(h)
})
afterEach(() => cleanup())

describe('BuilderPage — the seed turn is filed under a project', () => {
  it('creates the chat row FIRST — before the upload and the turn — then the confirmed brief starts the build', async () => {
    // The row is no longer a `create` block riding the turn: an upload has to name a conversation
    // the server has already written, so creation is its own call and it comes first.
    // No title rides it — the heading is derived from the draft, which is not known a round trip
    // earlier; the turn that follows is what names the row.
    renderHandoff()
    await waitFor(() => expect(h.startTurn).toHaveBeenCalled())
    expect(h.createConversation).toHaveBeenCalledWith({ id: 'build-X', projectId: 'p1', kind: 'build' })
    // ORDER IS THE POINT: create → upload → turn. Asserting only that all three ran would pass on
    // the ordering that put a file in front of a row that did not exist yet.
    expect(h.createConversation.mock.invocationCallOrder[0])
      .toBeLessThan(h.buildUserParts.mock.invocationCallOrder[0])
    expect(h.buildUserParts.mock.invocationCallOrder[0])
      .toBeLessThan(h.startTurn.mock.invocationCallOrder[0])
    expect(h.startTurn.mock.calls[0][0]).toBe('build-X')

    // The handed-off prompt is an interview turn, so nothing builds until the card is confirmed —
    // and what builds is the model's REFINED brief, not the raw handoff text.
    expect(h.buildFromPlan).not.toHaveBeenCalled()
    await confirmBrief()
    await waitFor(() => expect(h.buildFromPlan).toHaveBeenCalledWith('build-X', PLAN_CARD_ID, expect.any(String)))
  })

  it('persists the user turn (row + message, in ONE call) before any build', async () => {
    renderHandoff()
    await waitFor(() => expect(h.startTurn).toHaveBeenCalled())
    await confirmBrief()
    await waitFor(() => expect(h.buildFromPlan).toHaveBeenCalled())

    expect(h.startTurn.mock.invocationCallOrder[0]).toBeLessThan(h.buildFromPlan.mock.invocationCallOrder[0])
  })
})

describe('BuilderPage — a refused first-message turn aborts cleanly (was "an append failure aborts the turn")', () => {
  // Row creation is its own call ahead of the upload now, so a first send has TWO refusable
  // doors. The create's arm is pinned first; the rest reject `h.startTurn` directly.
  it('a refused row creation never uploads, and never starts a turn', async () => {
    // The create shares the upload's catch on purpose: the server's own sentence reaches the
    // banner and nothing downstream runs. Falling through would put a file — and a turn —
    // against a conversation row that does not exist.
    h.createConversation.mockRejectedValue(new Error('Project not found.'))
    renderHandoff()

    expect(await screen.findByText('Project not found.')).toBeTruthy()
    await act(async () => { await Promise.resolve() })
    expect(h.buildUserParts).not.toHaveBeenCalled()
    expect(h.startTurn).not.toHaveBeenCalled()
    expect(h.buildFromPlan).not.toHaveBeenCalled()
  })

  it('never reaches a build the server refused to create a row for (network error)', async () => {
    h.startTurn.mockRejectedValue(new Error('network down'))
    renderHandoff()
    // A generic (non-`TurnStartError`) rejection falls to `fireRelayTurn`'s shared fallback copy —
    // the same one every other refused send in this file's sibling suites shows.
    expect(await screen.findByText(/could not be sent/i)).toBeTruthy()
    await act(async () => { await Promise.resolve() })
    expect(h.startTurn).toHaveBeenCalledTimes(1)
    // No relay reply ever arrives, hence no card, hence no build.
    expect(screen.queryByTestId('build-brief-card')).toBeNull()
    expect(h.buildFromPlan).not.toHaveBeenCalled()
  })

  it('ABORTS the seeded send when the attachment upload fails — never a text-only build', async () => {
    // Silently swallowing this failure would build "from your description only" — a handed-off
    // prompt plus a file the build never saw. The upload happens BEFORE `startTurn`, so the seed
    // must abort exactly like the send path does.
    h.buildUserParts.mockRejectedValue(new Error('Attachment storage is full.'))
    renderHandoff()

    expect(await screen.findByText(/Attachment storage is full./i)).toBeTruthy()
    await act(async () => { await Promise.resolve() })
    expect(h.startTurn).not.toHaveBeenCalled()
    expect(h.buildFromPlan).not.toHaveBeenCalled()
  })

  it('a seed abort does not wedge the composer — the next send still reaches a build', async () => {
    // An abort that left the send path latched would force a reload: the toast would tell the user
    // to retry something they cannot retry.
    h.buildUserParts.mockRejectedValueOnce(new Error('Attachment storage is full.'))
    renderHandoff()
    await screen.findByText(/Attachment storage is full./i)

    h.buildUserParts.mockImplementation(async (text) => [{ type: 'text', text }])
    await sendAndConfirm('try again without the file')

    await waitFor(() => expect(h.buildFromPlan).toHaveBeenCalledWith('build-X', PLAN_CARD_ID, expect.any(String)))
  })

  it('leaves for /projects when the turn 404s (project deleted)', async () => {
    // `startTurn` throws `TurnStartError`, never `ApiError` — `isConversationGone` in
    // chatErrors.ts must recognise `TurnStartError` too, or a deleted project's 404 strands the
    // citizen on a chat that refuses every future send instead of routing them back.
    h.startTurn.mockRejectedValue(new TurnStartError(404, 'Project not found.'))
    renderHandoff()
    expect(await screen.findByTestId('projects-index')).toBeTruthy()
    expect(h.buildFromPlan).not.toHaveBeenCalled()
  })

  it("shows the server's own 400 message rather than blaming the connection", async () => {
    // Only a `TurnStartError` reaches `err.message` verbatim in `fireRelayTurn`'s catch — a
    // plain `Error`/`ApiError` falls to the generic "could not be sent" copy instead.
    h.startTurn.mockRejectedValue(new TurnStartError(400, 'header.projectId is required'))
    renderHandoff()
    expect(await screen.findByText('header.projectId is required')).toBeTruthy()
    expect(h.buildFromPlan).not.toHaveBeenCalled()
  })
})

describe('BuilderPage — the way out of a flat chat URL', () => {
  it('the surface itself draws no back link — the toolbar row does', async () => {
    // The back control (and its unsaved-work guard) lives in `WorkspaceToolbar.test.tsx` now.
    // Paired with a liveness check here, because "the link is gone" also passes on a surface
    // that rendered nothing at all.
    renderHandoff()
    await screen.findByPlaceholderText(/ask for another change/i)
    expect(screen.queryByRole('link', { name: /VIP Movement/i })).toBeNull()
  })
})

describe('BuilderPage — a refine turn', () => {
  it('sends projectId (no title) on a subsequent turn and starts with {projectId, prompt}', async () => {
    h.getBuild.mockResolvedValue({ id: 'build-X', kind: 'build', messages: [{ id: 'm0', role: 'user', parts: [{ type: 'text', text: 'hi' }], seq: 0 }] })
    render(
      <MemoryRouter initialEntries={['/chat/build-X']}>
        <Routes>
          {inWorkspace(<Route path="/chat/:chatId" element={<ConversationSurface projectId="p1" projectName="VIP Movement" buildSessionDeps={makeDeps()} />} />)}
        </Routes>
      </MemoryRouter>,
    )
    await screen.findByPlaceholderText(/ask for another change/i)
    await sendAndConfirm('make it blue')

    await waitFor(() => expect(h.buildFromPlan).toHaveBeenCalled())
    // A subsequent turn on an existing thread creates nothing. The row is already written, so a
    // create call here would be a round trip bought for nothing — and, against a server that
    // answers an existing id idempotently, an invisible one.
    expect(h.createConversation).not.toHaveBeenCalled()
    expect(h.buildFromPlan).toHaveBeenCalledWith('build-X', PLAN_CARD_ID, expect.any(String))
  })
})

describe('BuilderPage — the preview is fed NO app credentials', () => {
  it('never hands LivePreview a config / appKey / accessToken / previewCode', async () => {
    renderHandoff()
    await confirmBrief()
    await waitFor(() => expect(h.buildFromPlan).toHaveBeenCalled())
    // Provisioning is subsumed by the build-session start; the owner surface is pinned
    // separately by appRegistryApi.test.js.
    for (const props of h.previewProps) {
      expect(props.config).toBeUndefined()
      expect(props.appKey).toBeUndefined()
      expect(props.accessToken).toBeUndefined()
      expect(props.previewCode).toBeUndefined()
    }
  })
})

describe('BuilderPage — the preview is handed the first-view stop-clock', () => {
  it('\u2605 passes LivePreview a reveal callback \u2014 without it the first-view measurement is dead', async () => {
    // This is the ONLY production mount of LivePreview in the tree, so a callback added to the
    // component and never passed here is a counter that never fires and a suite that never notices.
    //
    // Deliberately not asserting what the callback DOES \u2014 that decision is pinned in `observe.ts`.
    // What can only be checked here is that the wire exists.
    renderHandoff()
    await screen.findByPlaceholderText(/ask for another change/i)

    expect(h.previewProps.length).toBeGreaterThan(0)
    for (const props of h.previewProps) {
      expect(typeof props.onRevealed).toBe('function')
    }
  })

  it('\u2605 and the callback it passes marks THIS project\u2019s app as seen', async () => {
    // The prior test only proves the wire exists \u2014 \u2018connected to the wrong project id\u2019 is a
    // silent corruption this one catches by opening the project for real, then INVOKING the
    // callback the mount actually handed the pane.
    const { markProjectOpened } = await import('../../utils/observe')
    markProjectOpened('p1', { hasApp: true })
    h.authFetch.mockClear()

    renderHandoff()
    await screen.findByPlaceholderText(/ask for another change/i)
    const { onRevealed } = h.previewProps[h.previewProps.length - 1]
    onRevealed()

    const sent = h.authFetch.mock.calls
      .filter(([url]) => url === '/api/observations')
      .map(([, opts]) => JSON.parse(String(opts.body)))
    expect(sent).toHaveLength(1)
    expect(sent[0].name).toBe('project_to_app_visible_ms')
    expect(sent[0].value).toBeGreaterThanOrEqual(0)
  })
})

describe('BuilderPage — the composer is not shared across a chat navigation', () => {
  function BuilderHost() {
    const { chatId } = useParams()
    return <ConversationSurface chatId={chatId} projectId="p1" projectName="P" buildSessionDeps={makeDeps()} />
  }
  function GoToB() {
    const navigate = useNavigate()
    return <button onClick={() => navigate('/chat/chat-B')}>go to B</button>
  }

  it('a seed upload that fails AFTER a chat switch does not clobber the adopted chat', async () => {
    // The seed abort rolls the optimistic message back using `provisional`/`userSeq`, which
    // describe the chat the seed started in — writing them after a navigation would wipe the
    // transcript of the chat now on screen.
    h.getBuild.mockImplementation(async (id) =>
      id === 'chat-B' ? { id: 'chat-B', kind: 'build', messages: [{ id: 'm0', role: 'assistant', parts: [{ type: 'text', text: 'CHAT B TRANSCRIPT' }], seq: 0 }] } : null,
    )
    let failUpload
    h.buildUserParts.mockReturnValue(new Promise((_resolve, reject) => { failUpload = reject }))
    render(
      <MemoryRouter initialEntries={[{ pathname: '/chat/chat-A', search: '?projectId=p1&kind=build', state: { prompt: 'seed for A', theme: 'bial' } }]}>
        <GoToB />
        <Routes>
          {inWorkspace(<Route path="/chat/:chatId" element={<BuilderHost />} />)}
        </Routes>
      </MemoryRouter>,
    )
    await waitFor(() => expect(h.buildUserParts).toHaveBeenCalled())

    fireEvent.click(screen.getByText('go to B'))
    await screen.findByText('CHAT B TRANSCRIPT')
    await act(async () => { failUpload(new Error('Attachment storage is full.')); await Promise.resolve() })

    expect(screen.getByText('CHAT B TRANSCRIPT')).toBeTruthy() // B's transcript survived
    expect(h.startTurn).not.toHaveBeenCalled()
    expect(h.buildFromPlan).not.toHaveBeenCalled()
  })

  it('drops a typed draft when the same instance adopts /chat/A → /chat/B', async () => {
    h.getBuild.mockResolvedValue(null)
    render(
      <MemoryRouter initialEntries={['/chat/chat-A']}>
        <GoToB />
        <Routes>
          {inWorkspace(<Route path="/chat/:chatId" element={<BuilderHost />} />)}
        </Routes>
      </MemoryRouter>,
    )
    const composer = await screen.findByPlaceholderText(/ask for another change/i)
    fireEvent.change(composer, { target: { value: 'a draft meant only for chat A' } })
    expect(composer.value).toBe('a draft meant only for chat A')

    fireEvent.click(screen.getByText('go to B'))
    await waitFor(() => expect(screen.getByPlaceholderText(/ask for another change/i).value).toBe(''))
  })
})

describe('BuilderPage — the StrictMode load strand', () => {
  const SAVED = { id: 'build-X', kind: 'build', messages: [{ id: 'm0', role: 'assistant', parts: [{ type: 'text', text: 'SAVED TRANSCRIPT LINE' }], seq: 0 }] }

  it('renders a saved transcript under <StrictMode>', async () => {
    h.getBuild.mockResolvedValue(SAVED)
    render(
      <StrictMode>
        <MemoryRouter initialEntries={['/chat/build-X']}>
          <Routes>
            {inWorkspace(<Route path="/chat/:chatId" element={<ConversationSurface projectId="p1" projectName="P" buildSessionDeps={makeDeps()} />} />)}
          </Routes>
        </MemoryRouter>
      </StrictMode>,
    )
    expect((await screen.findAllByText('SAVED TRANSCRIPT LINE')).length).toBeGreaterThan(0)
  })

  it('fires the handoff seed exactly once under <StrictMode> (no double-turn)', async () => {
    h.getBuild.mockResolvedValue(null)
    render(
      <StrictMode>
        <MemoryRouter initialEntries={[{ pathname: '/chat/build-X', search: '?projectId=p1&kind=build', state: { prompt: 'build me a gate tracker', theme: 'bial' } }]}>
          <Routes>
            {inWorkspace(<Route path="/chat/:chatId" element={<ConversationSurface projectId="p1" projectName="VIP" buildSessionDeps={makeDeps()} />} />)}
          </Routes>
        </MemoryRouter>
      </StrictMode>,
    )
    await waitFor(() => expect(h.startTurn).toHaveBeenCalled())
    await act(async () => { await Promise.resolve() })
    // A remounted effect must not re-send the handed-off prompt: a doubled seed bills the user
    // for two relay turns and leaves the thread arguing with itself over two briefs.
    expect(h.startTurn).toHaveBeenCalledTimes(1)
    // And the row is created once too — a doubled create is a second POST the server has to
    // absorb, and the only reason it is harmless is idempotency this suite does not own.
    expect(h.createConversation).toHaveBeenCalledTimes(1)
    expect(h.createConversation).toHaveBeenCalledWith({ id: 'build-X', projectId: 'p1', kind: 'build' })
  })
})

describe('BuilderPage — a send blocked by an in-flight reply explains itself', () => {
  it('toasts instead of silently dropping the Enter while the assistant is still replying', async () => {
    h.getBuild.mockResolvedValue({ id: 'build-X', kind: 'build', messages: [{ id: 'm0', role: 'user', parts: [{ type: 'text', text: 'hi' }], seq: 0 }] })
    h.readTurnStream.mockImplementation(() => new Promise(() => {})) // the reply never lands → `generating` stays true

    render(
      <MemoryRouter initialEntries={['/chat/build-X']}>
        <Routes>
          {inWorkspace(<Route path="/chat/:chatId" element={<ConversationSurface projectId="p1" projectName="P" buildSessionDeps={makeDeps()} />} />)}
        </Routes>
      </MemoryRouter>,
    )
    await screen.findByPlaceholderText(/ask for another change/i)
    await send('first')
    await waitFor(() => expect(h.startTurn).toHaveBeenCalledTimes(1))

    await send('second')

    expect(await screen.findByText(/send unlocks when it is done/i)).toBeTruthy()
    expect(h.startTurn).toHaveBeenCalledTimes(1) // the blocked send never re-entered
    // The second message is still in the box — the user composed it while waiting, which is
    // exactly what the mode-free contract invites them to do.
    expect(screen.getByPlaceholderText(/ask for another change/i).value).toBe('second')
  })
})

// `BuilderPage` strips the handed-off draft with a raw `window.history.replaceState`, which
// emits no popstate, so react-router's in-memory `location.state` survives it and
// `useDropTransientQuery` re-writes that survivor back into history. This fires on exactly the
// FIRST reload, which is why the tests below need the full mount-drop-remount cycle rather than
// a single render.
describe('BuilderPage — the hand-off does not replay on reload', () => {
  function LocationProbe({ sink }) {
    sink.current = useLocation()
    return null
  }

  const renderAt = (entry, sink) =>
    render(
      <MemoryRouter initialEntries={[entry]}>
        <LocationProbe sink={sink} />
        <Routes>
          {inWorkspace(
            <Route
              path="/chat/:chatId"
              element={<ConversationSurface projectId="p1" projectName="VIP Movement" buildSessionDeps={makeDeps()} />}
            />,
          )}
          <Route path="/projects" element={<div data-testid="projects-index">projects index</div>} />
        </Routes>
      </MemoryRouter>,
    )

  const HANDOFF_ENTRY = {
    pathname: '/chat/build-X',
    search: '?projectId=p1&kind=build',
    state: { prompt: 'reply with exactly the word OK', theme: 'bial', pendingAttachments: [] },
  }

  it('the post-drop history entry carries no prompt, and the URL is clean', async () => {
    const sink = { current: null }
    renderAt(HANDOFF_ENTRY, sink)

    await waitFor(() => expect(h.startTurn).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(sink.current.search).toBe(''))
    expect(sink.current.state?.prompt).toBeUndefined()
  })

  it('THE BUG: remounting over the dropped entry starts NO second turn', async () => {
    const sink = { current: null }
    renderAt(HANDOFF_ENTRY, sink)
    await waitFor(() => expect(h.startTurn).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(sink.current.search).toBe(''))

    // A reload is a fresh mount over the SAME history entry, and the browser keeps router state
    // across it — so replay the entry the drop actually left behind. By now the server has the
    // row, which is what a reloading user's page would find.
    const dropped = { pathname: sink.current.pathname, search: sink.current.search, state: sink.current.state }
    h.getBuild.mockResolvedValue({
      id: 'build-X',
      kind: 'build',
      messages: [{ id: 'm0', role: 'user', seq: 0, parts: [{ type: 'text', text: 'reply with exactly the word OK' }] }],
    })
    cleanup()
    h.startTurn.mockClear()

    const reloadSink = { current: null }
    renderAt(dropped, reloadSink)
    await screen.findByPlaceholderText(/ask for another change/i)
    await act(async () => { await Promise.resolve() })

    expect(h.startTurn).not.toHaveBeenCalled()
  })

  it('attachments handed off with the prompt are consumed by the FIRST turn and not re-fired', async () => {
    const sink = { current: null }
    renderAt(
      { ...HANDOFF_ENTRY, state: { ...HANDOFF_ENTRY.state, pendingAttachments: [{ name: 'floorplan.png', dataUrl: 'data:image/png;base64,AA' }] } },
      sink,
    )
    await waitFor(() => expect(h.buildUserParts).toHaveBeenCalled())
    const [, attachments] = h.buildUserParts.mock.calls[0]
    expect(attachments).toHaveLength(1)

    await waitFor(() => expect(sink.current.state?.pendingAttachments).toBeUndefined())
  })
})
