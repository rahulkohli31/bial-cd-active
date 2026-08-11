/**
 * U5→U13 core: Build-it starts a WRITE TURN. The atomic transition records the choice, flips the
 * conversation to Write and starts the turn server-side; the page subscribes to that turn with the
 * very same `readTurnStream` an ordinary send uses, the bubble narrates its `workspace` / `step` /
 * `preview` / `quota` frames, the live preview frames the sandbox URL off the `preview` frame, and
 * Stop is the TURN stop. A build session is not created, joined, or reattached anywhere in this
 * path — `buildFromPlan` hands back a `turnId` and nothing else.
 *
 * The transition's refusals moved with it: they are typed HTTP statuses now (429 daily cap, 409
 * busy workspace, 503 unconfigured), so `buildFromPlan` THROWS and the card re-arms with the
 * server's own message. There is no `build_failed` outcome left to return.
 *
 * The REAL useBuildSession hook + LivePreview + BuildProgress run; only the C3 transport (client +
 * EventSource, still reachable through the legacy reattach path) and the U10 turn transport are
 * mocks.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act, cleanup, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import BuilderPage from '../BuilderPage'
import { ApiError } from '../../utils/apiError'
import {
  FakeEventSource, PREVIEW_URL, makeClient, primeClient, renderBuilder, statusResp,
  PLAN_CARD_ID, planReply, primeTurn, turnStreaming, send, T_DELTA,
  scriptBuildTurn, BUILD_TURN_ID, T_STEP, T_PREVIEW, T_QUOTA, T_BUILD_END, T_WORKSPACE, T_END,
  T_DIAGNOSTIC,
} from './_builderSession.jsx'

const h = vi.hoisted(() => ({
  loadBuilds: vi.fn(),
  newBuild: vi.fn(),
  createBuild: vi.fn(),
  getBuild: vi.fn(),
  deleteBuild: vi.fn(),
  listProjectConversations: vi.fn(),
  buildUserParts: vi.fn(),
  startTurn: vi.fn(),
  readTurnStream: vi.fn(),
  buildFromPlan: vi.fn(),
  stopTurn: vi.fn(),
  switchMode: vi.fn(),
  resolvePlanOptions: vi.fn(),
  start: vi.fn(),
  relaunchPreview: vi.fn(),
  stop: vi.fn(),
  getStatus: vi.fn(),
  forceEnd: vi.fn(),
  acquireLock: vi.fn(),
  renewLock: vi.fn(),
  releaseLock: vi.fn(),
  heartbeat: vi.fn(),
}))

vi.mock('../../utils/builderHistory', () => ({
  loadBuilds: h.loadBuilds,
  newBuild: h.newBuild,
  createBuild: h.createBuild,
  getBuild: h.getBuild,
  deleteBuild: h.deleteBuild,
  deriveTitle: (t) => (t || '').slice(0, 40),
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
  stopTurn: (...a) => h.stopTurn(...a),
  switchMode: (...a) => h.switchMode(...a),
  resolvePlanOptions: (...a) => h.resolvePlanOptions(...a),
}))

function deps() {
  const fake = new FakeEventSource('x')
  return { fake, deps: { client: makeClient(h), eventSourceFactory: () => fake } }
}

/**
 * Get a build running the way a user does now (U11/U12 + U5): send a turn, wait for the plan card,
 * click Build it. The atomic transition starts a WRITE TURN server-side and the page subscribes to
 * it — the C3 client's `start` is never called from this page, and neither is `getStatus`.
 */
async function sendPrompt(text = 'build me a tool') {
  await send(text)
  fireEvent.click(await screen.findByRole('button', { name: /^Build it$/ }))
  await waitFor(() => expect(h.buildFromPlan).toHaveBeenCalled())
}

/** …and the page is genuinely ON the build turn's socket (what "the build is running" means). */
async function awaitBuildTurn(turnId = BUILD_TURN_ID) {
  await waitFor(() => expect(h.readTurnStream).toHaveBeenCalledWith(expect.objectContaining({ turnId })))
}

/** Script the build turn for this test and hand back the open socket to push frames into. */
function scriptedBuild(options) {
  const turn = scriptBuildTurn(options)
  h.readTurnStream.mockImplementation(turn.impl)
  return turn
}

beforeEach(() => {
  vi.clearAllMocks()
  Element.prototype.scrollIntoView = vi.fn()
  primeClient(h)
  h.newBuild.mockReturnValue('build-Y')
  h.createBuild.mockResolvedValue({ ok: true })
  h.deleteBuild.mockResolvedValue(true)
  h.getBuild.mockResolvedValue(null)
  h.loadBuilds.mockResolvedValue([])
  h.listProjectConversations.mockResolvedValue([{ id: 'build-X', kind: 'builder', title: 'My build', updatedAt: new Date().toISOString() }])
  h.buildUserParts.mockImplementation(async (text) => [{ type: 'text', text }])
  // The scripted turn: every send streams a plan + the options card, so these suites reach
  // the session mechanics in one send + one click. The transition answers `started` with the
  // session this page then reattaches (h.getStatus seeds it).
  primeTurn(h)
})
afterEach(() => cleanup())

