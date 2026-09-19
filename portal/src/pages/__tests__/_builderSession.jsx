/**
 * Mock-free harness for the BuilderPage build-session suites: exports fixtures and a render
 * helper only.
 *
 * WHY THIS EXISTS
 *
 * Each test file declares its own vi.mock and injects the mock client plus FakeEventSource via
 * the `buildSessionDeps` prop; the real useBuildSession/LivePreview/ActivityFeed/SessionControls
 * hooks run, so tests assert real rendered DOM.
 *
 * A composer send is a TURN (POST /turns + the frame stream); the plan streams as text and
 * `present_plan_options` renders the card; a build starts only through the atomic Build-it
 * transition — mock `turnStreamApi`, prime with `primeTurn(h)`, and drive `sendAndConfirm()`.
 *
 * Build-it is a WRITE TURN now, not a build SESSION: `buildFromPlan` returns a `turnId`, never
 * a `sessionId`, and the page subscribes with the same `readTurnStream` an ordinary send uses.
 * `scriptBuildTurn()` drives one via workspace/step/preview/diagnostic/quota/turn_ended frames.
 * `FakeEventSource` now serves only the LEGACY session path (the reload-mid-build reattach).
 *
 * Not a `*.test.*` file — the runner never collects it.
 */
import { act, fireEvent, screen, render, waitFor } from '@testing-library/react'
import { expect } from 'vitest'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import ConversationSurface from '../../components/chat/ConversationSurface'
// THE REAL SHELL, not a stub — both render helpers below mount the page THROUGH it, so preview
// assertions have a pane to render into.
import WorkspaceShell from '../../components/workspace/WorkspaceShell'

/**
 * Nest routes under the REAL workspace shell — mounting the surface bare loses its pane, so
 * preview assertions would silently assert against nothing.
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

// Build-session response builders (camelCase). `over` lets a test tweak one field. `startResp`
// is GONE with the `start` it answered — nothing on this harness can post one any more.
export const statusResp = (over = {}) => ({ sessionId: 's1', projectId: 'p1', appId: 'a1', status: 'provisioning', previewUrl: null, lastSeq: null, createdAt: 'c', updatedAt: 'u', ...over })

/** Assemble a BuildSessionClient from a per-file `h` bag of vi.fn()s.
 *
 *  TWO MEMBERS NOW: `acquireLock`/`releaseLock` went with the keep-alive loop that was their
 *  only caller; `start` went the same way, because the build lives inside the turn's own
 *  transaction and nothing provisions a session from the browser; `forceEnd` and then the
 *  session-scoped `stop` each went with the route they spoke to. Pinned against the real client
 *  in `utils/__tests__/buildSessionApi.test.ts`. */
export function makeClient(h) {
  return {
    relaunchPreview: h.relaunchPreview,
    getStatus: h.getStatus,
  }
}

/** Give the per-file `h` bag its default happy resolutions (call inside beforeEach). */
export function primeClient(h) {
  h.getStatus.mockResolvedValue(statusResp())
}

// ─── The turn half (streamed plan + the options card) ───────────────────

/** The plan text the scripted turn streams — the card's Build-it executes it server-side. */
export const BRIEF = 'Build an application for BIAL that tracks visitor passes.'

export const PLAN_CARD_ID = 'opt-1'

// Turn-stream frame builders (camelCase — the wire format).
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

// ─── The BUILD half — a Write turn, narrated by TURN FRAMES ───────────────

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
 * running turn by hand and assert mid-flight. Close it with `end()`.
 *
 * TWO SUBSCRIBE SHAPES: a send subscribes with no `turnId` (joining the turn its own POST just
 * started) and replays `plan` by default; a re-attach subscribes WITH one. Pass `hold: true` when
 * the send IS the build being asserted on, to hold that socket open instead.
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
  // THE HANDOFF'S ANSWER: `chatId` is the chat it CREATED and navigates to. `sessionId`/`appId`/
  // `reason` and the old `build_failed`/`already_built`/`stale_plan` outcomes are gone.
  h.buildFromPlan.mockResolvedValue({
    outcome: 'started',
    chatId: BUILD_CHAT_ID,
    turnId: BUILD_TURN_ID,
  })
  h.stopTurn?.mockResolvedValue('stopping')
}

/** The thread composer. */
export const composer = () => screen.getByPlaceholderText(/ask for another change/i)

/**
 * Wait out the composer gate's OPENING state (G1): send stays unavailable until the adopt
 * round-trip answers whether a build is still running in this chat. Waits for the CHECKING copy
 * only, not for it to vanish — several tests send while a build IS running, to assert the refusal.
 */
export const waitForGateOpen = () =>
  waitFor(() => expect(screen.queryByText(/checking whether a build/i)).toBeNull())

