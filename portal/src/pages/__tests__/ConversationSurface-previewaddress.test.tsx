/**
 * WHY THIS EXISTS — pins the preview address exactly as it resolves today, ahead of the
 * workspace-shell extraction that turns this three-source precedence into a named resolver
 * called from above the chat. A resolver that "tidied" the two gating predicates into one must
 * not pass unnoticed, so this pins the asymmetry below that looks like a bug and is not.
 *
 * THE RULE: a live turn's preview outranks a relaunched URL, which outranks the session's URL —
 * the turn arm is gated by the CHAT predicate alone, the lower two by the PROJECT predicate alone.
 *
 * Why the lower arm below is usually the relaunched URL: a session still framing is by definition
 * an ACTIVE build, which closes this chat's own composer gate — so a scenario needing both a
 * lower arm and a send can't use it. A relaunch has no lifecycle at all, so it frames without
 * gating anything.
 *
 * NOT RE-PINNED HERE (already pinned once, elsewhere):
 *  - composer draft + scroll across a hide/show cycle → `ProjectWorkspace.test.tsx`
 *  - a send refused mid-turn → `ConversationSurface-composer.test.jsx`, `-session.test.jsx`
 *  - cross-project build-gate isolation → `ConversationSurface-session.test.jsx`
 *  - the reload nonce's two legitimate bumps → `components/__tests__/LivePreview.test.jsx`
 *
 * The pane is the REAL LivePreview; a recording wrapper captures its props on the way through,
 * since the app-scoped ones (`compileState`, `workspaceLost`) are how this file proves the chat
 * predicate does NOT reach them.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { createElement } from 'react'
import { act, screen, waitFor, cleanup, fireEvent } from '@testing-library/react'
import {
  FakeEventSource, makeClient, primeClient, primeTurn, renderBuilderAt, withLiveBuildAnchor,
  statusResp, send, scriptBuildTurn, T_PREVIEW, T_BUILD_END, turnStreaming,
  T_DELTA, T_END, findStartAppControl, primeStandbyReattach,
} from './_builderSession.jsx'
import type { PreviewLifeState, PreviewState } from '../../utils/buildSessionApi'

/** The four arms, given URLs that cannot be confused with one another. */
const SESSION_URL = 'https://session-app.example.azurecontainerapps.io/'
const TURN_URL = 'https://turn-app.example.azurecontainerapps.io/'
const RELAUNCH_URL = 'https://relaunched-app.example.azurecontainerapps.io/'
/** The PROJECT arm's — the one a hard load arrives on, answered by the preview-state read. */
const PROJECT_URL = 'https://project-app.example.azurecontainerapps.io/'

const h = vi.hoisted(() => ({
  loadBuilds: vi.fn(), getBuild: vi.fn(),
  listProjectConversations: vi.fn(), buildUserParts: vi.fn(),
  startTurn: vi.fn(), readTurnStream: vi.fn(), buildFromPlan: vi.fn(), stopTurn: vi.fn(),
  resolvePlanOptions: vi.fn(),
  relaunchPreview: vi.fn(), stop: vi.fn(), getStatus: vi.fn(),
  fetchPreviewState: vi.fn(), fetchSaveState: vi.fn(),
  // THE TWO PROBES THAT RIDE THE PREVIEW TICK, mocked only so they cannot reach a real `fetch`.
  // Neither was needed while every scenario here answered the read `unknown`: both are gated on a
  // LIVE container, and the project arm's scenarios below are the first in this file to produce
  // one. They never throw in production either (both swallow and answer a safe default), so
  // leaving them real would not have failed a test — it would have made every one of those
  // scenarios open a socket to nowhere and wait for it.
  fetchCompileState: vi.fn(), checkWorkspace: vi.fn(),
}))

/** Every prop bag the pane has been handed, in order. */
const paneProps: Record<string, unknown>[] = []