describe('BuilderPage — the build-turn flow (ORIG-§3-d/f)', () => {
  it('Build it starts a WRITE TURN; its step frames render; the preview frame frames the sandbox URL', async () => {
    const turn = scriptedBuild()
    renderBuilder({ deps: deps().deps })
    await sendPrompt()

    // The build starts through the ATOMIC TRANSITION (record + flip + start, server-side) and the
    // page subscribes to the TURN it returns. Neither C3 door is opened: `start` was already not a
    // client concern, and now `getStatus` isn't either — there is no session to join at all.
    expect(h.buildFromPlan).toHaveBeenCalledWith('build-X', PLAN_CARD_ID)
    expect(h.start).not.toHaveBeenCalled()
    expect(h.getStatus).not.toHaveBeenCalled()
    // Cursor 0 deliberately: the build may have been running for seconds before this subscribe
    // landed, and the consolidating snapshot is what recovers the frames it missed.
    await waitFor(() =>
      expect(h.readTurnStream).toHaveBeenCalledWith(
        expect.objectContaining({ conversationId: 'build-X', turnId: BUILD_TURN_ID, cursor: 0 }),
      ),
    )

    await turn.frame(T_STEP('Scaffolding your app…'), T_STEP('Installing dependencies', { id: 'call-2', seq: 3 }))
    // The rows are actually in the DOM (not just props) — no remount needed.
    expect(await screen.findByText(/Scaffolding your app/i)).toBeTruthy()
    expect(screen.getByText(/Installing dependencies/i)).toBeTruthy()

    await turn.frame(T_PREVIEW())
    await waitFor(() => expect(document.querySelector('iframe')?.getAttribute('src')).toBe(PREVIEW_URL))
  })

  it('a doubly-truncated turn resubscribes once, then surfaces the connection-dropped notice (#28)', async () => {
    const { deps: sessionDeps } = deps()
    // The socket drops before the terminal on BOTH the first read AND the resubscribe.
    h.readTurnStream.mockImplementation(async ({ onFrame }) => {
      onFrame(T_DELTA('partial…'))
      return 'truncated'
    })
    renderBuilder({ deps: sessionDeps })
    await send('just answer me')

    expect(await screen.findByText(/connection dropped\. reload to catch up/i)).toBeTruthy()
    expect(h.readTurnStream).toHaveBeenCalledTimes(2) // one resume-once, then the honest error
  })

  it('Stop → graceful end reflected in the UI (preview reaches the terminal placeholder)', async () => {
    const turn = scriptedBuild()
    renderBuilder({ deps: deps().deps })
    await sendPrompt()
    await awaitBuildTurn()
    await turn.frame(T_PREVIEW())
    await waitFor(() => expect(document.querySelector('iframe')).toBeTruthy())

    // ONE working indicator, ONE way to interrupt it: a build has no separate stop any more, so
    // this is the same `stopTurn` an ordinary reply uses, addressed by the live turn id.
    fireEvent.click(screen.getByRole('button', { name: /^stop$/i }))
    await waitFor(() => expect(h.stopTurn).toHaveBeenCalledWith('build-X', BUILD_TURN_ID))
    expect(h.stop).not.toHaveBeenCalled() // never the C3 session stop

    await turn.frame(T_BUILD_END({ status: 'stopped', reason: 'stopped_by_user' }))
    await turn.end()
    // getAllBy: the pane now also ANNOUNCES its state through a persistent role="status"
    // region, so the terminal sentence legitimately appears twice — once on screen, once for
    // a screen reader that would otherwise be told nothing at all.
    await waitFor(() => expect(screen.getAllByText(/no longer running/i).length).toBeGreaterThan(0))
    expect(document.querySelector('iframe')).toBeNull() // terminal collapses the dead frame
  })

  it('a COMPLETED build keeps the preview framed — "done, preview live", never "no longer running" (#13/R2)', async () => {
    const turn = scriptedBuild()
    renderBuilder({ deps: deps().deps })
    await sendPrompt()
    await awaitBuildTurn()
    await turn.frame(T_PREVIEW())
    await waitFor(() => expect(document.querySelector('iframe')).toBeTruthy())

    await turn.frame(T_BUILD_END({ status: 'completed' }))
    await turn.end()
    // The server PARDONS a completed build's container (idle lease), so the frame stays live
    // with the honest completion chip — only stop/force-end/failure collapse to the placeholder.
    // The lesson this pins is the framedStatus one: "the turn is over" must never be flattened
    // into "the app is gone" while the URL the user is looking at still serves.
    await waitFor(() => expect(screen.getByText(/your app is live below/i)).toBeTruthy())
    expect(document.querySelector('iframe')?.getAttribute('src')).toBe(PREVIEW_URL)
    expect(screen.queryByText(/no longer running/i)).toBeNull()
  })

  // ('Force-end → the kill switch confirms, then ends the session' is RETIRED with U5.) It drove
  // `session.forceEnd`, the C3 kill switch that tears a build SESSION's sandbox down out of band —
  // and the composer-initiated build path no longer has a session to tear down, nor a turn-level
  // equivalent of one. `stopTurn` is the whole interrupt vocabulary a build turn has, and the Stop
  // test above is what pins it. The kill switch still belongs to the legacy session surfaces
  // (SessionBanners' block/reclaim arms), which reach it by session id and are tested there.

  it('U4: a self-heal diagnostic renders as a RETRY mid-build, and leaves no residue after completion', async () => {
    const turn = scriptedBuild()
    renderBuilder({ deps: deps().deps })
    await sendPrompt()
    await awaitBuildTurn()

    await turn.frame(T_STEP('Scaffolding your app…'), T_DIAGNOSTIC('Type error in app/page.tsx'))
    // Retry framing in citizen language — never the terminal red block ("the turn is not
    // failing — a repair run follows" is what the wire says).
    expect(await screen.findByText(/trying another way/i)).toBeTruthy()
    expect(document.querySelector('[data-kind="retry"]')).toBeTruthy()
    expect(document.querySelector('[data-kind="error"]')).toBeNull()
    // The detail renders ONCE — the title line, no monospace copy of the same string.
    expect(document.querySelector('[data-kind="retry"] pre')).toBeNull()
    expect(screen.getAllByText(/Type error in app\/page\.tsx/i)).toHaveLength(1)

    // The repair succeeds and the turn completes: no residual failure presentation.
    await turn.frame(T_BUILD_END())
    await turn.end()
    await waitFor(() => expect(document.querySelector('[data-kind="retry"]')).toBeNull())
    expect(document.querySelector('[data-kind="error"]')).toBeNull()
  })

  it('U4 mirror: repair exhausted → turn_ended(failed) — the retry framing does NOT persist beside the terminal', async () => {
    const turn = scriptedBuild()
    renderBuilder({ deps: deps().deps })
    await sendPrompt()
    await awaitBuildTurn()

    await turn.frame(T_DIAGNOSTIC('Type error in app/page.tsx'))
    expect(await screen.findByText(/trying another way/i)).toBeTruthy()

    await turn.frame(T_BUILD_END({ status: 'failed' }))
    await turn.end()
    // Exactly one failure presentation is visible at the terminal (the outcome's), so the
    // now-false "trying another way" must be gone.
    await waitFor(() => expect(screen.queryByText(/trying another way/i)).toBeNull())
  })

  it('a quota breach ends gracefully and shows the daily-limit banner (C7 §8)', async () => {
    const turn = scriptedBuild()
    renderBuilder({ deps: deps().deps })
    await sendPrompt()
    await awaitBuildTurn()

    // The cap now arrives as a `quota` FRAME mid-turn, and the turn ends citing it — structured
    // enough that the client formats the numbers itself rather than parsing a sentence.
    await turn.frame(T_QUOTA(), T_BUILD_END({ status: 'failed', reason: 'quota_exceeded' }))
    await turn.end()
    await waitFor(() => expect(screen.getAllByText(/resets at midnight IST/i).length).toBeGreaterThan(0))
    // getAllBy — see the announcement note above.
    expect(screen.getAllByText(/no longer running/i).length).toBeGreaterThan(0) // graceful terminal
  })
})

