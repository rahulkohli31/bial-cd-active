/**
 * Project-first guards for the builder, re-expressed against the C3 build session (U5) and the
 * relay-then-card trigger (003-U4): a send is a CHAT turn, and a build starts only when the user
 * confirms the brief card the model replies with.
 *
 * Preserved invariants (each fails SILENTLY otherwise):
 *  1. The seed turn is filed under a project (`header.projectId`), and an append failure ABORTS the
 *     turn — the build is never STARTED against a conversation row the server cannot find.
 *  2. The user turn is PERSISTED before the relay reads it — the append upserts the conversation
 *     header, so the row must exist by the time `POST /v1/claude` folds in the project's
 *     description + the interview protocol — and therefore before any build the card can trigger.
 *  3. Navigating between two chats never leaks one chat's composer draft into the other.
 *  4. INERTNESS: the builder feeds the preview NO app credentials (`config`/`appKey`/`accessToken`)
 *     — the app gets its data credentials server-side at provision (C9), and provisionApp is no
 *     longer called from the build path at all.
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
  loadBuilds: vi.fn(), newBuild: vi.fn(), createBuild: vi.fn(), getBuild: vi.fn(),
  deleteBuild: vi.fn(), listProjectConversations: vi.fn(), buildUserParts: vi.fn(),
  sendMessage: vi.fn(),
  startTurn: vi.fn(), readTurnStream: vi.fn(), buildFromPlan: vi.fn(),
  resolvePlanOptions: vi.fn(),
  previewProps: [],
  authFetch: vi.fn(),
  start: vi.fn(), stop: vi.fn(), getStatus: vi.fn(), forceEnd: vi.fn(),
}))

vi.mock('../../utils/builderHistory', () => ({
  loadBuilds: h.loadBuilds, newBuild: h.newBuild, createBuild: h.createBuild,
  getBuild: h.getBuild, deleteBuild: h.deleteBuild, deriveTitle: (t) => (t || '').slice(0, 40),
}))
// SPREAD THE ORIGINAL — `handleBuildIt` mints the new build chat's id through the shared
// `uuidv7` (ADR-0006), and a factory naming only `listProjectConversations` leaves every other
// export (including that one) undefined; Vitest now warns the moment a real caller reaches for
// it, which every confirmed brief in this suite does.
vi.mock('../../utils/conversationApi', async (importOriginal) => ({
  ...(await importOriginal()),
  listProjectConversations: h.listProjectConversations,
}))
// The REAL observe module runs for the reveal test below \u2014 only the transport is replaced, so the
// assertion is about the beacon that actually goes out, not about a mock being called.
vi.mock('../../utils/api', async (orig) => ({ ...(await orig()), authFetch: h.authFetch }))
vi.mock('../../components/layout/Navbar', () => ({ default: () => null }))
// Capture EVERY prop the preview is handed — the isolation assertion is about what it is fed.
vi.mock('../../components/LivePreview', () => ({ default: (props) => { h.previewProps.push(props); return null } }))
vi.mock('../../utils/attachmentStore', async (orig) => ({ ...(await orig()), buildUserParts: h.buildUserParts }))
// `switchMode` is GONE from this list (U1/U19): the route it posted to no longer exists, and a
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
  h.newBuild.mockReturnValue('build-N')
  h.createBuild.mockResolvedValue({ ok: true })
  h.getBuild.mockResolvedValue(null)
  h.loadBuilds.mockResolvedValue([])
  h.listProjectConversations.mockResolvedValue([])
  h.buildUserParts.mockImplementation(async (text) => [{ type: 'text', text }])
  // The scripted turn: every turn answers with a ready-to-build brief, so a single turn reaches
  // the card these guards need. (Whether the model asks or briefs is its own judgment, and it is
  // a property of the server-composed prompt — `backend/tests/services/agent/test_mode_prompts.py`.
  // The relay suite this used to point at was retired with the relay.)
  primeTurn(h)
})
afterEach(() => cleanup())

describe('BuilderPage — the seed turn is filed under a project', () => {
  it('sends the create block (projectId + title) on the turn\'s own FIRST call, then the confirmed brief starts the build (R-18/U13, was "the create branch")', async () => {
    // R-18: there is no separate `createBuild` round trip left to carry `header.projectId` /
    // `title` — they ride the turn's OWN `POST .../turns` as its `create` block instead, so the
    // server can check the workspace BEFORE creating the row (see `fireRelayTurn`'s R-18 comment).
    renderHandoff()
    await waitFor(() => expect(h.startTurn).toHaveBeenCalled())
    const [id, , , create] = h.startTurn.mock.calls[0]
    expect(id).toBe('build-X')
    expect(create.projectId).toBe('p1')
    expect(create.title).toBeTruthy()

    // The handed-off prompt is an interview turn, so nothing builds until the card is confirmed —
    // and what builds is the model's REFINED brief, not the raw handoff text.
    expect(h.buildFromPlan).not.toHaveBeenCalled()
    await confirmBrief()
    await waitFor(() => expect(h.buildFromPlan).toHaveBeenCalledWith('build-X', PLAN_CARD_ID, expect.any(String)))
  })

  it('persists the user turn (row + message, in ONE call) before any build (R-18/U13, was "before the relay reads it")', async () => {
    // The two-call ordering this used to prove — append, THEN the relay reads what it wrote — is
    // gone with the append: the row's creation and the turn's own POST are the SAME call now, so
    // there is nothing left to order the turn against except the LATER confirmed-brief call.
    renderHandoff()
    await waitFor(() => expect(h.startTurn).toHaveBeenCalled())
    await confirmBrief()
    await waitFor(() => expect(h.buildFromPlan).toHaveBeenCalled())

    expect(h.startTurn.mock.invocationCallOrder[0]).toBeLessThan(h.buildFromPlan.mock.invocationCallOrder[0])
  })
})

describe('BuilderPage — a refused first-message turn aborts cleanly (was "an append failure aborts the turn")', () => {
  // R-18/U13 RETIRES THE SEPARATE APPEND THIS DESCRIBE BLOCK WAS NAMED FOR. There is no longer a
  // `createBuild` call whose failure can leave `startTurn` unreached — the row's creation and the
  // turn's own POST are the SAME call, so a refusal that used to stop the append before the relay
  // was ever asked now IS a refused `startTurn`. Every test below rejects `h.startTurn` instead of
  // `h.createBuild`, and drops the old "startTurn was never called" assertion — it now WAS called,
  // and it is the one that failed.
  it('never reaches a build the server refused to create a row for (network error)', async () => {
    h.startTurn.mockRejectedValue(new Error('network down'))
    renderHandoff()
    // "Could not start this thread" was the retired create call's OWN sentence. A generic
    // (non-`TurnStartError`) rejection now falls to `fireRelayTurn`'s shared fallback copy —
    // the same one every other refused send in this file's sibling suites shows.
    expect(await screen.findByText(/could not be sent/i)).toBeTruthy()
    await act(async () => { await Promise.resolve() })
    expect(h.startTurn).toHaveBeenCalledTimes(1)
    // No relay reply ever arrives, hence no card, hence no build.
    expect(screen.queryByTestId('build-brief-card')).toBeNull()
    expect(h.buildFromPlan).not.toHaveBeenCalled()
  })

  it('ABORTS the seeded send when the attachment upload fails — never a text-only build (R3)', async () => {
    // This path used to swallow the failure and build "from your description only": the user
    // handed off a prompt + a spreadsheet from the project page, the upload failed, and a build
    // ran that never saw the file. Silently ignoring an attachment is the exact bug R3 deletes,
    // so the seed must abort exactly like the send path does.
    //
    // UNCHANGED BY R-18: the upload happens BEFORE `startTurn` is ever reached (`buildUserParts`
    // throws inside `fireRelayTurn` ahead of the turn call entirely), so this scenario never
    // touched the create/append protocol either before or after the collapse.
    h.buildUserParts.mockRejectedValue(new Error('Attachment storage is full.'))
    renderHandoff()

    expect(await screen.findByText(/Attachment storage is full./i)).toBeTruthy()
    await act(async () => { await Promise.resolve() })
    expect(h.startTurn).not.toHaveBeenCalled()
    expect(h.buildFromPlan).not.toHaveBeenCalled()
  })

  it('a seed abort does not wedge the composer — the next send still reaches a build (R3)', async () => {
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
    // A REAL BUG THIS SUITE'S OLD SHAPE WAS MASKING: `startTurn` throws `TurnStartError` for
    // every non-ok response, never `ApiError` — so `isConversationGone` (written for the retired
    // `createBuild`'s `ApiError`) silently never matched a `startTurn` refusal, and a deleted
    // project's 404 stranded the citizen on a chat that would refuse every future send the same
    // way instead of sending them back to `/projects`. Fixed in `chatErrors.ts`'s
    // `isConversationGone`, which now recognises `TurnStartError` too — this test asserts the
    // navigation it silently stopped performing.
    h.startTurn.mockRejectedValue(new TurnStartError(404, 'Project not found.'))
    renderHandoff()
    expect(await screen.findByTestId('projects-index')).toBeTruthy()
    expect(h.buildFromPlan).not.toHaveBeenCalled()
  })

  it("shows the server's own 400 message rather than blaming the connection", async () => {
    // A raw `ApiError` would not reproduce this any more — the real `startTurn` throws
    // `TurnStartError` for every non-ok response, and only a `TurnStartError` reaches
    // `err.message` verbatim in `fireRelayTurn`'s catch (a plain `Error`/`ApiError` falls to the
    // generic "could not be sent" copy instead).
    h.startTurn.mockRejectedValue(new TurnStartError(400, 'header.projectId is required'))
    renderHandoff()
    expect(await screen.findByText('header.projectId is required')).toBeTruthy()
    expect(h.buildFromPlan).not.toHaveBeenCalled()
  })
})

describe('BuilderPage — the way out of a flat chat URL', () => {
  it('the surface itself draws no back link — the toolbar row does', async () => {
    // Plan 002's U2 moved it: the row above both columns carries the project, the chat's kind and
    // the chat's title, so the surface no longer draws a header at all. Where the back control
    // goes, and that it routes through the unsaved-work guard, is `WorkspaceToolbar.test.tsx`'s.
    //
    // Paired with a liveness check, because "the link is gone" also passes when the surface
    // rendered nothing at all.
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
    // U7/R-18: a subsequent turn on an existing thread creates nothing — the row already exists,
    // so `create` (the turn call's 4th argument) is omitted rather than passed. There is no
    // longer a separate `createBuild` call to assert absent; the ONE call this send makes is
    // `startTurn`, and this is the shape it takes when the thread is not empty.
    const [, , , create] = h.startTurn.mock.calls[0]
    expect(create).toBeUndefined()
    expect(h.buildFromPlan).toHaveBeenCalledWith('build-X', PLAN_CARD_ID, expect.any(String))
  })
})

describe('BuilderPage — the preview is fed NO app credentials (C9 server-side, U5 inertness)', () => {
  it('never hands LivePreview a config / appKey / accessToken / previewCode', async () => {
    renderHandoff()
    await confirmBrief()
    await waitFor(() => expect(h.buildFromPlan).toHaveBeenCalled())
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

describe('BuilderPage — the preview is handed R104\u2019s stop-clock (U4)', () => {
  it('\u2605 passes LivePreview a reveal callback \u2014 without it the first-view measurement is dead', async () => {
    // THIS MOUNT IS THE ONLY PRODUCTION MOUNT OF LivePreview IN THE TREE, so a callback added to
    // the component and never passed here is a counter that never fires and a test suite that
    // never notices. Asserted against the RECORDED PROPS rather than by reading the file, and
    // this suite already stubs the pane to record them.
    //
    // Deliberately not asserting what the callback DOES: that decision lives in `observe.ts` and
    // is pinned there. What can only be checked here is that the wire exists.
    renderHandoff()
    await screen.findByPlaceholderText(/ask for another change/i)

    expect(h.previewProps.length).toBeGreaterThan(0)
    for (const props of h.previewProps) {
      expect(typeof props.onRevealed).toBe('function')
    }
  })

  it('\u2605 and the callback it passes marks THIS project\u2019s app as seen', async () => {
    // Asserting the prop is a function only proves a wire exists; it does not prove the wire is
    // connected to anything, and \u2018connected to the wrong project id\u2019 is a silent corruption of
    // the only R104 number there is. So: open the project for real through the observe module,
    // then INVOKE the callback the mount actually handed the pane.
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

  it('a seed upload that fails AFTER a chat switch does not clobber the adopted chat (R3)', async () => {
    // The seed abort rolls the optimistic message back — but `provisional`/`userSeq` describe the
    // chat the seed started in. If the user navigated away while the upload was in flight, writing
    // them would wipe the transcript of the chat now on screen.
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

describe('BuilderPage — the StrictMode load strand (U7)', () => {
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
    // A remounted effect must not re-send the handed-off prompt: a doubled seed bills the user for
    // two relay turns and leaves the thread arguing with itself over two briefs. R-18/U13 folded
    // "one create for the one seeded first turn" into this same call — `startTurn` firing once
    // IS the row being created once, so there is no second mock left to assert alongside it.
    expect(h.startTurn).toHaveBeenCalledTimes(1)
    expect(h.startTurn.mock.calls[0][3]).toMatchObject({ projectId: 'p1' })
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
    // exactly what the mode-free contract invites them to do (KTD-1).
    expect(screen.getByPlaceholderText(/ask for another change/i).value).toBe('second')
  })
})

// N1 (U3). The deterministic repro, at the page. Three sites chain into it and only one is the
// fix: ProjectBuilder hands the draft off as router state; BuilderPage strips it with a raw
// `window.history.replaceState`, which emits no popstate and so leaves react-router's in-memory
// `location.state` intact; `useDropTransientQuery` then re-wrote that survivor back into history.
// The result fires on exactly the FIRST reload — the second has no query left to drop — which is
// why this needs the full mount-drop-remount cycle rather than a single render.
describe('BuilderPage — the hand-off does not replay on reload (N1)', () => {
  /** Reports the live router location so the test can remount over the entry the drop produced. */
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
