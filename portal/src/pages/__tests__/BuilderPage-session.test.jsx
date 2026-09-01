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
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import BuilderPage from '../BuilderPage'
import {
  FakeEventSource, PREVIEW_URL, makeClient, primeClient, renderBuilder, statusResp,
  PLAN_CARD_ID, planReply, primeTurn, turnStreaming, send, T_DELTA,
  scriptBuildTurn, BUILD_TURN_ID, T_STEP, T_PREVIEW, T_QUOTA, T_BUILD_END, T_WORKSPACE, T_END,
  T_DIAGNOSTIC,
  inWorkspace,
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
  resolvePlanOptions: vi.fn(),
  start: vi.fn(),
  relaunchPreview: vi.fn(),
  stop: vi.fn(),
  getStatus: vi.fn(),
  forceEnd: vi.fn(),
}))

vi.mock('../../utils/builderHistory', () => ({
  loadBuilds: h.loadBuilds,
  newBuild: h.newBuild,
  createBuild: h.createBuild,
  getBuild: h.getBuild,
  deleteBuild: h.deleteBuild,
  deriveTitle: (t) => (t || '').slice(0, 40),
}))
// SPREAD THE ORIGINAL — `handleBuildIt` mints the new build chat's id through the shared
// `uuidv7` (ADR-0006), and a factory naming only `listProjectConversations` leaves every other
// export (including that one) undefined; Vitest now warns the moment a real caller reaches for
// it, which every Build-it press in this suite does.
vi.mock('../../utils/conversationApi', async (importOriginal) => ({
  ...(await importOriginal()),
  listProjectConversations: h.listProjectConversations,
}))
vi.mock('../../utils/chatHistory', () => ({ relativeTime: () => 'now' }))
vi.mock('../../components/layout/Navbar', () => ({ default: () => null }))
vi.mock('../../utils/attachmentStore', async (orig) => ({ ...(await orig()), buildUserParts: h.buildUserParts }))
// `switchMode` is GONE from this list (U1/U19): the route it posted to no longer exists, and a
// chat's kind can't change after creation, so there is nothing left for a mock to intercept.
vi.mock('../../utils/turnStreamApi', async (orig) => ({
  ...(await orig()),
  startTurn: (...a) => h.startTurn(...a),
  readTurnStream: (...a) => h.readTurnStream(...a),
  buildFromPlan: (...a) => h.buildFromPlan(...a),
  stopTurn: (...a) => h.stopTurn(...a),
  resolvePlanOptions: (...a) => h.resolvePlanOptions(...a),
}))

function deps() {
  const fake = new FakeEventSource('x')
  return { fake, deps: { client: makeClient(h), eventSourceFactory: () => fake } }
}

// BUILD-IT IS A HANDOFF (U5/U12), not a flip: the atomic transition creates a SECOND, brand-new
// build chat seeded with the plan and starts the turn THERE, and the click navigates the browser
// to it — the plan chat (`'build-X'`, this suite's default) is left exactly as it was. So "the
// build's conversation" this whole file used to mean `'build-X'` is now this id, and every place
// that used to assert `readTurnStream`/`buildFromPlan` against the plan chat's id for the SECOND
// (post-handoff) call has to name this one instead.
const LIVE_CHAT_ID = 'build-X-live'

/**
 * Wire up the handoff's two sides: `buildFromPlan` hands back the live chat's id, and that live
 * chat's own `getBuild` carries the `activeTurn` its adopt effect reattaches to (`reattachToTurn`
 * — the only thing that ever subscribes to a build turn now). Call again with a different pair for
 * a test that needs a specific turn id, or (inline, not through this helper) a non-default live
 * chat id — a NON-blanket `mockResolvedValue` on `getBuild` would answer the live chat's lookup
 * with the plan chat's own fixture too, which is why this is `mockImplementation`, keyed on the id.
 */