describe('BuilderPage — the transition\'s refusals are typed HTTP statuses now (KTD-8a)', () => {
  it('a busy workspace RE-ARMS the card with the server\'s own message — no turn started', async () => {
    // `build_failed` + `reason` is gone from the response. Every case it carried is a status the
    // fetch layer already raises on (429 the daily cap, 409 a busy workspace, 503 an unconfigured
    // engine), so `buildFromPlan` THROWS and the catch arm puts the server's sentence on the card.
    // The old 200-with-a-reason made the browser re-implement error handling it already had, and
    // a genuine bug arrived looking exactly like a quota refusal.
    h.buildFromPlan.mockRejectedValue(new Error('Another build is already running for your workspace.'))
    renderBuilder({ deps: deps().deps })
    await sendPrompt()

    expect(await screen.findByText(/another build is already running/i)).toBeTruthy()
    expect(screen.getByRole('button', { name: /^Build it$/ })).toBeTruthy()
    expect(document.querySelector('iframe')).toBeNull() // nothing framed for this chat
    // No turn was subscribed to: the refusal happened before anything started.
    expect(h.readTurnStream).not.toHaveBeenCalledWith(expect.objectContaining({ turnId: BUILD_TURN_ID }))
    expect(h.getStatus).not.toHaveBeenCalled() // and no client-side 409 dance any more
  })

  it('an already_started outcome (double click / second tab) JOINS the running turn', async () => {
    h.buildFromPlan.mockResolvedValue({ outcome: 'already_started', turnId: 'other-turn', appId: 'a1' })
    const turn = scriptedBuild()
    renderBuilder({ deps: deps().deps })
    await sendPrompt()
    await awaitBuildTurn('other-turn')

    await turn.frame(T_PREVIEW())
    await waitFor(() => expect(document.querySelector('iframe')?.getAttribute('src')).toBe(PREVIEW_URL))
    expect(screen.queryByText(/already have a build running/i)).toBeNull() // joined, not blocked
  })
})

