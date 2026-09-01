/**
 * Shared, MOCK-FREE harness for the BuilderPage build-session suites (U5→U13). It only exports
 * plain fixtures + a render helper; each test file declares its OWN vi.hoisted mocks + vi.mock
 * (those are hoisted per-file), then feeds the C3 mock client + a FakeEventSource into BuilderPage
 * via the `buildSessionDeps` prop — the "inject the mock via the deps bag" idiom (KTD-6). The REAL
 * useBuildSession hook + LivePreview + ActivityFeed + SessionControls run, so the tests assert the
 * rendered DOM, not a stubbed marker.
 *
 * U13 CHANGED THE TRANSPORT AND THE TRIGGER. A composer send is a TURN (POST /turns + the frame
 * stream); the plan streams as text and `present_plan_options` renders the card; a build starts
 * only through the atomic Build-it transition. So a suite that wants a build must (a) mock
 * `../../utils/turnStreamApi` onto its `h` bag (startTurn / readTurnStream / buildFromPlan /
 * resolvePlanOptions / stopTurn), (b) prime it with `primeTurn(h)`, and (c) drive
 * `sendAndConfirm()`. `turnStreaming` scripts the frame feed; `planReply()` is the standard
 * text-plus-card turn.
 *
 * U5 CHANGED WHAT BUILD-IT STARTS. It is no longer a C3 build SESSION — it is a WRITE TURN on the
 * same conversation, so `buildFromPlan` hands back a `turnId` (never a `sessionId`) and the page
 * subscribes to it with the very same `readTurnStream` an ordinary send uses. A build therefore
 * narrates itself through `workspace` / `step` / `preview` / `diagnostic` / `quota` / `turn_ended`
 * TURN FRAMES, not C7 envelopes: `scriptBuildTurn()` below is how a suite drives one, and the
 * FakeEventSource is now only for the LEGACY session path (the reload-mid-build reattach).
 *
 * Not a `*.test.*` file → the runner never collects it.
 */
import { act, fireEvent, screen, render, waitFor } from '@testing-library/react'
import { expect } from 'vitest'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import ConversationSurface from '../../components/chat/ConversationSurface'
// THE REAL SHELL, not a stub, and both helpers below mount the page THROUGH it as a layout route.
// After the extraction the surface is an outlet child rather than a root: it renders no page frame
// and no navbar, and — from U4 — no pane of its own. A harness that kept mounting it bare would
// leave every preview assertion in fifteen suites asserting against something the product does not
// render, which is the failure mode a stub cannot show you.
import WorkspaceShell from '../../components/workspace/WorkspaceShell'

/**
 * Nest routes under the REAL workspace shell, for the suites that build their own route tables.
 *
 * The surface is an outlet child now: it renders no page frame, no navbar and — from U4 — no pane
 * of its own, publishing what to frame upward instead. A table that mounts it bare therefore has
 * no pane at all, so every assertion about the preview silently asserts against something the
 * product does not render. This is one line at each such table rather than a stub, because a stub
 * is exactly what would hide the mistake.
 *
 * Usage: `<Routes>{inWorkspace(<Route path="/chat/:chatId" element={…} />)}<Route … /></Routes>`
 */
export const inWorkspace = (...routes) => (
  <Route key="workspace" element={<WorkspaceShell />}>
    {routes}
  </Route>
)

export { FakeEventSource } from '../../utils/buildSessionMock'

export const PREVIEW_URL = 'https://app-xyz.example.azurecontainerapps.io/'

// C3 response builders (camelCase). `over` lets a test tweak one field.
export const startResp = (over = {}) => ({ sessionId: 's1', projectId: 'p1', appId: 'a1', status: 'provisioning', previewUrl: null, createdAt: 'c', ...over })
export const statusResp = (over = {}) => ({ sessionId: 's1', projectId: 'p1', appId: 'a1', status: 'provisioning', previewUrl: null, lastSeq: null, createdAt: 'c', updatedAt: 'u', ...over })
export const ENDED_RESP = { sessionId: 's1', status: 'ended' }

/** Assemble a BuildSessionClient from a per-file `h` bag of vi.fn()s.
 *
 *  `acquireLock` / `releaseLock` are GONE (U28): nothing called them — the portal's keep-alive
 *  loop that was their only caller was itself deleted back in U13. `forceEnd` is the one lock
 *  op still reachable from the UI. */