function primeHandoff(liveChatId = LIVE_CHAT_ID, turnId = BUILD_TURN_ID) {
  h.buildFromPlan.mockResolvedValue({ outcome: 'started', chatId: liveChatId, turnId })
  h.getBuild.mockImplementation(async (id) =>
    id === liveChatId
      ? { id, kind: 'build', messages: [], activeTurn: { turnId, lastSeq: 0 } }
      : null,
  )
}

/**
 * Get a build running the way a user does now (U5/U12 + U11): send a turn, wait for the plan
 * card, click Build it. The atomic transition creates the live build chat and starts a WRITE TURN
 * on it, and the click navigates the browser there — so by the time this resolves, the routed
 * chat id has changed and the page is adopting the NEW chat, not the one Build-it was pressed in.
 */
async function sendPrompt(text = 'build me a tool') {
  await send(text)
  fireEvent.click(await screen.findByRole('button', { name: /^Build it$/ }))
  await waitFor(() => expect(h.buildFromPlan).toHaveBeenCalled())
}

/** …and the page has actually ARRIVED and is genuinely ON the build turn's socket (what "the
 *  build is running" means) — the live chat's own adopt reattaching to it, not a subscription the
 *  press itself opened. */
async function awaitBuildTurn(turnId = BUILD_TURN_ID, liveChatId = LIVE_CHAT_ID) {
  await waitFor(() => expect(h.getBuild).toHaveBeenCalledWith(liveChatId))
  await waitFor(() => expect(h.readTurnStream).toHaveBeenCalledWith(expect.objectContaining({ turnId })))
}

/** Script the build turn for this test and hand back the open socket to push frames into. */
function scriptedBuild(options) {
  const turn = scriptBuildTurn(options)
  h.readTurnStream.mockImplementation(turn.impl)
  return turn
}

/** The consolidating snapshot every subscribe gets FIRST on cursor 0 (`backend/.../turns.py`'s
 *  own docstring: "emit the first frame BEFORE any model byte — the snapshot serves that role"),
 *  carrying the `turnId` `handleStopTurn` reads out of `liveTurnIdRef` — `scriptBuildTurn`'s
 *  default `opening` (just a `workspace` frame) predates that contract, so a test that presses
 *  Stop has to supply one itself. Mirrors `BuilderPage-outcome.test.jsx`'s helper of the same name
 *  and purpose. */
const T_SNAPSHOT = (turnId, seq = 1) => ({
  type: 'snapshot', seq, turnId, turnStatus: 'running', items: [], textSoFar: '', steps: [],
})

beforeEach(() => {
  vi.clearAllMocks()
  Element.prototype.scrollIntoView = vi.fn()
  primeClient(h)
  h.newBuild.mockReturnValue('build-Y')
  h.createBuild.mockResolvedValue({ ok: true })
  h.deleteBuild.mockResolvedValue(true)
  h.getBuild.mockResolvedValue(null)
  h.loadBuilds.mockResolvedValue([])
  h.listProjectConversations.mockResolvedValue([{ id: 'build-X', kind: 'build', title: 'My build', updatedAt: new Date().toISOString() }])
  h.buildUserParts.mockImplementation(async (text) => [{ type: 'text', text }])
  // The scripted turn: every send streams a plan + the options card, so these suites reach
  // the session mechanics in one send + one click.
  primeTurn(h)
  // The transition's OWN answer (U5/U12): `chatId` names the live build chat the handoff creates,
  // and that chat's `getBuild` carries the `activeTurn` its adopt effect reattaches to. Overridden
  // per-test wherever the plan/live chat pair isn't the default (a different originating chat, or
  // a turn id the test wants to assert on specifically, e.g. `already_started`'s `other-turn`).
  primeHandoff()
})
afterEach(() => cleanup())