describe('BuilderPage — ONE gate: the composer is shut while the agent works (U16)', () => {
  it('a send is REFUSED while the build runs — the composer is disabled and the build is untouched', async () => {
    // The decision this pins: a build is not a parallel track you talk over. The tool calls the
    // agent makes ARE its answer, told in this thread — so while it works there is nothing to
    // send, and the composer says so instead of pretending otherwise.
    const turn = scriptedBuild()
    renderBuilder({ deps: deps().deps })
    await sendPrompt('first build')
    await awaitBuildTurn()
    await turn.frame(T_PREVIEW())
    await waitFor(() => expect(document.querySelector('iframe')).toBeTruthy())

    h.buildFromPlan.mockClear()
    h.stop.mockClear()
    h.startTurn.mockClear()

    // SENDING is what waits — not typing (KTD-1/KTD-2). The text box and attach stay live so the
    // citizen can compose their next message while they watch, and the note says why send is off.
    const textarea = screen.getByPlaceholderText(/describe what you need/i)
    expect(textarea.disabled).toBe(false)
    expect(screen.getByTitle(/Attach images/i).disabled).toBe(false)
    expect(screen.getByTestId('composer-gate-note').textContent).toMatch(/send unlocks when it’s done/i)

    // …and the gate is ENFORCED, not merely rendered: `aria-disabled` is affordance only and the
    // textarea is not disabled at all, so Enter must be refused by `handleSend` itself.
    fireEvent.change(textarea, { target: { value: 'make it dark mode' } })
    fireEvent.keyDown(textarea, { key: 'Enter' })
    expect(await screen.findByText(/send unlocks when it finishes/i)).toBeTruthy()
    expect(h.startTurn).not.toHaveBeenCalled()
    expect(h.stop).not.toHaveBeenCalled()
    expect(h.buildFromPlan).not.toHaveBeenCalled()
    expect(document.querySelector('iframe')).toBeTruthy() // the live build is untouched

    // There is also no second Build-it to click while the build runs: the card that started it
    // is resolved. So the "build over a still-live session" hazard the stop-then-start dance
    // existed for (finding #19) is now unreachable from this chat, not merely handled.
    expect(screen.queryByRole('button', { name: /^Build it$/ })).toBeNull()
  })

  it('the composer RE-OPENS at the terminal, and the send is then a CHAT turn (the routing rule)', async () => {
    // The other half of the one gate: the wait ends by itself. When the build finishes the chat
    // comes back, and a send is a question to the assistant — it never touches the build. Every
    // mode accepts a send now (the Write 400 is gone), so the thread staying in Write at the
    // terminal is no longer a dead end the page has to be rescued out of.
    const turn = scriptedBuild()
    renderBuilder({ deps: deps().deps })
    await sendPrompt('first build')
    await awaitBuildTurn()
    await turn.frame(T_PREVIEW())
    await waitFor(() => expect(document.querySelector('iframe')).toBeTruthy())
    expect(screen.getByTestId('composer-gate-note')).toBeTruthy()

    await turn.frame(T_BUILD_END())
    await turn.end()
    await waitFor(() => expect(screen.queryByTestId('composer-gate-note')).toBeNull())
    expect(screen.getByPlaceholderText(/describe what you need/i).disabled).toBe(false)
    expect(screen.getByTitle(/Attach images/i).disabled).toBe(false)

    h.buildFromPlan.mockClear()
    h.stop.mockClear()
    h.startTurn.mockClear()
    h.readTurnStream.mockImplementation(turnStreaming(planReply('Build it, but dark.', 'opt-2')))
    await send('make it dark mode')

    await waitFor(() => expect(h.startTurn).toHaveBeenCalled())
    expect(h.stop).not.toHaveBeenCalled()
    expect(h.buildFromPlan).not.toHaveBeenCalled() // no click, no build
  })

  it('the thread STAYS in Write at the terminal — the mode is where the build left it', async () => {
    // The inversion. `restore_conversation_mode` is deleted, not neutered: its entire
    // justification was that Write is a dead end a thread has to be rescued out of, and with every
    // mode accepting a send that is simply false. Flipping the thread out of Write at each build
    // terminal would be the exact opposite of what the convergence is for — the citizen who just
    // built an app is in the mode that iterates on it, and stays there until they move.
    const turn = scriptedBuild()
    renderBuilder({ deps: deps().deps })
    await sendPrompt('first build')
    expect((await screen.findByRole('button', { name: /^Mode: Write\./ }))).toBeTruthy()

    // The header still says `plan` — nothing hands the mode back any more, so a re-read at the
    // terminal would UNDO the flip the transition just made atomically.
    h.getBuild.mockResolvedValue({ id: 'build-X', kind: 'builder', mode: 'plan', messages: [] })
    await turn.frame(T_BUILD_END())
    await turn.end()

    await waitFor(() => expect(screen.queryByTestId('composer-gate-note')).toBeNull())
    expect(screen.getByRole('button', { name: /^Mode: Write\./ })).toBeTruthy()
    expect(screen.getByPlaceholderText(/describe what you need/i).disabled).toBe(false)
  })

  it('confirming the next brief starts a fresh build — nothing live to stop, so never a self-inflicted 409', async () => {
    // The refine journey, in its post-build shape. The old flow sent mid-build and had to STOP
    // the running session before starting the replacement; under one gate the previous build is
    // already terminal by the time a brief can even be asked for, so the stop is simply not
    // needed — and starting over a still-live build (the 409 that dance avoided) cannot arise.
    const first = scriptedBuild()
    renderBuilder({ deps: deps().deps })
    await sendPrompt('first build')
    await awaitBuildTurn()
    await first.frame(T_PREVIEW(), T_BUILD_END())
    await first.end()
    await waitFor(() => expect(screen.queryByTestId('composer-gate-note')).toBeNull())

    h.buildFromPlan.mockClear()
    h.stop.mockClear()
    h.readTurnStream.mockImplementation(turnStreaming(planReply('Build it, but dark.', 'opt-2')))
    await sendPrompt('make it dark mode')

    await waitFor(() => expect(h.buildFromPlan).toHaveBeenCalledWith('build-X', 'opt-2'))
    expect(h.stop).not.toHaveBeenCalled()
  })

  it('a RELOAD mid-build re-takes the gate from the transcript (review P1)', async () => {
    // The window the gate mattered most in, and was simply ABSENT from. `buildActive` derives
    // from refs only `Build it` stamps, so a fresh mount over a RUNNING build rendered an open
    // textarea, an armed mode pill, no note, and a past-tense "a build was running here" line —
    // about a build that is running right now. Every send from that page 409s. The projection
    // carries the session id on the `build_in_progress` part; that is all a rejoin needs.
    h.getBuild.mockResolvedValue({
      id: 'build-X',
      kind: 'builder',
      mode: 'write',
      messages: [
        { id: 'm0', role: 'user', seq: 0, parts: [{ type: 'text', text: 'a visitor app' }] },
        { id: 'srv_1_g', role: 'assistant', seq: 1, parts: [{ type: 'build_in_progress', sessionId: 'live-7' }] },
      ],
    })
    h.getStatus.mockResolvedValue(
      statusResp({ sessionId: 'live-7', projectId: 'p1', status: 'building' }),
    )
    const { deps: sessionDeps } = deps()
    renderBuilder({ deps: sessionDeps })

    await waitFor(() => expect(h.getStatus).toHaveBeenCalledWith('live-7'))
    const textarea = await screen.findByPlaceholderText(/describe what you need/i)
    await waitFor(() => expect(screen.getByTestId('composer-gate-note').textContent).toMatch(/send unlocks/i))
    // Typing and attaching stay live over the live build; only SEND waits.
    expect(textarea.disabled).toBe(false)
    expect(screen.getByTitle(/Attach images/i).disabled).toBe(false)
    // The mode pill IS frozen — not as a composer gate, but because the server stamps the running
    // turn's rows with the conversation's mode and 409s a mid-run switch (KTD-4).
    expect(screen.getByRole('button', { name: /^Mode: Write\./ }).disabled).toBe(true)
    // …and the transcript stops lying in the past tense: the live bubble supersedes the anchor.
    expect(document.querySelector('[data-kind="build-in-progress"]')).toBeNull()
    expect(screen.getByTestId('build-progress')).toBeTruthy()

    // Enforced, not merely rendered — the textarea is not disabled at all, so `handleSend` is the
    // only thing standing between Enter and a turn the server would refuse.
    fireEvent.change(textarea, { target: { value: 'while you are at it, add a chart' } })
    fireEvent.keyDown(textarea, { key: 'Enter' })
    expect(await screen.findByText(/send unlocks when it finishes/i)).toBeTruthy()
    expect(h.startTurn).not.toHaveBeenCalled()
    // …and the typed text SURVIVES the refusal, which is the point of keeping the box live.
    expect(textarea.value).toBe('while you are at it, add a chart')
  })

  it('the composer shuts on the CLICK, not on the server\'s answer (review P2)', async () => {
    // `buildFromPlan` is a full round-trip — the sandbox provision lives behind it, seconds long.
    // The composer used to stay open for all of it, and a send in that window hit the silent
    // double-Enter ref guard: no turn, no toast, the message simply gone.
    let answer = () => {}
    h.buildFromPlan.mockImplementation(
      () => new Promise((resolve) => { answer = () => resolve({ outcome: 'started', turnId: BUILD_TURN_ID, appId: 'a1' }) }),
    )
    const turn = scriptedBuild()
    renderBuilder({ deps: deps().deps })
    await send('a visitor app')
    fireEvent.click(await screen.findByRole('button', { name: /^Build it$/ }))

    const textarea = screen.getByPlaceholderText(/describe what you need/i)
    await waitFor(() => expect(screen.getByTestId('composer-gate-note').textContent).toMatch(/send unlocks/i))

    h.startTurn.mockClear()
    fireEvent.change(textarea, { target: { value: 'and make it dark' } })
    fireEvent.keyDown(textarea, { key: 'Enter' })
    expect(await screen.findByText(/send unlocks when it finishes/i)).toBeTruthy()
    expect(h.startTurn).not.toHaveBeenCalled() // refused OUT LOUD, never silently dropped

    // The gate then hands over to the live build turn without ever re-opening in between.
    await act(async () => { answer(); await Promise.resolve() })
    await awaitBuildTurn()
    await turn.frame(T_STEP('Scaffolding your app…'))
    expect(screen.getByTestId('composer-gate-note')).toBeTruthy()
  })

  it('a SIBLING chat in the same project keeps its composer OPEN — the gate is per-chat, like the server\'s', async () => {
    // The server's build gate is per-CONVERSATION. A gate that is not scoped to the chat that
    // started the build over-shoots it: the sibling's send goes dead and its reader is told
    // "building your app" about someone else's build — on a turn the server would accept. That is
    // why `generatingChatId` records WHICH chat is mid-turn rather than merely that one is, and
    // every other term of the gate has to be scoped the same way or it reintroduces the leak.
    const fake = new FakeEventSource('x')
    const sessionDeps = { client: makeClient(h), eventSourceFactory: () => fake }
    scriptedBuild()
    const { rerender } = render(
      <MemoryRouter initialEntries={['/x']}>
        <BuilderPage chatId="chat-A" projectId="pA" projectName="Project A" buildSessionDeps={sessionDeps} />
      </MemoryRouter>,
    )
    await screen.findByPlaceholderText(/describe what you need/i)
    await send('build A')
    fireEvent.click(await screen.findByRole('button', { name: /^Build it$/ }))
    await waitFor(() => expect(h.buildFromPlan).toHaveBeenCalledWith('chat-A', PLAN_CARD_ID))
    await awaitBuildTurn()
    await waitFor(() => expect(screen.getByTestId('composer-gate-note')).toBeTruthy())

    // The SAME instance moves to a sibling builder chat of the SAME project (flat routing —
    // only the chatId prop changes). A's build keeps running server-side either way.
    h.readTurnStream.mockImplementation(turnStreaming(planReply('A sibling plan.', 'opt-S')))
    rerender(
      <MemoryRouter initialEntries={['/x']}>
        <BuilderPage chatId="chat-B" projectId="pA" projectName="Project A" buildSessionDeps={sessionDeps} />
      </MemoryRouter>,
    )
    await waitFor(() => expect(h.getBuild).toHaveBeenCalledWith('chat-B'))
    const sibling = await screen.findByPlaceholderText(/describe what you need/i)
    await waitFor(() => expect(screen.queryByTestId('composer-gate-note')).toBeNull())
    expect(sibling.disabled).toBe(false)

    // …and the send genuinely goes out, rather than being refused on A's behalf.
    h.startTurn.mockClear()
    h.stop.mockClear()
    await send('what does this app do?')
    await waitFor(() => expect(h.startTurn).toHaveBeenCalled())
    expect(h.stop).not.toHaveBeenCalled() // A's live build is untouched
  })

  it('a Send in a DIFFERENT project does NOT tear down another project\'s live build (review F1)', async () => {
    // One BuilderPage instance persists across project switches (flat routing). A live build in
    // project A must survive a Send made from project B's chat — the bug was a tautological refine
    // guard that stopped A's build instead of refusing B.
    //
    // WHERE THE REFUSAL COMES FROM MOVED. One sandbox per user means a second build anywhere is a
    // 409 from the server, whatever project it was asked for in; `buildFromPlan` throws it and the
    // card carries the sentence. What must NOT happen either way is A's build being stopped to
    // make room for B's.
    const fake = new FakeEventSource('x')
    const sessionDeps = { client: makeClient(h), eventSourceFactory: () => fake }
    const turn = scriptedBuild()
    const { rerender } = render(
      <MemoryRouter initialEntries={['/x']}>
        <BuilderPage chatId="chat-A" projectId="pA" projectName="Project A" buildSessionDeps={sessionDeps} />
      </MemoryRouter>,
    )
    // Build + frame a preview in project A.
    const ta = await screen.findByPlaceholderText(/describe what you need/i)
    fireEvent.change(ta, { target: { value: 'build A' } })
    fireEvent.keyDown(ta, { key: 'Enter' })
    fireEvent.click(await screen.findByRole('button', { name: /^Build it$/ }))
    await waitFor(() => expect(h.buildFromPlan).toHaveBeenCalledWith('chat-A', PLAN_CARD_ID))
    await awaitBuildTurn()
    await turn.frame(T_PREVIEW())
    await waitFor(() => expect(document.querySelector('iframe')).toBeTruthy())

    // Navigate the SAME instance to project B's builder chat.
    h.buildFromPlan.mockClear()
    h.stop.mockClear()
    h.readTurnStream.mockImplementation(turnStreaming(planReply('Build B, please.', 'opt-B')))
    rerender(
      <MemoryRouter initialEntries={['/x']}>
        <BuilderPage chatId="chat-B" projectId="pB" projectName="Project B" buildSessionDeps={sessionDeps} />
      </MemoryRouter>,
    )
    await waitFor(() => expect(h.getBuild).toHaveBeenCalledWith('chat-B'))
    expect(document.querySelector('iframe')).toBeNull() // A's build is NOT shown under project B

    // Confirm a brief in project B → refused WITHOUT stopping or restarting A's build.
    h.buildFromPlan.mockRejectedValue(new Error('You already have a build running in another project.'))
    const tb = await screen.findByPlaceholderText(/describe what you need/i)
    fireEvent.change(tb, { target: { value: 'build B' } })
    fireEvent.keyDown(tb, { key: 'Enter' })
    fireEvent.click(await screen.findByRole('button', { name: /^Build it$/ }))
    // The refusal is surfaced ON the card, where the click was — and the card re-arms as a retry,
    // so B can build once A is over instead of dead-ending on a plan.
    expect(await screen.findByText(/running in another project/i)).toBeTruthy()
    expect(h.stop).not.toHaveBeenCalled() // A's live build is untouched
    const retry = screen.getByRole('button', { name: /^Build it$/ })
    expect(retry.disabled).toBe(false)
  })
})