vi.mock('../../utils/builderHistory', () => ({
  loadBuilds: h.loadBuilds, getBuild: h.getBuild, deriveTitle: (t: string) => (t || '').slice(0, 40),
}))
vi.mock('../../utils/conversationApi', () => ({
  // The send path creates the chat before its first upload; stubbed so no network is reached.
  createConversation: async () => ({ id: 'conv-created' }),
  listProjectConversations: h.listProjectConversations,
}))
vi.mock('../../components/layout/Navbar', () => ({ default: () => null }))
// A recording wrapper, not a stub — see the module docblock for why.
vi.mock('../../components/LivePreview', async (orig) => {
  const actual = await orig<typeof import('../../components/LivePreview')>()
  return {
    ...actual,
    default: (props: Record<string, unknown>) => {
      paneProps.push(props)
      return createElement(actual.default, props)
    },
  }
})
vi.mock('../../utils/attachmentStore', async (orig) => ({
  ...(await orig<typeof import('../../utils/attachmentStore')>()),
  buildUserParts: h.buildUserParts,
}))
// `switchMode` is GONE — a chat's kind is fixed at creation. `resolvePlanOptions` stays mocked
// because the surface reaches for it when a plan offer is answered, even though it's never
// exercised here.
vi.mock('../../utils/turnStreamApi', async (orig) => ({
  ...(await orig<typeof import('../../utils/turnStreamApi')>()),
  startTurn: (...a: unknown[]) => h.startTurn(...a),
  readTurnStream: (...a: unknown[]) => h.readTurnStream(...a),
  buildFromPlan: (...a: unknown[]) => h.buildFromPlan(...a),
  resolvePlanOptions: (...a: unknown[]) => h.resolvePlanOptions(...a),
  stopTurn: (...a: unknown[]) => h.stopTurn(...a),
}))
vi.mock('../../utils/buildSessionApi', async (orig) => ({
  ...(await orig<typeof import('../../utils/buildSessionApi')>()),
  fetchPreviewState: (...a: unknown[]) => h.fetchPreviewState(...a),
  fetchSaveState: (...a: unknown[]) => h.fetchSaveState(...a),
  fetchCompileState: (...a: unknown[]) => h.fetchCompileState(...a),
  checkWorkspace: (...a: unknown[]) => h.checkWorkspace(...a),
  // `StartAppControl.tsx` imports `relaunchPreview` DIRECTLY from this module rather than through
  // the injected client, so its call has to land on the same `h.relaunchPreview` the fixtures
  // below already prime.
  relaunchPreview: (...a: unknown[]) => h.relaunchPreview(...a),
}))

const deps = () => {
  const fake = new FakeEventSource('x')
  return { client: makeClient(h), eventSourceFactory: () => fake }
}

const frame = () => document.querySelector('iframe')
const framedUrl = () => frame()?.getAttribute('src') ?? null
/** The newest value the pane was handed for `name` — the app-scoped props read this. */
const lastPaneProp = (name: string) => paneProps[paneProps.length - 1]?.[name]

beforeEach(() => {
  vi.clearAllMocks()
  paneProps.length = 0
  sessionStorage.clear()
  Element.prototype.scrollIntoView = vi.fn()
  primeClient(h)
  primeTurn(h)
  h.getBuild.mockResolvedValue(null)
  h.loadBuilds.mockResolvedValue([])
  h.listProjectConversations.mockResolvedValue([])
  h.buildUserParts.mockImplementation(async (t: string) => [{ type: 'text', text: t }])
  h.relaunchPreview.mockResolvedValue({
    appId: 'a1', previewUrl: RELAUNCH_URL, status: 'ready', restoredFromFailedBuild: false,
  })
  // Neither probe is this file's subject; both are answered so nothing reaches a real `fetch`.
  h.fetchPreviewState.mockResolvedValue({
    state: 'unknown', alive: false, previewUrl: null, occupyingProjectName: null, restorable: null,
  })
  h.fetchSaveState.mockResolvedValue({ dirty: null })
  h.fetchCompileState.mockResolvedValue('unknown')
  h.checkWorkspace.mockResolvedValue(false)
})
afterEach(() => cleanup())

/**
 * Brings up a page whose RELAUNCH arm is live, stamped to `projectId`.
 *
 * The vehicle is `StartAppControl` (the old Relaunch-button affordance is gone):
 * `primeStandbyReattach` stamps the ref its own click path never touches, and
 * `findStartAppControl` presses whichever label it's currently showing. Full account,
 * including a real product bug this uncovered, is in `_builderSession.jsx`'s docblock.
 */