/** Type into the thread composer and send (Enter — the send button is icon-only, so unnamed). */
export async function send(text = 'a visitor app') {
  await waitForGateOpen()
  fireEvent.change(composer(), { target: { value: text } })
  fireEvent.keyDown(composer(), { key: 'Enter' })
}

// ─── THE ONE START CONTROL, and the vehicle for pressing it from a fresh chat ──
//
// `StartAppControl.tsx` speaks one vocabulary regardless of which arm handed it the action:
// `action.kind === 'start'` labels it "Launch Application", `'retry'` labels it "Try again", and
// both presses call the identical `start()` — the label is cosmetic. Use this helper rather than
// hard-coding one string: hard-coding broke every one of these suites when the map's default
// answer (`could-not-read`) turned out to say "Try again", not "Launch Application".
export const findStartAppControl = () =>
  screen.findByRole('button', { name: /^(Launch Application|Try again)$/ })

/**
 * Stamps `sessionProjectRef` directly (no real reattach) — only a REATTACH stamps that ref, never
 * the control's own click path (confirmed empirically; filed as a real product bug, not papered
 * over here). Left PENDING on purpose: the ref stamps synchronously before `getStatus` resolves,
 * so a call that never settles still reaches `NoFrame`/`StartAppControl`; the composer gate stays
 * shut until the caller resolves the returned `settle()`.
 */
export function primeStandbyReattach(h, { chatId = 'chat-A', projectId = 'p1', sessionId = 'standby-1' } = {}) {
  let resolveStatus
  // KEYED BY ID, not a blanket `mockResolvedValue` — moving the SAME page instance to a sibling
  // chat triggers that chat's own adopt effect, and a blanket answer would overwrite
  // `resolveStatus`, so `settle()` would stop reaching the original session.
  h.getBuild.mockImplementation(async (id) =>
    id === chatId
      ? {
          id,
          kind: 'build',
          messages: [
            { id: 'm0', role: 'user', seq: 0, parts: [{ type: 'text', text: 'a visitor app' }] },
            { id: 'srv_1_g', role: 'assistant', seq: 1, parts: [{ type: 'build_in_progress', sessionId }] },
          ],
        }
      : null,
  )
  h.getStatus.mockImplementation(() => new Promise((resolve) => { resolveStatus = resolve }))
  return {
    /** Settle the reattach on an already-dead session (no URL, no status the resolver can use) —
     *  opens the composer gate without disturbing whatever `StartAppControl` already framed. */
    settle: () =>
      resolveStatus?.({
        sessionId, projectId, appId: 'a1', status: 'ended', previewUrl: null,
        lastSeq: null, createdAt: 'c', updatedAt: 'u',
      }),
  }
}

/**
 * The full PRESS path: send a turn, wait for the plan-options card, click Build it.
 *
 * The click is a HANDOFF, not a stream into this chat: it creates a second chat, starts the turn
 * there, and navigates. Use this when the press ITSELF is the subject; for a build STREAMING on
 * this page, send ordinarily — every send on a build chat is a build turn.
 */
export async function sendAndConfirm(text = 'a visitor app') {
  await send(text)
  const build = await screen.findByRole('button', { name: /^Build this plan$/ })
  fireEvent.click(build)
  return build
}

/**
 * Annotated because TS suites use this harness too — without it, `hasSavedBuild` infers as
 * literal `null` and a legitimate `false` fails to typecheck.
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

// ─── Fixtures for the PREVIEW ADDRESS and its two scoping predicates ─────────────
//
// A predicate is only OBSERVABLE when the chat/project on screen differs from the one a signal
// was attributed to — these two fixtures supply an anchor attributed to whatever project is on
// screen, and a render helper that can move the SAME instance to a sibling chat or project.

/**
 * A transcript whose newest assistant part anchors a build with no recorded outcome.
 *
 * This is all a reattach needs (`reattachToLiveBuild`): the page reads the session id off the
 * anchor and stamps `sessionChatRef`/`sessionProjectRef` with the identities it is CURRENTLY
 * mounted at, then calls `getStatus`. Pair it with a `getStatus` answering a `previewUrl`.
 */
export const withLiveBuildAnchor = (sessionId = 'live-7', over = {}) => ({
  id: 'build-X',
  kind: 'build',
  messages: [
    { id: 'm0', role: 'user', seq: 0, parts: [{ type: 'text', text: 'a visitor app' }] },
    { id: 'srv_1_g', role: 'assistant', seq: 1, parts: [{ type: 'build_in_progress', sessionId }] },
  ],
  ...over,
})

/**
 * Render BuilderPage at an EXPLICIT chat/project identity, and hand back a `moveTo` that changes
 * it without remounting — flat routing means one instance survives every chat/project move.
 * Identities arrive as PROPS deliberately: MemoryRouter reads `initialEntries` once at mount, so
 * a naive re-render would silently assert against the original identity.
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