describe('BuilderPage — the "come back later" relaunch entry point (#43)', () => {
  // A reload drops the in-memory session, but the transcript's persisted BuildOutcome part proves
  // a build once ran — so a fresh mount must render the terminal placeholder (with its Relaunch
  // action), not the idle "submit a prompt" empty state. The live/reattach flow always wins: this
  // fallback only fires when there is no session at all.
  const outcomeTranscript = (status = 'ended') => ({
    id: 'build-X',
    messages: [
      { id: 'm0', role: 'user', parts: [{ type: 'text', text: 'a visitor app' }], seq: 0 },
      {
        id: 'm1',
        role: 'assistant',
        seq: 1,
        parts: [
          { type: 'text', text: status === 'failed' ? 'The build failed.' : 'Build finished.' },
          { type: 'build', status, sessionId: 's-old', previewUrl: 'https://old.example/' },
        ],
      },
    ],
  })

  it('a fresh mount with a persisted outcome and no live session offers Relaunch; clicking calls relaunch()', async () => {
    h.getBuild.mockResolvedValue(outcomeTranscript())
    h.relaunchPreview.mockResolvedValue({
      appId: 'a1', previewUrl: PREVIEW_URL, status: 'ready', restoredFromFailedBuild: false,
    })
    const { deps: sessionDeps } = deps()
    // R5: the affordance also needs the PROJECT's confirmed saved build now — an outcome in
    // the transcript alone proves a build ran, not that a Save happened.
    renderBuilder({ deps: sessionDeps, hasSavedBuild: true })

    // Wait for the TERMINAL placeholder (the persisted outcome resolved), not just any
    // Relaunch button — with the saved build confirmed, the pre-resolution empty state offers
    // one too, and that node is replaced when the transcript lands.
    await waitFor(() => expect(screen.getAllByText(/no longer running/i).length).toBeGreaterThan(0))
    const button = await screen.findByRole('button', { name: /relaunch preview/i })
    fireEvent.click(button)
    await waitFor(() => expect(h.relaunchPreview).toHaveBeenCalledWith({ projectId: 'p1' }))
    // The restored preview frames — the whole point of the journey.
    await waitFor(() => expect(document.querySelector('iframe')?.getAttribute('src')).toBe(PREVIEW_URL))
  })

  it('labels the entry point "Relaunch last saved version" when the newest outcome FAILED (U6/F1)', async () => {
    h.getBuild.mockResolvedValue(outcomeTranscript('failed'))
    const { deps: sessionDeps } = deps()
    renderBuilder({ deps: sessionDeps, hasSavedBuild: true })
    expect(await screen.findByRole('button', { name: /relaunch last saved version/i })).toBeTruthy()
  })

  it('a fresh mount with NO outcome keeps the idle empty state — nothing to relaunch', async () => {
    h.getBuild.mockResolvedValue(null)
    const { deps: sessionDeps } = deps()
    const { container } = renderBuilder({ deps: sessionDeps })
    await screen.findByPlaceholderText(/describe what you need/i)
    expect(container.textContent).toMatch(/preview will appear here/i)
    expect(screen.queryByRole('button', { name: /relaunch/i })).toBeNull()
  })
})