describe('BuilderPage — the build-turn flow (ORIG-§3-d/f)', () => {
  it('Build it starts a WRITE TURN; its step frames render; the preview frame frames the sandbox URL', async () => {
    const turn = scriptedBuild()
    renderBuilder({ deps: deps().deps })
    await sendPrompt()

    // The build starts through the ATOMIC TRANSITION — a SECOND, brand-new build chat, created,
    // seeded with the plan, and started, server-side (U5/U12) — and the page NAVIGATES there and
    // subscribes to the TURN the handoff returns. Neither C3 door is opened: `start` was already
    // not a client concern, and now `getStatus` isn't either — there is no session to join at all.
    // Third arg is the client-minted id of that new chat (a real `uuidv7()`, so only its shape is
    // pinned, not its value).
    expect(h.buildFromPlan).toHaveBeenCalledWith('build-X', PLAN_CARD_ID, expect.any(String))
    expect(h.start).not.toHaveBeenCalled()
    expect(h.getStatus).not.toHaveBeenCalled()
    // Cursor 0 deliberately: the build may have been running for seconds before this subscribe
    // landed, and the consolidating snapshot is what recovers the frames it missed. The
    // conversation is the LIVE build chat, not the plan chat Build-it was pressed in.
    await waitFor(() =>
      expect(h.readTurnStream).toHaveBeenCalledWith(
        expect.objectContaining({ conversationId: LIVE_CHAT_ID, turnId: BUILD_TURN_ID, cursor: 0 }),
      ),
    )

    await turn.frame(T_STEP('Scaffolding your app…'), T_STEP('Installing dependencies', { id: 'call-2', seq: 3 }))
    // The rows are actually in the DOM (not just props) — no remount needed. Only the MOST
    // RECENT step is visible live — it replaces the previous one in the same spot rather
    // than both accumulating.
    // SCOPED TO THE VISIBLE ROW (U17): the sr-only live region carries a second, deliberately
    // PACED copy of the label, so both queries have to name which of the two they mean. The
    // property under test — one row, replaced in place — is about the one a person looks at.
    const liveRow = within(await screen.findByTestId('build-activity'))
    expect(await liveRow.findByText(/Installing dependencies/i)).toBeTruthy()
    expect(liveRow.queryByText(/Scaffolding your app/i)).toBeNull()

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
    // A snapshot frame is what seeds `liveTurnIdRef` — the fact Stop reads to know which turn to
    // address — and cursor 0 gets one on every real subscribe (the wire contract `readTurnStream`
    // documents); `scriptBuildTurn`'s default opening predates that, so it's supplied here.
    const turn = scriptedBuild({ opening: [T_SNAPSHOT(BUILD_TURN_ID), T_WORKSPACE()] })
    renderBuilder({ deps: deps().deps })
    await sendPrompt()
    await awaitBuildTurn()
    await turn.frame(T_PREVIEW())
    await waitFor(() => expect(document.querySelector('iframe')).toBeTruthy())

    // ONE working indicator, ONE way to interrupt it: a build has no separate stop any more, so
    // this is the same `stopTurn` an ordinary reply uses, addressed by the live chat + turn id —
    // the chat the page is actually ON after the handoff, not the one Build-it was pressed in.
    // Addressed by test id: `stop-turn` is the RELOCATED control on the composer (R55, Plan D
    // U3). The build card's own Stop is still mounted beside it for now and does the same thing;
    // this is the one that survives the card's deletion.
    fireEvent.click(screen.getByTestId('stop-turn'))
    await waitFor(() => expect(h.stopTurn).toHaveBeenCalledWith(LIVE_CHAT_ID, BUILD_TURN_ID))
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
    // U16 — FLIPPED. This used to require the compiler's own title to render exactly once. The
    // title is built FOR THE MODEL (it is the first meaningful line of a `tsc` diagnostic), so
    // rendering it at all was the developer surface U16 removes. The retry row now carries the
    // platform's sentence plus a next action; the title still rides on the frame, unrendered.
    // This assertion is the END-TO-END half — server frame → parse → narrative → render — that
    // BuildProgress's own unit tests cannot reach.
    expect(document.querySelector('[data-kind="retry"] pre')).toBeNull()
    expect(screen.queryAllByText(/Type error in app\/page\.tsx/i)).toHaveLength(0)
    // Liveness, so the absence above is an absence and not a row that failed to render: this
    // frame carries no citizen-facing pair (the fixture predates it), so the feed's committed
    // fallback is what a citizen reads — both halves of it.
    expect(screen.getByText(/We hit a problem finishing that change\./i)).toBeTruthy()
    expect(
      screen.getByText(/Try describing what you want again, or ask for something simpler\./i),
    ).toBeTruthy()

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
    // `already_started` still names the live chat — it is the SAME chat a first press would have
    // created, just already there (a double press mints the same client id, so the server answers
    // with the chat that already exists rather than a second one).
    h.buildFromPlan.mockResolvedValue({ outcome: 'already_started', chatId: LIVE_CHAT_ID, turnId: 'other-turn' })
    h.getBuild.mockImplementation(async (id) =>
      id === LIVE_CHAT_ID
        ? { id, kind: 'build', messages: [], activeTurn: { turnId: 'other-turn', lastSeq: 0 } }
        : null,
    )
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
    //
    // THE TOAST'S WORDING IS THE GENERIC ONE, not "your app is being built". `buildActiveHere`
    // (the branch that would say that) only ever fires for a LEGACY session reattach now — this
    // page's own send path never starts one (`session.start()` is not called anywhere in it) — so
    // an ARRIVED, ALREADY-STREAMING build turn reads as an ordinary in-flight reply once you are
    // on the chat it runs in. "Building your app…" still appears, but only for the click-time
    // round-trip (`buildStarting`), pinned separately in `BuilderPage-composer.test.jsx`.
    fireEvent.change(textarea, { target: { value: 'make it dark mode' } })
    fireEvent.keyDown(textarea, { key: 'Enter' })
    expect(await screen.findByText(/send unlocks when the current reply finishes/i)).toBeTruthy()
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

  it('AN INERTNESS GUARD (L8): no mode control appears at the terminal, and a stray legacy `mode` field is ignored', async () => {
    // This used to prove Write STAYS after a build rather than flipping back — the inversion that
    // `restore_conversation_mode` was DELETED, not merely disarmed, because every mode accepting a
    // send made "Write is a dead end to rescue a thread out of" simply false. That whole axis is
    // gone now (U1/U19): `ModeSwitcher` is deleted, a chat's kind is fixed at creation, and the
    // HEADER RE-READ this test used to defend against reintroducing (`getBuild(activeId).then(saved
    // => setChatMode(saved.mode))`) is gone from `BuilderPage.tsx` too — there is no server-side
    // answer left to go fetch, and nothing left to undo. What survives, restated as an absence: no
    // build terminal ever renders a mode control, and a `getBuild` row still carrying a legacy
    // `mode` field (an old, pre-migration row) is simply ignored rather than read.
    const turn = scriptedBuild()
    renderBuilder({ deps: deps().deps })
    await sendPrompt('first build')
    await awaitBuildTurn()
    // LIVENESS FIRST: the page is genuinely on the live build, not merely missing the retired
    // control because it rendered nothing.
    expect(screen.getByTestId('build-progress')).toBeTruthy()
    expect(screen.queryByRole('button', { name: /^Mode:/ })).toBeNull()

    // A row with a stray legacy `mode` — if a re-read were reintroduced, this is what it would see.
    h.getBuild.mockResolvedValue({ id: LIVE_CHAT_ID, kind: 'build', mode: 'plan', messages: [] })
    await turn.frame(T_BUILD_END())
    await turn.end()

    await waitFor(() => expect(screen.queryByTestId('composer-gate-note')).toBeNull())
    expect(screen.getByPlaceholderText(/describe what you need/i).disabled).toBe(false)
    expect(screen.queryByRole('button', { name: /^Mode:/ })).toBeNull()
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

    // The page is ALREADY on the live build chat from the first handoff (Build-it navigated there,
    // and the routed chat id does not change again by itself) — so this second confirm names THAT
    // chat, not the original plan chat, as the conversation the offer belongs to.
    await waitFor(() =>
      expect(h.buildFromPlan).toHaveBeenCalledWith(LIVE_CHAT_ID, 'opt-2', expect.any(String)),
    )
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
      kind: 'build',
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
    // AN INERTNESS GUARD, not the frozen-pill assertion it replaces (L8). This used to prove the
    // mode pill froze during a live build rather than let a mid-run switch retroactively mislabel
    // it (KTD-4) — `ModeSwitcher` and the axis it drove are BOTH gone (U1/U19), so there is no
    // pill left to freeze, mid-build reload or otherwise.
    expect(screen.queryByRole('button', { name: /^Mode:/ })).toBeNull()
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
      () => new Promise((resolve) => { answer = () => resolve({ outcome: 'started', chatId: LIVE_CHAT_ID, turnId: BUILD_TURN_ID }) }),
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
    // BUILD-IT IS A HANDOFF (U5/U12): pressing it in `chat-A` creates a SECOND, brand-new build
    // chat and navigates there — this render has no `<Routes>` for that real `navigate()` to
    // resolve against (a `chatId` PROP, matching `BuilderPage-composer.test.jsx`'s `renderAt`
    // idiom), so arriving is simulated the same way every sibling-chat guard in this file already
    // simulates a chat switch: a `chatId` prop swap on the SAME instance.
    const CHAT_A_LIVE = 'chat-A-live'
    const fake = new FakeEventSource('x')
    const sessionDeps = { client: makeClient(h), eventSourceFactory: () => fake }
    h.buildFromPlan.mockResolvedValue({ outcome: 'started', chatId: CHAT_A_LIVE, turnId: BUILD_TURN_ID })
    h.getBuild.mockImplementation(async (id) =>
      id === CHAT_A_LIVE
        ? { id, kind: 'build', messages: [], activeTurn: { turnId: BUILD_TURN_ID, lastSeq: 0 } }
        : null,
    )
    scriptedBuild()
    const { rerender } = render(
      <MemoryRouter initialEntries={['/x']}>
        <Routes>{inWorkspace(<Route path="*" element=<BuilderPage chatId="chat-A" projectId="pA" projectName="Project A" buildSessionDeps={sessionDeps} /> />)}</Routes>
      </MemoryRouter>,
    )
    await screen.findByPlaceholderText(/describe what you need/i)
    await send('build A')
    fireEvent.click(await screen.findByRole('button', { name: /^Build it$/ }))
    await waitFor(() =>
      expect(h.buildFromPlan).toHaveBeenCalledWith('chat-A', PLAN_CARD_ID, expect.any(String)),
    )

    rerender(
      <MemoryRouter initialEntries={['/x']}>
        <BuilderPage chatId={CHAT_A_LIVE} projectId="pA" projectName="Project A" buildSessionDeps={sessionDeps} />
      </MemoryRouter>,
    )
    await awaitBuildTurn(BUILD_TURN_ID, CHAT_A_LIVE)
    await waitFor(() => expect(screen.getByTestId('composer-gate-note')).toBeTruthy())

    // The SAME instance moves to a sibling builder chat of the SAME project (flat routing —
    // only the chatId prop changes). A's build keeps running server-side either way.
    h.readTurnStream.mockImplementation(turnStreaming(planReply('A sibling plan.', 'opt-S')))
    rerender(
      <MemoryRouter initialEntries={['/x']}>
        <Routes>{inWorkspace(<Route path="*" element=<BuilderPage chatId="chat-B" projectId="pA" projectName="Project A" buildSessionDeps={sessionDeps} /> />)}</Routes>
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
    // BUILD-IT IS A HANDOFF (U5/U12): pressing it in `chat-A` navigates to a SECOND, brand-new
    // build chat, which this route-less (`chatId`-prop) render simulates the same way every other
    // sibling-chat guard in this file does — a chatId prop swap on the SAME instance.
    const CHAT_A_LIVE = 'chat-A-live'
    const fake = new FakeEventSource('x')
    const sessionDeps = { client: makeClient(h), eventSourceFactory: () => fake }
    h.buildFromPlan.mockResolvedValue({ outcome: 'started', chatId: CHAT_A_LIVE, turnId: BUILD_TURN_ID })
    h.getBuild.mockImplementation(async (id) =>
      id === CHAT_A_LIVE
        ? { id, kind: 'build', messages: [], activeTurn: { turnId: BUILD_TURN_ID, lastSeq: 0 } }
        : null,
    )
    const turn = scriptedBuild()
    const { rerender } = render(
      <MemoryRouter initialEntries={['/x']}>
        <Routes>{inWorkspace(<Route path="*" element=<BuilderPage chatId="chat-A" projectId="pA" projectName="Project A" buildSessionDeps={sessionDeps} /> />)}</Routes>
      </MemoryRouter>,
    )
    // Build + frame a preview in project A.
    const ta = await screen.findByPlaceholderText(/describe what you need/i)
    fireEvent.change(ta, { target: { value: 'build A' } })
    fireEvent.keyDown(ta, { key: 'Enter' })
    fireEvent.click(await screen.findByRole('button', { name: /^Build it$/ }))
    await waitFor(() =>
      expect(h.buildFromPlan).toHaveBeenCalledWith('chat-A', PLAN_CARD_ID, expect.any(String)),
    )
    // `inWorkspace`, like every other mount in this test: the app pane is a SIBLING of the
    // router outlet now, so a bare `<BuilderPage>` has no host to frame the preview into and
    // the iframe assertion below would fail for a reason that has nothing to do with the
    // teardown this test is about.
    rerender(
      <MemoryRouter initialEntries={['/x']}>
        <Routes>{inWorkspace(<Route path="*" element=<BuilderPage chatId={CHAT_A_LIVE} projectId="pA" projectName="Project A" buildSessionDeps={sessionDeps} /> />)}</Routes>
      </MemoryRouter>,
    )
    await awaitBuildTurn(BUILD_TURN_ID, CHAT_A_LIVE)
    await turn.frame(T_PREVIEW())
    await waitFor(() => expect(document.querySelector('iframe')).toBeTruthy())

    // Navigate the SAME instance to project B's builder chat.
    h.buildFromPlan.mockClear()
    h.stop.mockClear()
    h.readTurnStream.mockImplementation(turnStreaming(planReply('Build B, please.', 'opt-B')))
    rerender(
      <MemoryRouter initialEntries={['/x']}>
        <Routes>{inWorkspace(<Route path="*" element=<BuilderPage chatId="chat-B" projectId="pB" projectName="Project B" buildSessionDeps={sessionDeps} /> />)}</Routes>
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

// N12 (U2) used to live here as five tests narrowing a mode-switch failure per HTTP status (a
// 409 keeps "finish the current step", a 401 names the session, a 500/network drop get a generic
// failure) plus a sixth pinning that a failed switch re-arms the pill rather than wedging it dead.
//
// AN INERTNESS GUARD NOW (L8), not five deletions. There is no mode to fail switching INTO:
// `ModeSwitcher`, `switchMode` (client and the `/api/conversations/{id}/mode` route it posted to),
// `chatMode`/`switchingMode`/`handleModeSelect` are ALL gone (U1/U19) — a chat's kind is fixed when
// it is created, so the entire failure taxonomy above describes a request that can no longer be
// made. What replaces it is the one claim that subsumes all six: the control is not on the
// surface, ⌥P opens nothing, and this is true at every point in a build's life the old suite
// checked it — idle, and mid-build.
describe('a failed mode switch says what actually failed (N12) — RETIRED, now an inertness guard', () => {
  it('no mode pill exists idle, and ⌥P opens no menu — the whole surface this suite exercised is gone', async () => {
    h.getBuild.mockResolvedValue({ id: 'build-X', kind: 'build', messages: [] })
    const { deps: sessionDeps } = deps()
    renderBuilder({ deps: sessionDeps })

    // LIVENESS FIRST: an absent pill also describes a component that threw and rendered nothing.
    await screen.findByPlaceholderText(/describe what you need/i)
    expect(screen.queryByRole('button', { name: /^Mode: /i })).toBeNull()
    fireEvent.keyDown(document, { code: 'KeyP', altKey: true })
    expect(screen.queryByRole('menuitemradio')).toBeNull()
  })

  it('no mode pill exists mid-build either — the frozen-pill mechanic this suite also drove is gone', async () => {
    const turn = scriptedBuild()
    renderBuilder({ deps: deps().deps })
    await sendPrompt('first build')
    await awaitBuildTurn()

    // LIVENESS: genuinely on the live build, not merely missing a control on a blank page.
    expect(screen.getByTestId('build-progress')).toBeTruthy()
    expect(screen.queryByRole('button', { name: /^Mode: /i })).toBeNull()
    fireEvent.keyDown(document, { code: 'KeyP', altKey: true })
    expect(screen.queryByRole('menuitemradio')).toBeNull()
    void turn // scripted but not driven further — the build's own progress isn't this test's claim
  })
})

// A READ turn attaches the very same container a build does (U5b), so it emits the very same
// `workspace` frame — which the build bubble used to read as a build's signature, because until
// then only Write ever had a container. Left inferred, an Ask question announced "Building your
// app…" while it ran and left an empty assistant bubble under the answer when it finished.
//
// THE FIRST TEST BELOW IS REWRITTEN, NOT DELETED, AND THE REASON IS ARCHITECTURAL (U1). `isBuild`
// used to be `chatMode === 'write'` — a real per-turn fact this page could ask, because a
// conversation's mode could be ask/plan/write. `BuilderPage.tsx` hardcodes it `true` now: "This
// page renders a build chat and a build chat has no other kind of send" (the comment on the call
// site). There is no more a per-send setting that could make a turn on THIS PAGE a read turn, so
// "the page can narrate a read turn without claiming to build" asserts a state the page can no
// longer be in — asserting it through this page would mean asserting `isBuild: false` behaviour
// nothing here can produce any more.
//
// What is still true, and what the rewritten test asserts instead: a build turn narrates the
// container wait FIRST and only THEN claims to be building — the ORDERING survives even though
// the "might not be a build" branch of the choice does not. The `isBuild: false` arm itself is
// unchanged and still correct in `turnNarrative.ts` (`narrativeStatus`'s own `if (!isBuild)`
// branch) — it has no direct unit test anywhere in the repo (`src/utils/__tests__/` carries no
// `turnNarrative.test.ts`), and adding one is out of this file's scope.
//
// The remaining two tests are UNCHANGED in substance: they were always about the TERMINAL leaving
// no empty bubble behind, which `headline()` still resolves to `null` for regardless of `isBuild`.
describe('a read turn reads the live container without becoming a build (2026-07-30) — the live half is rewritten for U1', () => {
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

  it('narrates the container wait FIRST, and only THEN claims to be building (rewritten for U1)', async () => {
    // Was: "…then never claims to be building" — a claim about a page state (`isBuild: false`)
    // this page cannot produce any more. What survives is the ORDER: the wait is narrated
    // honestly before anything else (never silent for 30-60s), and the SAME live bubble only
    // moves to "Building your app…" once the container reports ready, never before.
    h.getBuild.mockResolvedValue({ id: 'build-X', kind: 'build', messages: [] })
    const turn = scriptReadTurn()
    renderBuilder({ deps: deps().deps })
    await send('What is the heading text on the page right now? One line.')

    const bubble = await screen.findByTestId('build-bubble')
    expect(within(bubble).getByText(/Setting up your sandbox/i)).toBeTruthy()
    expect(within(bubble).queryByText(/Building your app/i)).toBeNull() // not yet — still preparing

    // LIVENESS: the headline text itself moves — proof the page reacted to the frame rather than
    // having frozen on the first one.
    await turn.frame(T_WORKSPACE('ready', 2))
    await waitFor(() => expect(screen.getByTestId('build-progress').textContent).toMatch(/Building your app/i))
    expect(screen.queryByText(/Setting up your sandbox/i)).toBeNull()
  })

  // Both terminals, because the reported symptom was the FAILED one: the `turn_ended` frame is
  // what makes the difference between "still thinking" (bubble suppressed for its own reason) and
  // a settled turn, and a settled read turn is exactly when the empty bubble appeared. UNCHANGED
  // by U1: `headline()` returns `null` for 'ended'/'failed' regardless of `isBuild`, and no step
  // frame ever arrived, so the wrapper still has nothing left to say once the answer lands.
  for (const status of ['completed', 'failed']) {
    it(`leaves no empty bubble behind once a ${status} answer has landed`, async () => {
      h.getBuild.mockResolvedValue({ id: 'build-X', kind: 'build', messages: [] })
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

// U2 — the platform's own sentences about the workspace, and the slot they share.
describe('what the platform says about the workspace itself (U2)', () => {
  function scriptReadTurn() {
    const live = { emit: null, close: null }
    h.readTurnStream.mockImplementation(async ({ onFrame }) => {
      live.emit = onFrame
      onFrame(T_WORKSPACE('preparing', 1, 'Getting your workspace ready…'))
      return new Promise((resolve) => { live.close = resolve })
    })
    return {
      frame: async (...frames) => { await act(async () => { for (const f of frames) live.emit?.(f) }) },
      end: async () => { await act(async () => { live.close?.('completed'); await Promise.resolve() }) },
    }
  }

  // ★ THE BANNER IS THE ONLY PLACE THIS SENTENCE CAN LIVE. Putting an app back takes tens of
  // seconds, and a bubble that scrolls away takes its own next action with it — this one ends in
  // "send your message again", which the citizen has to still be able to see when it comes back.
  it('shows a recovery sentence above the composer, not in the transcript', async () => {
    h.getBuild.mockResolvedValue({ id: 'build-X', kind: 'build', messages: [] })
    const turn = scriptReadTurn()
    renderBuilder({ deps: deps().deps })
    await send('Add a column for the gate number.')
    // Wait for the turn to be genuinely under way: `resetTurnNarrative` clears the banner at the
    // start of every turn, so a notice framed before that lands would be wiped by the setup
    // rather than by anything this test is about.
    await screen.findByTestId('build-bubble')

    await turn.frame(T_WORKSPACE('preparing', 2, null, 'Your workspace had been reset, so we are putting your app back.'))

    const banner = await screen.findByTestId('turn-banner')
    expect(banner.textContent).toMatch(/putting your app back/i)
  })

  // The ordinary phase machine ticks `preparing` -> `ready` on EVERY turn and carries no message.
  // Reading those as platform speech would post an empty banner on every message — and worse,
  // `ready` would wipe a sentence that is still true the moment the container came up.
  //
  // THE LIVENESS SIGNAL IS REWRITTEN FOR U1, the claim about the banner is not. This used to prove
  // liveness by the build BUBBLE disappearing on `ready` — true only while `isBuild` could be
  // `false` (a plain read turn's bubble had nothing left to narrate once its container was up).
  // `isBuild` is unconditional now (see the describe block above), so the bubble does not
  // disappear — it moves on to "Building your app…", which is itself the liveness proof: the
  // headline text changing is what shows the page reacted to the `ready` frame, not a component
  // that froze on the first one.
  it('is not posted or cleared by the ordinary lifecycle frames', async () => {
    h.getBuild.mockResolvedValue({ id: 'build-X', kind: 'build', messages: [] })
    const turn = scriptReadTurn()
    renderBuilder({ deps: deps().deps })
    await send('Add a column for the gate number.')
    await screen.findByTestId('build-bubble')

    expect(screen.queryByTestId('turn-banner')).toBeNull()

    await turn.frame(T_WORKSPACE('preparing', 2, null, 'We could not check whether your workspace is intact.'))
    await screen.findByTestId('turn-banner')

    await turn.frame(T_WORKSPACE('ready', 3))

    // LIVENESS: the headline moves from the preparing wait to the building claim.
    await waitFor(() => expect(screen.getByTestId('build-progress').textContent).toMatch(/Building your app/i))
    // …and the notice survives the ordinary `ready` tick untouched — neither posted anew nor wiped.
    expect(screen.getByTestId('turn-banner').textContent).toMatch(/could not check/i)
  })
})