export function makeClient(h) {
  return {
    start: h.start,
    relaunchPreview: h.relaunchPreview,
    stop: h.stop,
    getStatus: h.getStatus,
    forceEnd: h.forceEnd,
  }
}

/** Give the per-file `h` bag its default happy resolutions (call inside beforeEach). */
export function primeClient(h) {
  h.start.mockResolvedValue(startResp())
  h.stop.mockResolvedValue(ENDED_RESP)
  h.getStatus.mockResolvedValue(statusResp())
  h.forceEnd.mockResolvedValue(ENDED_RESP)
}

// ─── U13: the turn half (streamed plan + the options card) ───────────────────

/** The plan text the scripted turn streams — the card's Build-it executes it server-side. */
export const BRIEF = 'Build an application for BIAL that tracks visitor passes.'

export const PLAN_CARD_ID = 'opt-1'

// Turn-stream frame builders (camelCase — the U10 wire).
export const T_DELTA = (text, seq = 1) => ({ type: 'text_delta', seq, text })
export const T_CARD = (toolCallId = PLAN_CARD_ID, seq = 2) => ({
  type: 'plan_options',
  seq,
  item: { type: 'plan_options', seq: 0, mode: 'plan', toolCallId, state: 'pending', reason: null },
})
export const T_END = (status = 'completed', seq = 9) => ({ type: 'turn_ended', seq, turnId: 't1', status })

/** The standard planning turn: streams the plan text, presents the card, completes. */
export const planReply = (text = BRIEF, toolCallId = PLAN_CARD_ID) => [
  T_DELTA(text),
  T_CARD(toolCallId),
  T_END(),
]

/** A text-only turn (an answer / clarifying question — no card). */
export const textReply = (text) => [T_DELTA(text), T_END()]

/** A `readTurnStream` implementation that plays `frames` then resolves `outcome`. */
export const turnStreaming = (frames, outcome = 'completed') =>
  async ({ onFrame }) => {
    for (const frame of frames) onFrame(frame)
    return outcome
  }

// ─── U5: the BUILD half — a Write turn, narrated by TURN FRAMES ───────────────

/** The turn a Build-it starts. `sessionId` is gone from the transition's answer entirely. */
export const BUILD_TURN_ID = 'bt-1'
/** The chat a handoff CREATES — a different conversation from the one Build it was pressed
 *  in, which is the whole shape of the press now. */
export const BUILD_CHAT_ID = 'bc-1'

/** The sandbox lifecycle. `narrativeStatus` returns null until one of these lands, so a build
 *  test that omits it renders no bubble at all — the workspace frame IS the build's beginning. */
export const T_WORKSPACE = (state = 'ready', seq = 1, message = null, notice = null) => ({ type: 'workspace', seq, state, message, notice })

/** One tool call. `pending` is the in-flight state on the wire; the same `toolCallId` arriving a
 *  second time REPLACES the first, which is how a spinner becomes its own result. */
export const T_STEP = (label = 'Scaffolding your app…', { id = 'call-1', state = 'pending', tool = 'write_file', seq = 2, hidden = false } = {}) => ({
  type: 'step',
  seq,
  toolCallId: id,
  phase: state === 'pending' ? 'started' : 'finished',
  item: { type: 'step', seq, mode: 'write', tool, label, state, hidden, detail: {} },
})

export const T_PREVIEW = (url = PREVIEW_URL, state = 'ready', seq = 3) => ({ type: 'preview', seq, state, previewUrl: url })

/** NOT a failure — a repair run follows. It renders as an in-narrative alert row. */
export const T_DIAGNOSTIC = (title = 'Type error in app/page.tsx', seq = 4) => ({
  type: 'diagnostic', seq, source: 'tsc', title, cleanedStack: 'app/page.tsx:12:5',
})

export const T_QUOTA = (seq = 5) => ({ type: 'quota', seq, limit: 1_000_000, used: 1_000_000, resetsAt: '2026-07-15T18:30:00Z' })

/** The build's terminal. `snapshotCommitted` is TRI-STATE — omit it to mean UNKNOWN. */
export const T_BUILD_END = (over = {}) => ({
  type: 'turn_ended', seq: 9, turnId: BUILD_TURN_ID, status: 'completed', reason: null, ...over,
})