// N12 (U2). Every mode-switch failure used to be reported as "Finish the current step before
// switching modes" — a race past the disabled pill was assumed to be the only way to fail. That
// assumption made the message a lie for every other cause, and the lie had teeth: with the thread
// already in Write, an expired session produced a chat that could neither send nor switch out,
// whose only explanation named a step that did not exist. These pin the narrowing per arm.
describe('a failed mode switch says what actually failed (N12)', () => {
  /** Open the pill the way a citizen does (⌥P) and pick a mode. */
  const chooseMode = async (label) => {
    await screen.findByRole('button', { name: /^Mode: / })
    fireEvent.keyDown(document, { code: 'KeyP', altKey: true })
    fireEvent.click(await screen.findByRole('menuitemradio', { name: new RegExp(label, 'i') }))
  }

  const failWith = (status, code = null) => {
    h.switchMode.mockRejectedValue(new ApiError('server copy', status, code))
  }

  beforeEach(() => {
    h.getBuild.mockResolvedValue({ id: 'build-X', kind: 'builder', mode: 'ask', messages: [] })
  })

  it('a 409 keeps the step copy — the ONE case it is true for', async () => {
    failWith(409)
    const { deps: sessionDeps } = deps()
    renderBuilder({ deps: sessionDeps })
    await chooseMode('Write')
    expect(await screen.findByText(/finish the current step before switching modes/i)).toBeTruthy()
  })

  it('a 401 names the SESSION, never the step (the bug)', async () => {
    failWith(401)
    const { deps: sessionDeps } = deps()
    renderBuilder({ deps: sessionDeps })
    await chooseMode('Write')
    expect(await screen.findByText(/session expired/i)).toBeTruthy()
    expect(screen.queryByText(/finish the current step/i)).toBeNull()
  })

  it('a 500 gets a distinct generic failure, never the step copy', async () => {
    failWith(500)
    const { deps: sessionDeps } = deps()
    renderBuilder({ deps: sessionDeps })
    await chooseMode('Write')
    expect(await screen.findByText(/could not switch modes/i)).toBeTruthy()
    expect(screen.queryByText(/finish the current step/i)).toBeNull()
  })

  it('a network drop (no status at all) still reports something honest', async () => {
    h.switchMode.mockRejectedValue(new TypeError('Failed to fetch'))
    const { deps: sessionDeps } = deps()
    renderBuilder({ deps: sessionDeps })
    await chooseMode('Write')
    expect(await screen.findByText(/could not switch modes/i)).toBeTruthy()
  })

  it('the control is never left inert — a failed switch re-arms the pill', async () => {
    // `switchingMode` gates handleModeSelect's own early return. Leaking it on a failure arm
    // would make the FIRST failure permanent: every later click returns before reaching the
    // server, and the pill silently stops working with nothing on screen to say so.
    failWith(500)
    const { deps: sessionDeps } = deps()
    renderBuilder({ deps: sessionDeps })
    await chooseMode('Write')
    await screen.findByText(/could not switch modes/i)

    h.switchMode.mockClear()
    h.switchMode.mockResolvedValue('plan')
    await chooseMode('Plan')
    await waitFor(() => expect(h.switchMode).toHaveBeenCalledWith('build-X', 'plan'))
    expect(await screen.findByRole('button', { name: /^Mode: Plan\./ })).toBeTruthy()
  })
})