async function relaunchFramedAt(chatId: string, projectId: string) {
  const reattach = primeStandbyReattach(h, { chatId, projectId })
  const view = renderBuilderAt({ chatId, projectId, hasSavedBuild: true, deps: deps() })
  await waitFor(() => expect(h.getStatus).toHaveBeenCalled())
  fireEvent.click(await findStartAppControl())
  await waitFor(() => expect(h.relaunchPreview).toHaveBeenCalled())
  await waitFor(() => expect(framedUrl()).toBe(RELAUNCH_URL))
  // Several callers send a turn right after this returns — a reattach left pending would keep
  // the composer gate shut on them for good (see `primeStandbyReattach`'s docblock).
  reattach.settle()
  await waitFor(() => expect(screen.queryByText(/checking whether a build/i)).toBeNull())
  return view
}

/** A turn that streams one preview frame and completes — the chat-scoped arm, on demand. */
const turnFraming = (url: string) =>
  turnStreaming([T_DELTA('working on it'), T_PREVIEW(url), T_END()])

describe('BuilderPage — the preview address: three sources, two predicates', () => {
  it('a live turn preview outranks a relaunched URL when BOTH predicates hold', async () => {
    const view = await relaunchFramedAt('chat-A', 'pA')

    h.readTurnStream.mockImplementation(turnFraming(TURN_URL))
    await send('add a chart')

    await waitFor(() => expect(framedUrl()).toBe(TURN_URL))
    view.unmount()
  })

  it('a turn narrating a SIBLING chat of the same project does not frame — the relaunched URL does', async () => {
    // The chat predicate, violated on its own. The turn's URL is still in state; it is simply not
    // this chat's turn, and a resolver that dropped `turnNarrativeIsThisChat` would frame a
    // sibling conversation's app over this one.
    const view = await relaunchFramedAt('chat-A', 'pA')
    h.readTurnStream.mockImplementation(turnFraming(TURN_URL))
    await send('add a chart')
    await waitFor(() => expect(framedUrl()).toBe(TURN_URL))

    view.moveTo({ chatId: 'chat-B' }) // same project, sibling conversation

    await waitFor(() => expect(framedUrl()).toBe(RELAUNCH_URL))
    view.unmount()
  })

  it('with the project predicate false and no live turn, the pane frames NOTHING', async () => {
    // Both lower arms are gated by the project predicate, so the address resolves to null. Never a
    // fallback, and above all never the other project's app.
    const view = await relaunchFramedAt('chat-A', 'pA')

    view.moveTo({ chatId: 'chat-B', projectId: 'pB' })

    await waitFor(() => expect(frame()).toBeNull())
    view.unmount()
  })

  it('THE ASYMMETRY: the project predicate is false and the turn still frames', async () => {
    // The cell a resolver that "tidied" the two predicates into one would get wrong. The turn arm
    // is chat-scoped ONLY — an ordinary send stamps the turn narrative and never the session's
    // project — so a turn narrating the open chat frames even from a project the lower arms are
    // out of scope for.
    const view = await relaunchFramedAt('chat-A', 'pA')

    view.moveTo({ chatId: 'chat-B', projectId: 'pB' })
    await waitFor(() => expect(frame()).toBeNull()) // the lower arms are gated off, as above

    h.readTurnStream.mockImplementation(turnFraming(TURN_URL))
    await send('build me something here')

    await waitFor(() => expect(framedUrl()).toBe(TURN_URL))
    view.unmount()
  })

  it('a relaunched URL outranks the session\'s own URL', async () => {
    // The middle of the precedence, which only shows when both lower arms are populated at once: a
    // relaunch restores an app the ENDED session's dead preview would otherwise still be naming.
    // `asleep`+`restorable` is what resolves the workspace map to `not-running` for that dead
    // session. The poll only runs once something is framed, so the sequence here is mount, let the
    // poll answer, THEN press.
    h.getBuild.mockResolvedValue(withLiveBuildAnchor('live-7'))
    h.getStatus.mockResolvedValue(
      statusResp({ sessionId: 'live-7', status: 'ended', previewUrl: SESSION_URL }),
    )
    h.fetchPreviewState.mockResolvedValue({
      state: 'asleep', alive: false, previewUrl: null, occupyingProjectName: null, restorable: true,
    })
    const view = renderBuilderAt({ chatId: 'chat-A', projectId: 'pA', hasSavedBuild: true, deps: deps() })
    await waitFor(() => expect(h.getStatus).toHaveBeenCalledWith('live-7'))

    fireEvent.click(await findStartAppControl())
    await waitFor(() => expect(h.relaunchPreview).toHaveBeenCalled())
    // The press doesn't itself change what the workspace map says — `onStartOutcome` only asks it
    // again. Answer `alive` now so the frame this test is actually about gets a chance to mount.
    h.fetchPreviewState.mockResolvedValue({
      state: 'alive', alive: true, previewUrl: RELAUNCH_URL, occupyingProjectName: null, restorable: null,
    })
    fireEvent.focus(window)

    await waitFor(() => expect(framedUrl()).toBe(RELAUNCH_URL))
    view.unmount()
  })

  it('the session\'s URL frames on its own, and only for the project it belongs to', async () => {
    // The bottom arm, and the project predicate that gates it. Nothing is sent here — a session
    // still framing is an ACTIVE build, which closes this chat's composer by design.
    h.getBuild.mockResolvedValue(withLiveBuildAnchor('live-7'))
    h.getStatus.mockResolvedValue(
      statusResp({ sessionId: 'live-7', status: 'ready', previewUrl: SESSION_URL }),
    )
    const view = renderBuilderAt({ chatId: 'chat-A', projectId: 'pA', deps: deps() })
    await waitFor(() => expect(framedUrl()).toBe(SESSION_URL))

    // The SAME session, viewed from another project: out of scope, so it reaches nothing.
    h.getBuild.mockResolvedValue(null)
    view.moveTo({ chatId: 'chat-B', projectId: 'pB' })

    await waitFor(() => expect(frame()).toBeNull())
    view.unmount()
  })
})

