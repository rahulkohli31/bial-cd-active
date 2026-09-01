/**
 * CHARACTERIZATION — the preview address, exactly as it resolves today (Plan A, U1).
 *
 * WHAT THIS FILE IS FOR. The workspace-shell extraction moves the pane out of this page: the
 * three-source precedence becomes a named resolver called from above the chat (U2), and the iframe
 * becomes a shell-mounted host whose identity is that address plus its reload nonce (U4). Both
 * moves are claimed to be behaviour-preserving, and a suite that only exercised the happy path
 * would let a resolver that "tidied" the two predicates into one pass unnoticed. So this file pins
 * the address AS IT IS — including the asymmetry that looks like a bug and is not.
 *
 * THE THING BEING PINNED, in one line: a live turn's preview outranks a relaunched URL, which
 * outranks the session's URL — and the turn arm is gated by the CHAT predicate alone while the
 * lower two are gated by the PROJECT predicate alone.
 *
 * WHY THE LOWER ARM IS USUALLY THE RELAUNCHED URL HERE. A session whose status still frames is by
 * definition an ACTIVE build (`isActiveBuildStatus` counts `ready`), which closes the composer gate
 * on its own chat — so a scenario that needs both a lower arm and a send cannot use it. A relaunch
 * is the arm with no lifecycle at all (`useBuildSession.relaunch` deliberately leaves the session's
 * status untouched), so it frames without gating anything. The session arm has its own two
 * scenarios below, where nothing needs to be sent.
 *
 * WHAT IS DELIBERATELY NOT RE-PINNED HERE, because it is already pinned once and two assertions of
 * one fact drift apart:
 *  - the composer draft across a panel hide/show — `BuilderPage-panel.test.jsx:57`;
 *  - the scroll position across the same cycle — `:86`, the one that actually discriminates a
 *    CSS-hide from an unmount;
 *  - a send refused while a turn runs — `BuilderPage-composer.test.jsx:179`,
 *    `BuilderPage-session.test.jsx:316,338`;
 *  - cross-project isolation of the build gate — `BuilderPage-session.test.jsx:543`;
 *  - the reload nonce's two legitimate bumps, a turn ending over a live preview and the manual
 *    Reload — `components/__tests__/LivePreview.test.jsx:355` and `:375`.
 *
 * The pane is the REAL LivePreview, so the address is read off the actual iframe rather than off a
 * stubbed marker; a thin recording wrapper captures the props on the way through, because the
 * app-scoped ones (`compileState`, `workspaceLost`) are facts about the project's one app and their
 * whole characterization is that the chat predicate does NOT reach them.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { createElement } from 'react'
import { screen, waitFor, cleanup, fireEvent } from '@testing-library/react'
import {
  FakeEventSource, makeClient, primeClient, primeTurn, renderBuilderAt, withLiveBuildAnchor,
  statusResp, send, scriptBuildTurn, T_PREVIEW, T_BUILD_END, turnStreaming,
  T_DELTA, T_END,
} from './_builderSession.jsx'

/** The three arms, given URLs that cannot be confused with one another. */
const SESSION_URL = 'https://session-app.example.azurecontainerapps.io/'
const TURN_URL = 'https://turn-app.example.azurecontainerapps.io/'
const RELAUNCH_URL = 'https://relaunched-app.example.azurecontainerapps.io/'

const h = vi.hoisted(() => ({
  loadBuilds: vi.fn(), newBuild: vi.fn(), createBuild: vi.fn(), getBuild: vi.fn(),
  deleteBuild: vi.fn(), listProjectConversations: vi.fn(), buildUserParts: vi.fn(),
  startTurn: vi.fn(), readTurnStream: vi.fn(), buildFromPlan: vi.fn(), stopTurn: vi.fn(),
  switchMode: vi.fn(), resolvePlanOptions: vi.fn(),
  start: vi.fn(), relaunchPreview: vi.fn(), stop: vi.fn(), getStatus: vi.fn(), forceEnd: vi.fn(),
  fetchPreviewState: vi.fn(), fetchSaveState: vi.fn(),
}))

/** Every prop bag the pane has been handed, in order. */
const paneProps: Record<string, unknown>[] = []

vi.mock('../../utils/builderHistory', () => ({
  loadBuilds: h.loadBuilds, newBuild: h.newBuild, createBuild: h.createBuild,
  getBuild: h.getBuild, deleteBuild: h.deleteBuild, deriveTitle: (t: string) => (t || '').slice(0, 40),
}))
vi.mock('../../utils/conversationApi', () => ({ listProjectConversations: h.listProjectConversations }))
vi.mock('../../utils/chatHistory', () => ({ relativeTime: () => 'now' }))
vi.mock('../../components/layout/Navbar', () => ({ default: () => null }))
// A RECORDING WRAPPER, not a stub: the real pane still renders (so the iframe and its element
// identity are observable) and the props are captured on the way through (so the app-scoped ones
// are readable without asserting against the pane's cover copy, which is not this file's subject).
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
vi.mock('../../utils/turnStreamApi', async (orig) => ({
  ...(await orig<typeof import('../../utils/turnStreamApi')>()),
  startTurn: (...a: unknown[]) => h.startTurn(...a),
  readTurnStream: (...a: unknown[]) => h.readTurnStream(...a),
  buildFromPlan: (...a: unknown[]) => h.buildFromPlan(...a),
  switchMode: (...a: unknown[]) => h.switchMode(...a),
  resolvePlanOptions: (...a: unknown[]) => h.resolvePlanOptions(...a),
  stopTurn: (...a: unknown[]) => h.stopTurn(...a),
}))
vi.mock('../../utils/buildSessionApi', async (orig) => ({
  ...(await orig<typeof import('../../utils/buildSessionApi')>()),
  fetchPreviewState: (...a: unknown[]) => h.fetchPreviewState(...a),
  fetchSaveState: (...a: unknown[]) => h.fetchSaveState(...a),
}))