// A READ turn attaches the very same container a build does (U5b), so it emits the very same
// `workspace` frame — which the build bubble used to read as a build's signature, because until
// then only Write ever had a container. Left inferred, an Ask question announced "Building your
// app…" while it ran and left an empty assistant bubble under the answer when it finished.
describe('a read turn reads the live container without becoming a build (2026-07-30)', () => {
  /** An open read-turn socket: the workspace frame lands first, the answer arrives later. */
  function scriptReadTurn() {
    const live = { emit: null, close: null }
    h.readTurnStream.mockImplementation(async ({ onFrame }) => {
      live.emit = onFrame
      onFrame(T_WORKSPACE('preparing', 1, 'Getting your workspace ready…'))
      return new Promise((resolve) => { live.close = resolve })
    })
    return {
      frame: async (...frames) => {
        await act(async () => { for (const f of frames) live.emit?.(f) })
      },
      end: async () => { await act(async () => { live.close?.('completed'); await Promise.resolve() }) },
    }
  }

  it('narrates the wait for the container, then never claims to be building', async () => {
    h.getBuild.mockResolvedValue({ id: 'build-X', kind: 'builder', mode: 'ask', messages: [] })
    const turn = scriptReadTurn()
    renderBuilder({ deps: deps().deps })
    await send('What is the heading text on the page right now? One line.')

    // The 30-60s attach is worth narrating on a read turn too — a question that sits silent for
    // a minute reads as a broken product, which is the whole reason the frame exists. Scoped to
    // the bubble because the preview pane narrates the same wait, honestly, on its own side.
    const bubble = await screen.findByTestId('build-bubble')
    expect(within(bubble).getByText(/Setting up your sandbox/i)).toBeTruthy()

    // …and that is ALL it narrates. `ready` on a read turn is not the start of a build.
    await turn.frame(T_WORKSPACE('ready', 2))
    await waitFor(() => expect(screen.queryByTestId('build-bubble')).toBeNull())
    expect(screen.queryByText(/Building your app/i)).toBeNull()
  })

  // Both terminals, because the reported symptom was the FAILED one: the `turn_ended` frame is
  // what makes the difference between "still thinking" (bubble suppressed for its own reason) and
  // a settled turn, and a settled read turn is exactly when the empty bubble appeared.
  for (const status of ['completed', 'failed']) {
    it(`leaves no empty bubble behind once a ${status} answer has landed`, async () => {
      h.getBuild.mockResolvedValue({ id: 'build-X', kind: 'builder', mode: 'ask', messages: [] })
      const turn = scriptReadTurn()
      renderBuilder({ deps: deps().deps })
      await send('What is the heading text on the page right now? One line.')
      await screen.findByTestId('build-bubble')

      await turn.frame(
        T_WORKSPACE('ready', 2),
        T_DELTA('It says "Gate Cleaning Log — T1".', 3),
        T_END(status),
      )
      await turn.end()

      expect(await screen.findByText(/Gate Cleaning Log — T1/)).toBeTruthy()
      // The answer IS the whole reply. The bubble that used to sit under it held nothing but an
      // avatar and three canned suggestions about an app nobody had described.
      expect(screen.queryByTestId('build-bubble')).toBeNull()
    })
  }
})

// The other half of the same emptiness rule, on the side it was written for: a WRITE turn whose
// container never came up is terminal with no steps and no headline, so BuildProgress renders
// nothing — and the wrapper used to render around that nothing.
describe('a build that dies before its first step shows no empty bubble (2026-07-30)', () => {
  it('renders the failure, not an empty assistant bubble', async () => {
    const turn = scriptedBuild({ opening: [T_WORKSPACE('unavailable', 1, 'The workspace service is not available right now.')] })
    renderBuilder({ deps: deps().deps })
    await sendPrompt()
    await awaitBuildTurn()
    await turn.frame(T_BUILD_END({ status: 'failed' }))
    await turn.end('failed')

    await waitFor(() => expect(screen.queryByTestId('build-bubble')).toBeNull())
  })
})