describe('BuilderPage — the app-scoped props are NOT narrowed to the open chat', () => {
  it('the compile state reaches the pane while the narrating chat is a sibling', async () => {
    // `compileState`/`workspaceLost` are facts about the PROJECT'S ONE APP, deliberately ungated
    // by `turnNarrativeIsThisChat` — blanking them on a chat switch is what leaves an error screen
    // uncovered.
    const view = await relaunchFramedAt('chat-A', 'pA')
    h.readTurnStream.mockImplementation(
      turnStreaming([T_DELTA('working'), T_PREVIEW(TURN_URL), { type: 'compile', seq: 4, state: 'failed' }, T_END()]),
    )
    await send('add a chart')
    await waitFor(() => expect(lastPaneProp('compileState')).toBe('failed'))

    view.moveTo({ chatId: 'chat-B' }) // sibling chat — the chat predicate is now false

    // The address followed the predicate (the turn's URL is gone); the compile fact did not.
    await waitFor(() => expect(framedUrl()).toBe(RELAUNCH_URL))
    expect(lastPaneProp('compileState')).toBe('failed')
    view.unmount()
  })
})

describe('BuilderPage — the frame\'s identity is its ADDRESS, and nothing else', () => {
  it('re-rendering at the same address keeps the SAME iframe node and does not re-issue its src', async () => {
    // `LivePreview.test.jsx` already pins that a same-key render keeps the node; unproven without
    // this is that the PAGE keeps handing it the same address across an ordinary re-render.
    const view = await relaunchFramedAt('chat-A', 'pA')
    const before = frame()
    let loads = 0
    before?.addEventListener('load', () => { loads += 1 })

    view.rerenderSame()

    expect(frame()).toBe(before)
    expect(framedUrl()).toBe(RELAUNCH_URL)
    expect(loads).toBe(0)
    view.unmount()
  })

  it('a turn ending on the SAME url does not re-frame', async () => {
    // Half of the failure this pins. Re-deriving the frame's key from anything but the address —
    // the route, a render counter, the turn's terminal — reloads a live app for no reason and
    // takes its HMR socket with it. The other half is the scenario below.
    const view = await relaunchFramedAt('chat-A', 'pA')

    // `hold` — the send IS the build here, so its socket has to stay open for the frames
    // pushed in below rather than replaying a plan and completing.
    const turn = scriptBuildTurn({ hold: true })
    h.readTurnStream.mockImplementation(turn.impl)
    // An ordinary send, not the plan card: this page renders a BUILD chat, so every send on it is
    // already a build turn — the card would hand off to a SECOND chat whose turn would never
    // stream into this frame.
    await send('a visitor app')
    // NO `turnId` in the subscribe is the send path's shape, not an oversight: a send subscribes
    // to whatever turn its own POST just started, so the id is the server's to know. Only a
    // RE-ATTACH names a turn, because it's joining one it didn't start.
    await waitFor(() =>
      expect(h.readTurnStream).toHaveBeenCalledWith(
        expect.objectContaining({ conversationId: 'chat-A' }),
      ),
    )
    await turn.frame(T_PREVIEW(TURN_URL))
    await waitFor(() => expect(framedUrl()).toBe(TURN_URL))
    const framedByTheTurn = frame()

    await turn.frame(T_BUILD_END({ previewUrl: TURN_URL }))
    await turn.end()

    expect(frame()).toBe(framedByTheTurn)
    expect(framedUrl()).toBe(TURN_URL)
    view.unmount()
  })

  it('★ a SECOND SEND does not remount the app', async () => {
    // ★ THE DEFECT, MEASURED THE WAY IT WAS ORIGINALLY PROVEN. Stamping a marker on the live
    // `<iframe>` before the send and finding `marked === false` when the card came back: the
    // element was REPLACED, so the generated app re-requested its document on every message and
    // discarded its in-app state — form entries, selected tab, scroll position. The backend read
    // `{"state":"alive"}` before and after; same container, same URL throughout.
    //
    // SO THIS ASSERTS ELEMENT IDENTITY, NOT THE ADDRESS MATCHING. Those are different claims: the
    // src is byte-identical across a remount, which is exactly why the URL comparison the earlier
    // scenarios use cannot see this. The dataset marker is carried too, because it is the same
    // evidence the issue was closed on and it survives nothing but the original node.
    //
    // WHAT MADE THE FRAME COME DOWN: a send resets the turn narrative, so the turn's status drops
    // to `null` while its preview URL stays — and the status fell through to the transcript's own
    // `'ended'`. With liveness spelled as "a turn TERMINATED successfully", it was false for that
    // render, `keepFramed` collapsed, and `frameContext` unmounted the iframe. Liveness on the
    // address answers from the preview the turn published, which does not blink off at a send.
    //
    // Mutation check: narrow the resolver's turn arm to `turnStatus === 'ended'` and this goes red
    // with `frame()` a different node — and the pane briefly reading "no longer running", which is
    // what the citizen was shown over a container that was up the whole time.
    h.getBuild.mockResolvedValue({
      id: 'chat-A',
      messages: [
        { id: 'm0', role: 'user', parts: [{ type: 'text', text: 'a visitor app' }], seq: 0 },
        {
          id: 'm1',
          role: 'assistant',
          seq: 1,
          // The persisted build outcome is what makes the address's status fall to `'ended'` on the
          // send. Without it the scenario is vacuous — nothing terminal, so nothing to outrank.
          parts: [{ type: 'build', status: 'ended', sessionId: 's-old', previewUrl: TURN_URL }],
        },
      ],
    })
    const view = renderBuilderAt({ chatId: 'chat-A', projectId: 'pA', deps: deps() })

    h.readTurnStream.mockImplementation(turnFraming(TURN_URL))
    await send('build me a visitor app')
    await waitFor(() => expect(framedUrl()).toBe(TURN_URL))

    const framed = frame()
    // The app's own state, standing in for the form entries and scroll position a remount loses.
    framed?.setAttribute('data-citizen-typed-here', 'yes')

    // THE SECOND MESSAGE. `hold: true` so the turn stays open across the assertions below — the
    // remount happened at the START of a send, which is the moment being pinned.
    const second = scriptBuildTurn({ hold: true })
    h.readTurnStream.mockImplementation(second.impl)
    await send('now add a chart')

    expect(frame()).toBe(framed)
    expect(frame()?.getAttribute('data-citizen-typed-here')).toBe('yes')
    // …and the pane never tells them their preview ended while it is up. Paired with the identity
    // assertion above, so a pane that rendered nothing at all cannot satisfy the absence.
    expect(screen.queryByText(/no longer running/i)).toBeNull()
    view.unmount()
  })

  it('a different project is a different app, so a different address, so a genuine remount', async () => {
    // The other half. Implementing "never unmount" by pinning the key to a constant satisfies the
    // scenario above and leaves a frame pointing at a container that no longer exists, with nothing
    // able to detect it.
    const view = await relaunchFramedAt('chat-A', 'pA')
    const before = frame()

    view.moveTo({ chatId: 'chat-B', projectId: 'pB' })
    await waitFor(() => expect(frame()).toBeNull())

    view.moveTo({ chatId: 'chat-A', projectId: 'pA' })
    await waitFor(() => expect(framedUrl()).toBe(RELAUNCH_URL))
    expect(frame()).not.toBe(before)
    view.unmount()
  })
})