/**
 * A `readTurnStream` implementation that can hold a socket OPEN, so a test can push frames into a
 * running turn by hand and assert on it mid-flight. Close it with `end()`.
 *
 * TWO SUBSCRIBE SHAPES, AND WHICH ONE IS "THE BUILD" HAS CHANGED. A send subscribes with NO
 * `turnId` — it is joining the turn its own POST just started, so the id is the server's to know
 * — and a RE-ATTACH subscribes WITH one, because it is joining a turn it did not start. The
 * Build-it press used to be a third shape: it started a build in THIS chat and watched it with an
 * id. It is a handoff now, so that shape is gone, and on a build chat the plain send IS the build.
 *
 * By default the no-`turnId` branch replays `plan` and completes, which is what a suite driving a
 * PLAN chat's reply wants. Pass `hold: true` when the send is the build being asserted on, and the
 * same socket is held open instead — one helper, both shapes, rather than a local copy per suite.
 */
export function scriptBuildTurn({ plan = planReply(), opening = [T_WORKSPACE()], hold = false } = {}) {
  const live = { emit: null, close: null }
  const impl = async ({ turnId, onFrame }) => {
    if (!turnId && !hold) {
      for (const frame of plan) onFrame(frame)
      return 'completed'
    }
    live.emit = onFrame
    for (const frame of opening) onFrame(frame)
    return new Promise((resolve) => { live.close = resolve })
  }
  return {
    impl,
    /** Push more frames into the open build turn (wrapped in act, so effects flush between). */
    frame: async (...frames) => {
      await act(async () => { for (const frame of frames) live.emit?.(frame) })
    },
    /** Close the socket. The TRANSPORT outcome only; the frames decide the semantic one. */
    end: async (outcome = 'completed') => {
      await act(async () => { live.close?.(outcome); await Promise.resolve() })
    },
  }
}

/** Give the per-file `h` bag its default TURN resolutions (call inside beforeEach). */
export function primeTurn(h, frames = planReply()) {
  h.startTurn.mockResolvedValue({ turnId: 't1' })
  h.readTurnStream.mockImplementation(turnStreaming(frames))
  // THE HANDOFF'S ANSWER: `chatId` is the chat it CREATED and the one the press navigates to, so
  // it is the field the caller actually acts on. `sessionId`, `appId`, `reason` and the
  // `build_failed` / `already_built` / `stale_plan` outcomes are all gone from the contract.
  h.buildFromPlan.mockResolvedValue({
    outcome: 'started',
    chatId: BUILD_CHAT_ID,
    turnId: BUILD_TURN_ID,
  })
  h.stopTurn?.mockResolvedValue('stopping')
}

/** The thread composer. */
export const composer = () => screen.getByPlaceholderText(/describe what you need/i)

/**
 * Wait out the composer gate's OPENING state (G1).
 *
 * Send stays unavailable until the adopt round-trip has answered "is a build still running in this
 * chat?" — opening it over a possibly-live build is the bug the gate exists to prevent, and a real
 * user cannot outrun that round-trip either. Deliberately waits for the CHECKING copy only, not
 * for the note to vanish: several tests send while a build IS running, to assert the refusal.
 */
export const waitForGateOpen = () =>
  waitFor(() => expect(screen.queryByText(/checking whether a build/i)).toBeNull())

/** Type into the thread composer and send (Enter — the send button is icon-only, so unnamed). */
export async function send(text = 'a visitor app') {
  await waitForGateOpen()
  fireEvent.change(composer(), { target: { value: text } })
  fireEvent.keyDown(composer(), { key: 'Enter' })
}

/**
 * The full PRESS path: send a turn, wait for the plan-options card, click Build it.
 *
 * WHAT THIS IS FOR HAS NARROWED. It used to be how a test reached a build at all — the plan
 * streamed, the card presented, the click flipped this thread into Write and streamed the build
 * here. The click is a HANDOFF now: it creates a second chat, starts the turn there and
 * navigates, so nothing after it streams into the chat the button was in.
 *
 * So use this when the press ITSELF is the subject (that `buildFromPlan` is called, with the
 * minted id, and where it lands). For a test that needs a build STREAMING on this page, send
 * ordinarily — this page renders a build chat, and every send on one is a build turn.
 */
export async function sendAndConfirm(text = 'a visitor app') {
  await send(text)
  const build = await screen.findByRole('button', { name: /^Build this plan$/ })
  fireEvent.click(build)
  return build
}

/** Wait until a plan-options card's Build-it is on screen (without confirming it). */
export const findPlanCard = () => screen.findByRole('button', { name: /^Build this plan$/ })