const deps = () => {
  const fake = new FakeEventSource('x')
  return { client: makeClient(h), eventSourceFactory: () => fake }
}

const frame = () => document.querySelector('iframe')
const framedUrl = () => frame()?.getAttribute('src') ?? null
/** The newest value the pane was handed for `name` — the app-scoped props read this. */
const lastPaneProp = (name: string) => paneProps[paneProps.length - 1]?.[name]

/** A transcript carrying a finished build, which is what makes the terminal placeholder render. */
const withOutcome = () => ({
  id: 'build-X',
  kind: 'builder',
  messages: [
    { id: 'm0', role: 'user', seq: 0, parts: [{ type: 'text', text: 'a visitor app' }] },
    {
      id: 'm1',
      role: 'assistant',
      seq: 1,
      parts: [
        { type: 'text', text: 'Build finished.' },
        { type: 'build', status: 'ended', sessionId: 's-old', previewUrl: 'https://old.example/' },
      ],
    },
  ],
})

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
  h.createBuild.mockResolvedValue({ ok: true })
  h.buildUserParts.mockImplementation(async (t: string) => [{ type: 'text', text: t }])
  h.relaunchPreview.mockResolvedValue({
    appId: 'a1', previewUrl: RELAUNCH_URL, status: 'ready', restoredFromFailedBuild: false,
  })
  // Neither probe is this file's subject; both are answered so nothing reaches a real `fetch`.
  h.fetchPreviewState.mockResolvedValue({
    state: 'unknown', alive: false, previewUrl: null, occupyingProjectName: null, restorable: null,
  })
  h.fetchSaveState.mockResolvedValue({ dirty: null })
})
afterEach(() => cleanup())

/**
 * Bring up a page whose RELAUNCH arm is live and stamped to `projectId`.
 *
 * `handleRelaunch` stamps `sessionProjectRef` itself, which is what puts the arm in scope — so
 * this is also the cheapest way to make the project predicate TRUE without a live session that
 * would gate the composer.
 */
async function relaunchFramedAt(chatId: string, projectId: string) {
  h.getBuild.mockResolvedValue(withOutcome())
  const view = renderBuilderAt({ chatId, projectId, hasSavedBuild: true, deps: deps() })
  // The TERMINAL placeholder, not merely any Relaunch button: with a saved build confirmed the
  // pre-resolution empty state offers one too, and that node is replaced when the transcript lands.
  await waitFor(() => expect(screen.getAllByText(/no longer running/i).length).toBeGreaterThan(0))
  fireEvent.click(await screen.findByRole('button', { name: /relaunch/i }))
  await waitFor(() => expect(framedUrl()).toBe(RELAUNCH_URL))
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
    h.getBuild.mockResolvedValue(withLiveBuildAnchor('live-7'))
    h.getStatus.mockResolvedValue(
      statusResp({ sessionId: 'live-7', status: 'ended', previewUrl: SESSION_URL }),
    )
    const view = renderBuilderAt({ chatId: 'chat-A', projectId: 'pA', hasSavedBuild: true, deps: deps() })
    await waitFor(() => expect(h.getStatus).toHaveBeenCalledWith('live-7'))

    fireEvent.click(await screen.findByRole('button', { name: /relaunch/i }))

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
    // `compileState` and `workspaceLost` are facts about the PROJECT'S ONE APP, and their producer
    // outlives the turn. They are deliberately ungated by `turnNarrativeIsThisChat`, and blanking
    // them on a chat switch is what leaves an error screen uncovered. They are not address sources
    // and must not follow the address into the resolver.
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
    // AE4's mechanism, at the page level. `LivePreview` pins that a same-key render keeps the node
    // (`LivePreview.test.jsx:143`); what is unproven without this is that the PAGE keeps handing it
    // the same address across an ordinary re-render — the property the shell extraction must not
    // lose, since after it the pane outlives the route entirely.
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
    // AN ORDINARY SEND, not the plan card. This page renders a BUILD chat and every send on one
    // is a build turn, so a send is the shortest honest way to get a build streaming here — and
    // the card is no longer even a route to one from this chat: pressing Build it hands off to a
    // SECOND chat and navigates there, so the turn it starts would never stream into this frame.
    await send('a visitor app')
    // NO `turnId` IN THE SUBSCRIBE, and that is the send path's shape rather than an oversight:
    // a send subscribes to whatever turn its own POST just started on this conversation, so the
    // id is the server's to know. Only a RE-ATTACH names a turn, because it is joining one it
    // did not start.
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

  it('a different project is a different app, so a different address, so a genuine remount', async () => {
    // The other half. Implementing "never unmount" by pinning the key to a constant satisfies the
    // scenario above and leaves a frame pointing at a container that no longer exists, with nothing
    // able to detect it.
    const view = await relaunchFramedAt('chat-A', 'pA')
    const before = frame()

    view.moveTo({ chatId: 'chat-B', projectId: 'pB' })
    await waitFor(() => expect(frame()).toBeNull())

    // Coming back re-acquires the address, and the frame that returns is a NEW element.
    view.moveTo({ chatId: 'chat-A', projectId: 'pA' })
    await waitFor(() => expect(framedUrl()).toBe(RELAUNCH_URL))
    expect(frame()).not.toBe(before)
    view.unmount()
  })
})