/**
 * THE FOURTH ARM: the project's own live preview, on a CHAT route.
 *
 * WHAT WAS BROKEN. Reload a build chat whose app is up — a bookmark, an F5, a browser restart —
 * and the headline said the app was running while the pane framed nothing. Two independent
 * reasons, and fixing either alone changes nothing on screen:
 *
 *  1. this surface fed the resolver `projectPreviewUrl: null`, so the read it was already making
 *     could not reach the address at all;
 *  2. every project-scoped arm is gated by `sessionBelongsToOpenProject`, which this route supplies
 *     as `sessionProjectMatches` — a ref stamped only by a reattach or by the start control. A hard
 *     load has done neither, so it is `false` and the arm is closed whatever it is fed.
 *
 * The scenarios below pin the pair, and the LOOP GUARD pins the third part: with the address now
 * downstream of the poll's own answer, keeping the framed URL in the poll effect's dependency list
 * makes the effect tear itself down on every successful read — its first statement is
 * `setPolledPreview(null)` — for an unbounded stream of requests and a flapping iframe `src`. The
 * count is asserted, not "settled": a self-re-arming effect settles too, one request at a time,
 * forever.
 */
describe('BuilderPage — the project arm, and the hard load it exists for', () => {
  /** A whole preview-state body, in the shape `fetchPreviewState` parses one into. */
  const polled = (state: PreviewLifeState, restorable: boolean | null = null): PreviewState => ({
    state,
    alive: state === 'alive',
    previewUrl: state === 'alive' ? PROJECT_URL : null,
    occupyingProjectName: null,
    occupyingProjectId: null,
    restorable,
  })
  const probeCount = () => h.fetchPreviewState.mock.calls.length
  /** One task turn, inside act — no clock is moved, so nothing here is a poll TICK. A macrotask
   *  rather than a microtask, so a deferred probe answer (the loop guard's) actually lands. */
  const flush = () => act(async () => { await new Promise((resolve) => { setTimeout(resolve, 0) }) })

  it('a hard load with no session frames the app the poll says is running', async () => {
    // The whole bug, in one scenario: no anchor, no session, no press — just the route and the
    // read it already makes. Before the fix this rendered the running-app copy over an empty pane.
    h.fetchPreviewState.mockResolvedValue(polled('alive'))

    const view = renderBuilderAt({ chatId: 'chat-A', projectId: 'pA', deps: deps() })

    await waitFor(() => expect(framedUrl()).toBe(PROJECT_URL))
    expect(document.querySelectorAll('iframe')).toHaveLength(1)
    view.unmount()
  })

  it('THE LOOP GUARD: a successful read does not re-arm the effect that made it', async () => {
    // Parts 1 and 2 are individually harmless and JOINTLY are the bug: once the address is a
    // function of the probe's own answer, an effect that also depends on the address tears itself
    // down on every successful read — its first statement is `setPolledPreview(null)` — for an
    // unbounded stream of requests and a flapping iframe `src`.
    //
    // ★ THE ANSWER MUST ARRIVE ON A LATER TASK, AND THAT IS THE WHOLE TEST. With
    // `mockResolvedValue` the mutant is INERT: the answer lands in the same flush as the effect's
    // own `setPolledPreview(null)`, React coalesces the two into one commit, the dependency never
    // observes the flicker and the loop never starts — verified, the reverted fix passed at
    // exactly two reads. A real network answers a task later, so the `null` commit lands FIRST and
    // the effect re-runs on it. One `setTimeout` is the difference between this guard and a green
    // test that proves nothing.
    h.fetchPreviewState.mockImplementation(
      () => new Promise<PreviewState>((resolve) => { setTimeout(() => resolve(polled('alive')), 0) }),
    )
    const view = renderBuilderAt({ chatId: 'chat-A', projectId: 'pA', deps: deps() })
    await waitFor(() => expect(framedUrl()).toBe(PROJECT_URL))

    // ONE read got us here — the mount's. Asserted as a number rather than as "it stopped",
    // because a self-re-arming effect stops too, between one request and the next, forever.
    const settled = probeCount()
    expect(settled).toBe(1)

    // Twelve renders at the same identity, each given a full task turn to fire anything it armed.
    for (let i = 0; i < 12; i += 1) {
      view.rerenderSame()
      await flush()
    }

    expect(probeCount()).toBe(settled)
    expect(framedUrl()).toBe(PROJECT_URL) // and the src never flapped
    view.unmount()
  })

  it('an SPA move to a sibling chat keeps the SAME frame — the app does not reload', async () => {
    // The path that works today, and the one this unit must not regress. The project arm is a fact
    // about the project, so a move between its conversations is not an invalidation: the address is
    // byte-identical, the iframe node is the same node, and the app inside it never reloads.
    h.fetchPreviewState.mockResolvedValue(polled('alive'))
    const view = renderBuilderAt({ chatId: 'chat-A', projectId: 'pA', deps: deps() })
    await waitFor(() => expect(framedUrl()).toBe(PROJECT_URL))
    const before = frame()

    view.moveTo({ chatId: 'chat-B' }) // same project, sibling conversation

    await waitFor(() => expect(framedUrl()).toBe(PROJECT_URL))
    expect(frame()).toBe(before)
    view.unmount()
  })

  it('a live turn\'s preview still outranks the project address', async () => {
    // The precedence is unchanged: the new arm is ranked LAST, so it can never displace the turn
    // the citizen is watching. (This is also the scenario that exercises the fold — the turn's
    // `preview` frame is one of the two lifecycle signals now folded into the probe epoch.)
    h.fetchPreviewState.mockResolvedValue(polled('alive'))
    const view = renderBuilderAt({ chatId: 'chat-A', projectId: 'pA', deps: deps() })
    await waitFor(() => expect(framedUrl()).toBe(PROJECT_URL))

    h.readTurnStream.mockImplementation(turnFraming(TURN_URL))
    await send('add a chart')

    await waitFor(() => expect(framedUrl()).toBe(TURN_URL))
    view.unmount()
  })

  it('a `slot_taken` answer frames NOTHING, and the held arm renders instead', async () => {
    // The arm's contract is `alive` and nothing else. Another project is holding the one slot, so
    // there is no framable URL — and the pane says so rather than framing a guess.
    h.fetchPreviewState.mockResolvedValue(polled('slot_taken', true))

    const view = renderBuilderAt({ chatId: 'chat-A', projectId: 'pA', deps: deps() })

    await waitFor(() => expect(screen.queryByTestId('app-pane-empty')).not.toBeNull())
    // ★ ONE HELD ARM NOW, WHATEVER THE SERVER COULD ATTRIBUTE. This read to `held-unattributed`,
    // a second held state offering `action` and `secondAction` both null — a card that named the
    // problem, named no remedy and left nothing to press at all. A missing holder name is a reason
    // to say LESS, not to DO less, so the merge keeps the take-back and degrades only the sentence.
    expect(screen.getByTestId('app-pane-empty').getAttribute('data-workspace-state'))
      .toBe('held-by-another-project')
    expect(frame()).toBeNull()
    view.unmount()
  })

  it('a move to another project never frames the project it just left, not for one commit', async () => {
    // WHAT THE STAMP MADE POSSIBLE, AND WHAT THE LABEL TAKES BACK. One instance of this component
    // survives a project switch, and the poll's answer is state — the effect that drops it runs
    // AFTER the commit. So the first render at the new project holds the previous project's live
    // URL while the stamp above already points at the project now on screen: one commit of
    // somebody else's app in this pane. `paneProps` records every bag the pane was handed, so the
    // window is visible here even though it closes before any `waitFor` could look.
    h.fetchPreviewState.mockImplementation(async (id: string) =>
      id === 'pA' ? polled('alive') : polled('asleep', true),
    )
    const view = renderBuilderAt({ chatId: 'chat-A', projectId: 'pA', deps: deps() })
    await waitFor(() => expect(framedUrl()).toBe(PROJECT_URL))
    const seen = paneProps.length

    view.moveTo({ chatId: 'chat-B', projectId: 'pB' })

    await waitFor(() => expect(frame()).toBeNull())
    expect(paneProps.slice(seen).map((props) => props.previewUrl)).not.toContain(PROJECT_URL)
    view.unmount()
  })
})