/**
 * Annotated because TypeScript suites use this harness too (`*.test.tsx`), and without it TS
 * infers each option's type from its DEFAULT — so `hasSavedBuild` came out as the literal
 * `null` and a suite that passed `false` (a legitimate, load-bearing value: "the server
 * confirmed there is no saved build") failed to typecheck.
 *
 * @param {{
 *   deps?: object,
 *   projectId?: string,
 *   hasSavedBuild?: boolean | null,
 *   initialEntries?: string[],
 * }} [opts]
 */
export function renderBuilder({ deps, projectId = 'p1', hasSavedBuild = null, initialEntries = ['/chat/build-X?projectId=p1&kind=build'] } = {}) {
  return render(
    <MemoryRouter initialEntries={initialEntries}>
      <Routes>
        <Route element={<WorkspaceShell />}>
          <Route path="/chat/:chatId" element={<ConversationSurface projectId={projectId} projectName="VIP Movement" projectHasSavedBuild={hasSavedBuild} buildSessionDeps={deps} />} />
        </Route>
        <Route path="/projects" element={<div>projects index</div>} />
        <Route path="/projects/:pid" element={<div>project page</div>} />
      </Routes>
    </MemoryRouter>,
  )
}

// ─── Plan A / U1: fixtures for the PREVIEW ADDRESS and its two scoping predicates ─────────────
//
// The address has three sources and two predicates, and a predicate is only OBSERVABLE when the
// chat or the project on screen differs from the one the signal was attributed to. `renderBuilder`
// cannot express that: it mounts one identity and never moves. These two fixtures supply the
// missing halves — a transcript that attributes a SESSION to whatever project is on screen, and a
// render helper that can move the SAME BuilderPage instance to a sibling chat or another project.

/**
 * A transcript whose newest assistant part anchors a build with no recorded outcome.
 *
 * This is all a reattach needs (`reattachToLiveBuild`): the page reads the session id off the
 * anchor, stamps `sessionChatRef`/`sessionProjectRef` with the identities it is CURRENTLY mounted
 * at, and calls `getStatus`. Pair it with a `getStatus` that answers with a `previewUrl` and the
 * session arm of the address is live, attributed to the project that was on screen.
 */
export const withLiveBuildAnchor = (sessionId = 'live-7', over = {}) => ({
  id: 'build-X',
  kind: 'builder',
  mode: 'plan',
  messages: [
    { id: 'm0', role: 'user', seq: 0, parts: [{ type: 'text', text: 'a visitor app' }] },
    { id: 'srv_1_g', role: 'assistant', seq: 1, parts: [{ type: 'build_in_progress', sessionId }] },
  ],
  ...over,
})

/**
 * Render BuilderPage at an EXPLICIT chat/project identity, and hand back a `moveTo` that changes
 * it without remounting.
 *
 * Flat routing means one BuilderPage instance survives every chat and project move — only its
 * props change — so `moveTo` is what the product actually does, not a test shortcut. The router
 * entry is deliberately constant and the identities arrive as PROPS (`chatId` wins over
 * `useParams`): a MemoryRouter reads `initialEntries` once at mount, so re-rendering with a new
 * one would change nothing and the test would silently assert against the original identity.
 *
 * @param {{
 *   chatId?: string,
 *   projectId?: string,
 *   projectName?: string,
 *   hasSavedBuild?: boolean | null,
 *   deps?: object,
 * }} [opts]
 */
export function renderBuilderAt({
  chatId = 'chat-A',
  projectId = 'pA',
  projectName = 'VIP Movement',
  hasSavedBuild = null,
  deps,
} = {}) {
  const at = { chatId, projectId, projectName }
  const tree = () => (
    <MemoryRouter initialEntries={['/chat/routed']}>
      <Routes>
        <Route element={<WorkspaceShell />}>
          <Route
            path="/chat/:chatId"
            element={
              <ConversationSurface
                chatId={at.chatId}
                projectId={at.projectId}
                projectName={at.projectName}
                projectHasSavedBuild={hasSavedBuild}
                buildSessionDeps={deps}
              />
            }
          />
        </Route>
        <Route path="/projects" element={<div>projects index</div>} />
        <Route path="/projects/:pid" element={<div>project page</div>} />
      </Routes>
    </MemoryRouter>
  )
  const view = render(tree())
  return {
    ...view,
    /** Move the SAME instance to another chat and/or project. */
    moveTo: (next) => {
      Object.assign(at, next)
      view.rerender(tree())
    },
    /** Re-render at the SAME identity — the no-op move an identity assertion needs. */
    rerenderSame: () => view.rerender(tree()),
  }
}
