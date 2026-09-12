/**
 * Build-it starts a WRITE TURN, not a build session: the atomic transition flips the chat to
 * Write and starts the turn server-side; the page subscribes with the same `readTurnStream` an
 * ordinary send uses, and `buildFromPlan` hands back only a `turnId`.
 *
 * The transition's refusals are typed HTTP statuses now (429 daily cap, 409 busy, 503
 * unconfigured) — `buildFromPlan` THROWS and the card re-arms with the server's message.
 *
 * The REAL useBuildSession hook + LivePreview run; only the build-session transport and the
 * turn transport are mocked.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act, cleanup, within } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import ConversationSurface from '../../components/chat/ConversationSurface'
import {
  FakeEventSource, PREVIEW_URL, makeClient, primeClient, renderBuilder, statusResp,
  PLAN_CARD_ID, planReply, primeTurn, turnStreaming, send, T_DELTA,
  scriptBuildTurn, BUILD_TURN_ID, T_STEP, T_PREVIEW, T_QUOTA, T_BUILD_END, T_WORKSPACE, T_END,
  T_DIAGNOSTIC,
  inWorkspace,
} from './_builderSession.jsx'

const previewState = (state, restorable = null) => ({
  state,
  alive: state === 'alive',
  previewUrl: state === 'alive' ? PREVIEW_URL : null,
  occupyingProjectName: null,
  occupyingProjectId: null,
  restorable,
})

const h = vi.hoisted(() => ({
  loadBuilds: vi.fn(),
  getBuild: vi.fn(),
  listProjectConversations: vi.fn(),
  buildUserParts: vi.fn(),
  startTurn: vi.fn(),
  readTurnStream: vi.fn(),
  buildFromPlan: vi.fn(),
  stopTurn: vi.fn(),
  resolvePlanOptions: vi.fn(),
  relaunchPreview: vi.fn(),
  stop: vi.fn(),
  getStatus: vi.fn(),
  fetchPreviewState: vi.fn(),
}))

vi.mock('../../utils/builderHistory', () => ({
  loadBuilds: h.loadBuilds,
  getBuild: h.getBuild,
  deriveTitle: (t) => (t || '').slice(0, 40),
}))
// SPREAD THE ORIGINAL: `handleBuildIt` mints the new chat's id via `uuidv7`, and naming only
// `listProjectConversations` would leave the rest of the module undefined too.
vi.mock('../../utils/conversationApi', async (importOriginal) => ({
  ...(await importOriginal()),
  // The send path creates the chat before its first upload; stubbed so no network is reached.
  createConversation: async () => ({ id: 'conv-created' }),
  listProjectConversations: h.listProjectConversations,
}))
// THE WORKSPACE READ: left unmocked, the poll's real fetch fails and the pane reports it could
// not check on the app — wrong for a workspace that has gone to sleep. `relaunchPreview` reaches
// this module directly, so it is mocked here too, not only on the injected client.
vi.mock('../../utils/buildSessionApi', async (orig) => ({
  ...(await orig()),
  fetchPreviewState: (...a) => h.fetchPreviewState(...a),
  relaunchPreview: (...a) => h.relaunchPreview(...a),
}))
vi.mock('../../components/layout/Navbar', () => ({ default: () => null }))
vi.mock('../../utils/attachmentStore', async (orig) => ({ ...(await orig()), buildUserParts: h.buildUserParts }))
// `switchMode` is GONE from this list: the route it posted to no longer exists, and a
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

// BUILD-IT IS A HANDOFF, not a flip: the atomic transition creates a SECOND, new build chat
// seeded with the plan and starts the turn there; the plan chat (`'build-X'`) is left as-is. So
// the build's conversation is THIS id — every assertion about the post-handoff call names it.
const LIVE_CHAT_ID = 'build-X-live'

/**
 * Wire up the handoff's two sides: `buildFromPlan` hands back the live chat's id, and that
 * chat's `getBuild` carries the `activeTurn` its adopt effect reattaches to. Keyed
 * `mockImplementation`, not a blanket `mockResolvedValue` — a blanket answer would hand the
 * live chat's lookup the plan chat's fixture too.
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
 * Send a turn, wait for the plan card, click Build it — the atomic transition creates the live
 * build chat and navigates there, so by the time this resolves the routed chat id has changed.
 */
async function sendPrompt(text = 'build me a tool') {
  await send(text)
  fireEvent.click(await screen.findByRole('button', { name: /^Build this plan$/ }))
  await waitFor(() => expect(h.buildFromPlan).toHaveBeenCalled())
}

/** Confirms the page has genuinely arrived on the build turn's socket — the live chat's own
 *  adopt reattaching to it, not a subscription the press itself opened. */
async function awaitBuildTurn(turnId = BUILD_TURN_ID, liveChatId = LIVE_CHAT_ID) {
  await waitFor(() => expect(h.getBuild).toHaveBeenCalledWith(liveChatId))
  await waitFor(() => expect(h.readTurnStream).toHaveBeenCalledWith(expect.objectContaining({ turnId })))
}

function scriptedBuild(options) {
  const turn = scriptBuildTurn(options)
  h.readTurnStream.mockImplementation(turn.impl)
  return turn
}

/** The consolidating snapshot every subscribe gets FIRST on cursor 0, carrying the `turnId`
 *  `handleStopTurn` reads out of `liveTurnIdRef` — `scriptBuildTurn`'s default `opening` predates
 *  that contract, so a Stop test must supply one itself. Mirrors the same-named helper in
 *  `ConversationSurface-outcome.test.jsx`. */
const T_SNAPSHOT = (turnId, seq = 1) => ({
  type: 'snapshot', seq, turnId, turnStatus: 'running', items: [], parts: [], working: false,
})

beforeEach(() => {
  // ALIVE by default — most scenarios here are a running build with its preview framed; the
  // "come back later" tests below override it for a workspace gone to sleep.
  h.fetchPreviewState.mockResolvedValue(previewState('alive'))
  vi.clearAllMocks()
  Element.prototype.scrollIntoView = vi.fn()
  primeClient(h)
  h.getBuild.mockResolvedValue(null)
  h.loadBuilds.mockResolvedValue([])
  h.listProjectConversations.mockResolvedValue([{ id: 'build-X', kind: 'build', title: 'My build', updatedAt: new Date().toISOString() }])
  h.buildUserParts.mockImplementation(async (text) => [{ type: 'text', text }])
  primeTurn(h)
  primeHandoff()
})
afterEach(() => cleanup())

describe('BuilderPage — the build-turn flow', () => {
  it('Build it starts a WRITE TURN; its step frames render; the preview frame frames the sandbox URL', async () => {
    const turn = scriptedBuild()
    renderBuilder({ deps: deps().deps })
    await sendPrompt()

    // Started via the ATOMIC TRANSITION: a second, new build chat is created and started
    // server-side — the build-session door stays shut, `getStatus` is never called. Third arg is
    // the new chat's client-minted id (a real `uuidv7()` — only its shape is pinned).
    expect(h.buildFromPlan).toHaveBeenCalledWith('build-X', PLAN_CARD_ID, expect.any(String))
    expect(h.getStatus).not.toHaveBeenCalled()
    // Cursor 0 deliberately: the build may have run for seconds before this subscribe landed, and
    // the snapshot recovers missed frames. The conversation is the LIVE build chat, not the plan chat.
    await waitFor(() =>
      expect(h.readTurnStream).toHaveBeenCalledWith(
        expect.objectContaining({ conversationId: LIVE_CHAT_ID, turnId: BUILD_TURN_ID, cursor: 0 }),
      ),
    )

    await turn.frame(T_STEP('Scaffolding your app…'), T_STEP('Installing dependencies', { id: 'call-2', seq: 3 }))
    // THE GROUP KEEPS EVERY STEP rather than replacing one row, collapsed to a count.
    const group = await screen.findByTestId('activity-group')
    await waitFor(() => expect(group.textContent).toMatch(/Installing dependencies/i))
    expect(within(group).getByTestId('activity-glyphs').children).toHaveLength(2)
    expect(screen.queryByTestId('activity-group-rows')).toBeNull()
    fireEvent.click(within(group).getByTestId('activity-group-trigger'))
    const rows = within(await screen.findByTestId('activity-group-rows'))
    expect(rows.getByText(/Scaffolding your app/i)).toBeTruthy()

    await turn.frame(T_PREVIEW())
    await waitFor(() => expect(document.querySelector('iframe')?.getAttribute('src')).toBe(PREVIEW_URL))
  })

  it('a doubly-truncated turn resubscribes once, then surfaces the connection-dropped notice', async () => {
    const { deps: sessionDeps } = deps()
    h.readTurnStream.mockImplementation(async ({ onFrame }) => {
      onFrame(T_DELTA('partial…'))
      return 'truncated'
    })
    renderBuilder({ deps: sessionDeps })
    await send('just answer me')

    expect(await screen.findByText(/connection dropped\. reload to catch up/i)).toBeTruthy()
    expect(h.readTurnStream).toHaveBeenCalledTimes(2) // one resume-once, then the honest error
  })

  it('Stop → the turn ends and the running app stays framed', async () => {
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
    // this is the same `stopTurn` an ordinary reply uses, addressed by the live chat + turn id.
    // `stop-turn` is the composer control; the build card's own Stop button is still mounted
    // beside it for now and does the same thing, but this is the one that survives its deletion.
    fireEvent.click(screen.getByTestId('stop-turn'))
    await waitFor(() => expect(h.stopTurn).toHaveBeenCalledWith(LIVE_CHAT_ID, BUILD_TURN_ID))
    expect(h.stop).not.toHaveBeenCalled() // never the build-session stop

    // THE CONTAINER IS NOT TORN DOWN BY A STOP: `finish_turn_sandbox` pardons it with no branch
    // on how the turn ended, and the stopped arm is reached precisely because Stop arrives as a
    // cancellation into that `finally`. So the assertions below reject any path that turns a
    // citizen's own interruption into "your app is gone" — held by element IDENTITY rather than
    // by the src matching, because the two are not the same claim: a remount re-requests the
    // document and throws away everything the citizen had typed into their app, while reporting
    // the same URL either way.
    const framed = document.querySelector('iframe')
    await turn.frame(T_BUILD_END({ status: 'stopped', reason: 'stopped_by_user' }))
    await turn.end()
    await waitFor(() => expect(h.stopTurn).toHaveBeenCalled())

    expect(document.querySelector('iframe')).toBe(framed)
    expect(screen.queryByText(/no longer running/i)).toBeNull()
    expect(screen.queryByTestId('preview-ended-card')).toBeNull()
  })

  it('a COMPLETED build keeps the preview framed — never "no longer running"', async () => {
    const turn = scriptedBuild()
    renderBuilder({ deps: deps().deps })
    await sendPrompt()
    await awaitBuildTurn()
    await turn.frame(T_PREVIEW())
    await waitFor(() => expect(document.querySelector('iframe')).toBeTruthy())

    const framed = document.querySelector('iframe')
    await turn.frame(T_BUILD_END({ status: 'completed' }))
    await turn.end()
    // The server PARDONS a completed build's container (idle lease), so the frame stays live.
    // "The turn is over" must never be flattened into "the app is gone" while the URL the user is
    // looking at still serves — the chip that used to state a build outcome over the framed app's
    // own navigation is deleted; liveness is now carried by the frame itself surviving, held by
    // element identity so a remount cannot pass as continuity.
    await waitFor(() => expect(screen.queryByText(/no longer running/i)).toBeNull())
    expect(document.querySelector('iframe')).toBe(framed)
    expect(document.querySelector('iframe')?.getAttribute('src')).toBe(PREVIEW_URL)
    expect(screen.queryByText(/your app is live below/i)).toBeNull()
  })

  // ('Force-end → the kill switch confirms, then ends the session' is RETIRED.) It drove
  // `session.forceEnd`, the kill switch that tore a build SESSION's sandbox down out of band —
  // and the composer-initiated build path has no session to tear down, nor a turn-level
  // equivalent of one. `stopTurn` is the whole interrupt vocabulary a build turn has, and the Stop
  // test above is what pins it. The kill switch is gone everywhere now, not just from this
  // surface: the client, the hook wrapper and the backend route were deleted together, once no UI
  // call site was left anywhere — the block banner took its Force-end button with it.

  it('a self-heal diagnostic renders as a RETRY mid-build, and leaves no residue after completion', async () => {
    const turn = scriptedBuild()
    renderBuilder({ deps: deps().deps })
    await sendPrompt()
    await awaitBuildTurn()

    await turn.frame(T_STEP('Scaffolding your app…'), T_DIAGNOSTIC('Type error in app/page.tsx'))

    // RE-POINTED, NOT GONE: a diagnostic is a failed row inside the activity group, so the
    // group's own label still carries it after the block that used to show it is gone.
    const group = await screen.findByTestId('activity-group')
    // ONE FAILED GLYPH, group still RUNNING. SCOPED TO THE GLYPH STRIP, not the group: expanded
    // rows carry their own sr-only "failed" too, so an unscoped count answers 2 for one problem.
    const glyphs = () => within(within(screen.getByTestId('activity-group')).getByTestId('activity-glyphs'))
    await waitFor(() => expect(glyphs().getAllByText(/^failed$/i).length).toBe(1))

    // The compiler's title is built FOR THE MODEL and never reaches the screen — not merely
    // unrendered: `convertPart` never copies it into a part, so nothing is in the DOM to leak.
    expect(screen.queryAllByText(/Type error in app\/page\.tsx/i)).toHaveLength(0)
    expect(group.querySelector('pre')).toBeNull()

    // Liveness for those two absences: the citizen-facing half DID render, inside the group.
    fireEvent.click(within(group).getByTestId('activity-group-trigger'))
    const rows = within(await screen.findByTestId('activity-group-rows'))
    expect(rows.getByText(/We hit a problem finishing that change\./i)).toBeTruthy()

    // The repair succeeds and completes; the problem STAYS on the record — a finished build must
    // never look as though nothing had gone wrong.
    await turn.frame(T_BUILD_END())
    await turn.end()
    expect(glyphs().getAllByText(/^failed$/i).length).toBe(1)
  })

  it('repair exhausted → turn_ended(failed) — the retry framing does NOT persist beside the terminal', async () => {
    const turn = scriptedBuild()
    renderBuilder({ deps: deps().deps })
    await sendPrompt()
    await awaitBuildTurn()

    await turn.frame(T_DIAGNOSTIC('Type error in app/page.tsx'))
    const group = await screen.findByTestId('activity-group')
    const glyphs = () => within(within(screen.getByTestId('activity-group')).getByTestId('activity-glyphs'))
    await waitFor(() => expect(glyphs().getAllByText(/^failed$/i).length).toBe(1))
    void group

    await turn.frame(T_BUILD_END({ status: 'failed' }))
    await turn.end()
    // The group's count is the single failure record at a failed terminal now; no second block
    // (the old card's amber retry text, which used to contradict it) may join it.
    await waitFor(() => expect(screen.queryByText(/trying another way/i)).toBeNull())
    expect(glyphs().getAllByText(/^failed$/i).length).toBe(1)
  })

  it('a quota breach ends gracefully and shows the daily-limit banner', async () => {
    // The resolver's PROJECT arm on the chat route outranks
    // `transcriptHasBuildOutcome ? 'ended'`. The shared fixture answers `alive` for EVERY
    // project id as scenery; under it the pane now correctly frames the serving container
    // instead of saying the preview is gone. This test is about the banner, not about a
    // live container, so it says so.
    h.fetchPreviewState.mockResolvedValue(previewState('unknown'))
    const turn = scriptedBuild()
    renderBuilder({ deps: deps().deps })
    await sendPrompt()
    await awaitBuildTurn()

    await turn.frame(T_QUOTA(), T_BUILD_END({ status: 'failed', reason: 'quota_exceeded' }))
    await turn.end()
    // The cap is a fact about SENDING, so it's stated where sending happens: the composer's gate
    // note names when it works again, and Send carries the same sentence in its accessible name.
    await waitFor(() =>
      expect(screen.getByTestId('composer-gate-note').textContent).toMatch(/you can send again after/i),
    )
    expect(
      screen.getByRole('button', { name: /Send message — You can send again after/i }),
    ).toBeTruthy()
    // …and the composer is NOT disabled by it: a citizen refused mid-thought keeps the
    // text they typed, and can select and copy it out.
    expect(screen.getByTestId('composer-input').hasAttribute('disabled')).toBe(false)
    // ★ THE LIVENESS HANDLE MOVED WITH THE CARD IT USED TO READ. This asserted `/no longer
    // running/i`, which was `LivePreview`'s terminal placeholder — one of four workspace verdicts
    // that component authored beside the workspace map's own. Both are deleted: the map computes
    // ONE state for the whole workspace and `AppPane` draws it, and this reading (`unknown`, with
    // nothing ever decided) reaches the map's internal read-failure arm.
    //
    // WHAT THE ASSERTION IS FOR is unchanged — this scenario is about the BANNER, and the pane is
    // only here to prove the surface rendered a whole screen rather than half of one. So it now
    // reads the state the pane actually reached, which is a stronger handle than a sentence:
    // `data-workspace-state` is the internal name and is exempt from every copy change.
    // ★ THE LIVENESS HANDLE MOVED WITH THE CARD IT USED TO READ. This asserted `/no longer
    // running/i`, which was `LivePreview`'s terminal placeholder — one of four workspace verdicts
    // that component authored beside the workspace map's own. It is deleted: the map computes ONE
    // state for the whole workspace and `AppPane` draws it, and a pane with a terminal address and
    // nothing serving now collapses to an EMPTY pane rather than to a sentence.
    //
    // WHAT THE ASSERTION IS FOR IS UNCHANGED — this scenario is about the BANNER, and the pane is
    // here only to prove the surface rendered a whole screen rather than half of one. So it reads
    // the two facts that survive the deletion: the pane is in the tree, and the retired verdict is
    // not being drawn anywhere on it.
    const pane = document.querySelector('[data-testid="app-pane-region"]')
    expect(pane).not.toBeNull()
    expect(pane?.textContent ?? '').not.toMatch(/no longer running/i)
  })
})

describe('BuilderPage — the transition\'s refusals are typed HTTP statuses now', () => {
  it('a busy workspace RE-ARMS the card with the server\'s own message — no turn started', async () => {
    // `build_failed` + `reason` is gone from the response: every case it carried is now a status
    // the fetch layer raises on (429/409/503), so `buildFromPlan` THROWS and the catch arm puts
    // the server's sentence on the card — the old 200-with-a-reason looked just like a quota refusal.
    h.buildFromPlan.mockRejectedValue(new Error('Another build is already running for your workspace.'))
    // The resolver's PROJECT arm on the chat route outranks
    // `transcriptHasBuildOutcome ? 'ended'`. The shared fixture answers `alive` for EVERY
    // project id as scenery; under it the pane now correctly frames the serving container
    // instead of saying the preview is gone. This test is about the banner, not about a
    // live container, so it says so.
    h.fetchPreviewState.mockResolvedValue(previewState('unknown'))
    renderBuilder({ deps: deps().deps })
    await sendPrompt()

    expect(await screen.findByText(/another build is already running/i)).toBeTruthy()
    expect(screen.getByRole('button', { name: /^Build this plan$/ })).toBeTruthy()
    expect(document.querySelector('iframe')).toBeNull()
    expect(h.readTurnStream).not.toHaveBeenCalledWith(expect.objectContaining({ turnId: BUILD_TURN_ID }))
    expect(h.getStatus).not.toHaveBeenCalled() // and no client-side 409 dance any more
  })

  it('an already_started outcome (double click / second tab) JOINS the running turn', async () => {
    // `already_started` still names the live chat: a double press mints the same client id, so
    // the server answers with the existing chat rather than a second one.
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

describe('BuilderPage — ONE gate: the composer is shut while the agent works', () => {
  it('a send is REFUSED while the build runs — the composer is disabled and the build is untouched', async () => {
    // The decision this pins: a build is not a parallel track you talk over — the tool calls the
    // agent makes ARE its answer, so while it works the composer says there is nothing to send.
    const turn = scriptedBuild()
    renderBuilder({ deps: deps().deps })
    await sendPrompt('first build')
    await awaitBuildTurn()
    await turn.frame(T_PREVIEW())
    await waitFor(() => expect(document.querySelector('iframe')).toBeTruthy())

    h.buildFromPlan.mockClear()
    h.stop.mockClear()
    h.startTurn.mockClear()

    // SENDING is what waits — not typing. The text box and attach stay live so the
    // citizen can compose their next message while they watch, and the note says why send is off.
    const textarea = screen.getByPlaceholderText(/ask for another change/i)
    expect(textarea.disabled).toBe(false)
    expect(screen.getByTitle(/Attach images/i).disabled).toBe(false)
    expect(screen.getByTestId('composer-gate-note').textContent).toMatch(/send unlocks when it is done/i)

    // ENFORCED, not merely rendered: `aria-disabled` is affordance only, so Enter must be refused
    // by `handleSend`. The refusal's wording is the GENERIC one, not "your app is being built" —
    // that branch only fires for a LEGACY session reattach now. "Building your app…" still
    // appears, but only for the click-time round-trip, pinned in `ConversationSurface-composer.test.jsx`.
    fireEvent.change(textarea, { target: { value: 'make it dark mode' } })
    fireEvent.keyDown(textarea, { key: 'Enter' })
    expect(await screen.findByText(/send unlocks when it is done/i)).toBeTruthy()
    expect(h.startTurn).not.toHaveBeenCalled()
    expect(h.stop).not.toHaveBeenCalled()
    expect(h.buildFromPlan).not.toHaveBeenCalled()
    expect(document.querySelector('iframe')).toBeTruthy() // the live build is untouched

    // No second Build-it to click while the build runs either: the card that started it is
    // resolved, so the "build over a still-live session" hazard is unreachable from this chat.
    expect(screen.queryByRole('button', { name: /^Build this plan$/ })).toBeNull()
  })

  it('the composer RE-OPENS at the terminal, and the send is then a CHAT turn (the routing rule)', async () => {
    // The other half of the one gate: the wait ends by itself. When the build finishes, a send is
    // a question to the assistant — it never touches the build (every mode accepts a send now).
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
    expect(screen.getByPlaceholderText(/ask for another change/i).disabled).toBe(false)
    expect(screen.getByTitle(/Attach images/i).disabled).toBe(false)

    h.buildFromPlan.mockClear()
    h.stop.mockClear()
    h.startTurn.mockClear()
    h.readTurnStream.mockImplementation(turnStreaming(planReply('Build it, but dark.', 'opt-2')))
    await send('make it dark mode')

    await waitFor(() => expect(h.startTurn).toHaveBeenCalled())
    expect(h.stop).not.toHaveBeenCalled()
    expect(h.buildFromPlan).not.toHaveBeenCalled()
  })

  it('AN INERTNESS GUARD: no mode control appears at the terminal, and a stray legacy `mode` field is ignored', async () => {
    // Restated as an absence: no build terminal ever renders a mode control, and a `getBuild` row
    // still carrying a legacy `mode` field is simply ignored rather than read.
    const turn = scriptedBuild()
    renderBuilder({ deps: deps().deps })
    await sendPrompt('first build')
    await awaitBuildTurn()
    // LIVENESS FIRST: the page is genuinely on the live build, not merely missing a mode control
    // because it rendered nothing — the stop control's presence proves that.
    expect(screen.getByTestId('stop-turn')).toBeTruthy()
    expect(screen.queryByRole('button', { name: /^Mode:/ })).toBeNull()

    // A row with a stray legacy `mode` — if a re-read were reintroduced, this is what it would see.
    h.getBuild.mockResolvedValue({ id: LIVE_CHAT_ID, kind: 'build', messages: [] })
    await turn.frame(T_BUILD_END())
    await turn.end()

    await waitFor(() => expect(screen.queryByTestId('composer-gate-note')).toBeNull())
    expect(screen.getByPlaceholderText(/ask for another change/i).disabled).toBe(false)
    expect(screen.queryByRole('button', { name: /^Mode:/ })).toBeNull()
  })

  it('confirming the next brief starts a fresh build — nothing live to stop, so never a self-inflicted 409', async () => {
    // The old flow had to STOP the running session before starting the replacement; under one
    // gate the previous build is already terminal by the time a brief can be asked for.
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

    // The page is ALREADY on the live build chat from the first handoff (Build-it navigated there),
    // so this second confirm names THAT chat, not the original plan chat.
    await waitFor(() =>
      expect(h.buildFromPlan).toHaveBeenCalledWith(LIVE_CHAT_ID, 'opt-2', expect.any(String)),
    )
    expect(h.stop).not.toHaveBeenCalled()
  })

  it('a RELOAD mid-build re-takes the gate from the transcript', async () => {
    // The window the gate mattered most in, and was simply ABSENT from: `buildActive` derives
    // from refs only `Build it` stamps, so a fresh mount over a RUNNING build rendered an open
    // textarea and no note. The projection carries the session id on the `build_in_progress` part.
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
    const textarea = await screen.findByPlaceholderText(/ask for another change/i)
    await waitFor(() => expect(screen.getByTestId('composer-gate-note').textContent).toMatch(/send unlocks/i))
    expect(textarea.disabled).toBe(false)
    expect(screen.getByTitle(/Attach images/i).disabled).toBe(false)
    // AN INERTNESS GUARD, not a frozen-pill assertion: there is no mode pill to freeze or thaw,
    // mid-build reload or otherwise.
    expect(screen.queryByRole('button', { name: /^Mode:/ })).toBeNull()
    // The transcript stops lying in the past tense too: `build_in_progress` maps to no rendered
    // part at all (see `convertMessage`), so the anchor sentence cannot appear either way.
    expect(document.querySelector('[data-kind="build-in-progress"]')).toBeNull()
    expect(screen.getByTestId('stop-turn')).toBeTruthy()

    fireEvent.change(textarea, { target: { value: 'while you are at it, add a chart' } })
    fireEvent.keyDown(textarea, { key: 'Enter' })
    expect((await screen.findByTestId('composer-gate-note')).textContent).toMatch(/building your app/i)
    expect(h.startTurn).not.toHaveBeenCalled()
    // …and the typed text SURVIVES the refusal, which is the point of keeping the box live.
    expect(textarea.value).toBe('while you are at it, add a chart')
  })

  it('the composer shuts on the CLICK, not on the server\'s answer', async () => {
    // `buildFromPlan` is a full round-trip, seconds long. The composer used to stay open for all
    // of it; a send in that window hit the silent double-Enter ref guard, and the message was gone.
    let answer = () => {}
    h.buildFromPlan.mockImplementation(
      () => new Promise((resolve) => { answer = () => resolve({ outcome: 'started', chatId: LIVE_CHAT_ID, turnId: BUILD_TURN_ID }) }),
    )
    const turn = scriptedBuild()
    renderBuilder({ deps: deps().deps })
    await send('a visitor app')
    fireEvent.click(await screen.findByRole('button', { name: /^Build this plan$/ }))

    const textarea = screen.getByPlaceholderText(/ask for another change/i)
    await waitFor(() => expect(screen.getByTestId('composer-gate-note').textContent).toMatch(/send unlocks/i))

    h.startTurn.mockClear()
    fireEvent.change(textarea, { target: { value: 'and make it dark' } })
    fireEvent.keyDown(textarea, { key: 'Enter' })
    expect((await screen.findByTestId('composer-gate-note')).textContent).toMatch(/building your app/i)
    expect(h.startTurn).not.toHaveBeenCalled() // refused OUT LOUD, never silently dropped

    await act(async () => { answer(); await Promise.resolve() })
    await awaitBuildTurn()
    await turn.frame(T_STEP('Scaffolding your app…'))
    expect(screen.getByTestId('composer-gate-note')).toBeTruthy()
  })

  it('a SIBLING chat in the same project keeps its composer OPEN — the gate is per-chat, like the server\'s', async () => {
    // The server's build gate is per-CONVERSATION. A gate not scoped to the chat that started the
    // build over-shoots it: the sibling's send would go dead over a turn the server would accept.
    // `generatingChatId` records WHICH chat is mid-turn, not merely that one is.
    // BUILD-IT IS A HANDOFF: pressing it navigates to a second, new build chat — simulated here,
    // as in every sibling-chat guard in this file, by a `chatId` prop swap on the same instance.
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
        <Routes>{inWorkspace(<Route path="*" element=<ConversationSurface chatId="chat-A" projectId="pA" projectName="Project A" buildSessionDeps={sessionDeps} /> />)}</Routes>
      </MemoryRouter>,
    )
    await screen.findByPlaceholderText(/ask for another change/i)
    await send('build A')
    fireEvent.click(await screen.findByRole('button', { name: /^Build this plan$/ }))
    await waitFor(() =>
      expect(h.buildFromPlan).toHaveBeenCalledWith('chat-A', PLAN_CARD_ID, expect.any(String)),
    )

    rerender(
      <MemoryRouter initialEntries={['/x']}>
        <ConversationSurface chatId={CHAT_A_LIVE} projectId="pA" projectName="Project A" buildSessionDeps={sessionDeps} />
      </MemoryRouter>,
    )
    await awaitBuildTurn(BUILD_TURN_ID, CHAT_A_LIVE)
    await waitFor(() => expect(screen.getByTestId('composer-gate-note')).toBeTruthy())

    // The SAME instance moves to a sibling chat of the same project (flat routing); A's build
    // keeps running server-side either way.
    h.readTurnStream.mockImplementation(turnStreaming(planReply('A sibling plan.', 'opt-S')))
    rerender(
      <MemoryRouter initialEntries={['/x']}>
        <Routes>{inWorkspace(<Route path="*" element=<ConversationSurface chatId="chat-B" projectId="pA" projectName="Project A" buildSessionDeps={sessionDeps} /> />)}</Routes>
      </MemoryRouter>,
    )
    await waitFor(() => expect(h.getBuild).toHaveBeenCalledWith('chat-B'))
    const sibling = await screen.findByPlaceholderText(/ask for another change/i)
    await waitFor(() => expect(screen.queryByTestId('composer-gate-note')).toBeNull())
    expect(sibling.disabled).toBe(false)

    // …and the send genuinely goes out, rather than being refused on A's behalf.
    h.startTurn.mockClear()
    h.stop.mockClear()
    await send('what does this app do?')
    await waitFor(() => expect(h.startTurn).toHaveBeenCalled())
    expect(h.stop).not.toHaveBeenCalled()
  })

  it('a Send in a DIFFERENT project does NOT tear down another project\'s live build', async () => {
    // One BuilderPage instance persists across project switches. A live build in project A must
    // survive a Send made from project B's chat — the bug was a tautological refine guard that
    // stopped A's build instead of refusing B. A second build anywhere is now a 409 from the
    // server; `buildFromPlan` throws it, but A's build must NOT be stopped to make room for B's.
    // Simulated, as above, by a `chatId` prop swap on the same instance.
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
        <Routes>{inWorkspace(<Route path="*" element=<ConversationSurface chatId="chat-A" projectId="pA" projectName="Project A" buildSessionDeps={sessionDeps} /> />)}</Routes>
      </MemoryRouter>,
    )
    // Build + frame a preview in project A.
    const ta = await screen.findByPlaceholderText(/ask for another change/i)
    fireEvent.change(ta, { target: { value: 'build A' } })
    fireEvent.keyDown(ta, { key: 'Enter' })
    fireEvent.click(await screen.findByRole('button', { name: /^Build this plan$/ }))
    await waitFor(() =>
      expect(h.buildFromPlan).toHaveBeenCalledWith('chat-A', PLAN_CARD_ID, expect.any(String)),
    )
    // `inWorkspace`, like every mount here: the app pane is a SIBLING of the router outlet, so a
    // bare `<ConversationSurface>` has no host to frame the preview into.
    rerender(
      <MemoryRouter initialEntries={['/x']}>
        <Routes>{inWorkspace(<Route path="*" element=<ConversationSurface chatId={CHAT_A_LIVE} projectId="pA" projectName="Project A" buildSessionDeps={sessionDeps} /> />)}</Routes>
      </MemoryRouter>,
    )
    await awaitBuildTurn(BUILD_TURN_ID, CHAT_A_LIVE)
    await turn.frame(T_PREVIEW())
    await waitFor(() => expect(document.querySelector('iframe')).toBeTruthy())

    // Navigate the SAME instance to project B's builder chat.
    // Project B must answer for ITSELF. The blanket `alive` fixture would have B's
    // pane frame project A's container — the cross-project frame the resolver's project
    // label exists to prevent — so each id now answers its own truth.
    h.fetchPreviewState.mockImplementation(async (id) =>
      previewState(id === 'pA' ? 'alive' : 'unknown'),
    )
    h.buildFromPlan.mockClear()
    h.stop.mockClear()
    h.readTurnStream.mockImplementation(turnStreaming(planReply('Build B, please.', 'opt-B')))
    rerender(
      <MemoryRouter initialEntries={['/x']}>
        <Routes>{inWorkspace(<Route path="*" element=<ConversationSurface chatId="chat-B" projectId="pB" projectName="Project B" buildSessionDeps={sessionDeps} /> />)}</Routes>
      </MemoryRouter>,
    )
    await waitFor(() => expect(h.getBuild).toHaveBeenCalledWith('chat-B'))
    expect(document.querySelector('iframe')).toBeNull() // A's build is NOT shown under project B

    // Confirm a brief in project B → refused WITHOUT stopping or restarting A's build.
    h.buildFromPlan.mockRejectedValue(new Error('You already have a build running in another project.'))
    const tb = await screen.findByPlaceholderText(/ask for another change/i)
    fireEvent.change(tb, { target: { value: 'build B' } })
    fireEvent.keyDown(tb, { key: 'Enter' })
    fireEvent.click(await screen.findByRole('button', { name: /^Build this plan$/ }))
    // The refusal is surfaced ON the card, where the click was, and the card re-arms as a retry.
    expect(await screen.findByText(/running in another project/i)).toBeTruthy()
    expect(h.stop).not.toHaveBeenCalled()
    const retry = screen.getByRole('button', { name: /^Build this plan$/ })
    expect(retry.disabled).toBe(false)
  })
})

describe('BuilderPage — the "come back later" relaunch entry point', () => {
  // A reload drops the in-memory session, but the transcript's persisted BuildOutcome part proves
  // a build once ran — so a fresh mount must render the terminal placeholder, not the idle empty
  // state. The live/reattach flow always wins: this fallback only fires with no session at all.
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

  it('a fresh mount with a persisted outcome and no live session offers the way back; pressing it starts the app', async () => {
    // The journey is unchanged: reload, find a way back to the app, press it, watch the restored
    // preview frame. What moved is the CONTROL — exactly one control starts the app now
    // (`Launch Application`), drawn by `AppPane` itself rather than nested inside the terminal card.
    h.fetchPreviewState.mockResolvedValue(previewState('asleep', true))
    h.getBuild.mockResolvedValue(outcomeTranscript())
    h.relaunchPreview.mockResolvedValue({
      appId: 'a1', previewUrl: PREVIEW_URL, status: 'ready', restoredFromFailedBuild: false, ready: true,
    })
    const { deps: sessionDeps } = deps()
    // The affordance needs the PROJECT's confirmed saved build — an outcome in the transcript
    // alone proves a build ran, not that a Save happened.
    renderBuilder({ deps: sessionDeps, hasSavedBuild: true })

    const button = await screen.findByRole('button', { name: /launch application/i })
    // Without this the fixture keeps answering `asleep` after a successful start, and the pane is
    // right to refuse framing a container the platform still calls stopped.
    h.fetchPreviewState.mockResolvedValue(previewState('alive'))
    fireEvent.click(button)
    await waitFor(() => expect(h.relaunchPreview).toHaveBeenCalledWith({ projectId: 'p1' }))
    // The restored preview frames — the half an inertness assertion could never see.
    await waitFor(() => expect(document.querySelector('iframe')?.getAttribute('src')).toBe(PREVIEW_URL))
  })

  it('INERTNESS GUARD: a FAILED newest outcome no longer gets its own button label', async () => {
    // There is one control now, saying the same thing however the last build ended — but
    // `restoredFromFailedBuild` still travels to the pane and says "this is your last SAVED
    // version" in a sentence instead. Paired with a liveness assertion: an absence alone would
    // pass on a pane offering no way back at all.
    h.fetchPreviewState.mockResolvedValue(previewState('asleep', true))
    h.getBuild.mockResolvedValue(outcomeTranscript('failed'))
    const { deps: sessionDeps } = deps()
    renderBuilder({ deps: sessionDeps, hasSavedBuild: true })

    // LIVENESS: there is still exactly one way back.
    expect(await screen.findByRole('button', { name: /launch application/i })).toBeTruthy()
    // INERTNESS: neither retired label survives, under any spelling.
    expect(screen.queryByRole('button', { name: /relaunch/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /bring it back/i })).toBeNull()
  })

  // `AppPane` mounts `NoFrame` whenever the address resolver has no URL — this test overrides the
  // default `alive` fixture to `never_built` so the pane's own empty-state sentence renders.
  it('a fresh mount with NO outcome keeps the idle empty state — nothing to relaunch', async () => {
    h.fetchPreviewState.mockResolvedValue(previewState('never_built', false))
    h.getBuild.mockResolvedValue(null)
    const { deps: sessionDeps } = deps()
    const { container } = renderBuilder({ deps: sessionDeps })
    await screen.findByPlaceholderText(/ask for another change/i)
    await waitFor(() => expect(container.textContent).toMatch(/describe what you want to build/i))
    // The half that has not changed: no phantom way back for a project that has never been built.
    expect(screen.queryByRole('button', { name: /relaunch/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /launch application/i })).toBeNull()
  })
})

// AN INERTNESS GUARD: there is no mode to fail switching INTO — a chat's kind is fixed when it is
// created, so a mode-switch failure can no longer occur, idle or mid-build.
describe('a failed mode switch says what actually failed — RETIRED, now an inertness guard', () => {
  it('no mode pill exists idle, and ⌥P opens no menu — the whole surface this suite exercised is gone', async () => {
    h.getBuild.mockResolvedValue({ id: 'build-X', kind: 'build', messages: [] })
    const { deps: sessionDeps } = deps()
    renderBuilder({ deps: sessionDeps })

    // LIVENESS FIRST: an absent pill also describes a component that threw and rendered nothing.
    await screen.findByPlaceholderText(/ask for another change/i)
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
    expect(screen.getByTestId('stop-turn')).toBeTruthy()
    expect(screen.queryByRole('button', { name: /^Mode: /i })).toBeNull()
    fireEvent.keyDown(document, { code: 'KeyP', altKey: true })
    expect(screen.queryByRole('menuitemradio')).toBeNull()
    void turn // scripted but not driven further — the build's own progress isn't this test's claim
  })
})

// A read turn attaches the very same container a build does, so it emits the very same
// `workspace` frame; this page always renders a build chat, so `isBuild` is hardcoded `true` here.
// NOT COVERED: the `isBuild: false` arm of `narrativeStatus` (`turnNarrative.ts`) has no direct
// unit test anywhere in the repo — out of this file's scope.
describe('a read turn reads the live container without becoming a build', () => {
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

  it('says a reply is coming for the whole container wait, with no phase headline', async () => {
    // NO PHASE HEADLINE — the screen should read as an app being built, not as an agent being
    // watched. It must still not go silent: the composer says a reply is coming for the whole wait.
    h.getBuild.mockResolvedValue({ id: 'build-X', kind: 'build', messages: [] })
    const turn = scriptReadTurn()
    renderBuilder({ deps: deps().deps })
    await send('What is the heading text on the page right now? One line.')

    await waitFor(() =>
      expect(screen.getByTestId('composer-gate-note').textContent).toMatch(/send unlocks when it is done/i),
    )
    // …and it is stoppable for all of it, which the wait's own headline never made it.
    expect(screen.getByTestId('stop-turn')).toBeTruthy()
    // NO PHASE NARRATION IN THE CHAT — scoped to the panel deliberately: the APP PANE narrates the workspace phase instead.
    const chat = within(screen.getByTestId('chat-panel'))
    expect(chat.queryByText(/Setting up your sandbox/i)).toBeNull()
    expect(chat.queryByText(/Building your app…/i)).toBeNull()

    await turn.frame(T_WORKSPACE('ready', 2))
    expect(screen.getByTestId('composer-gate-note').textContent).toMatch(/send unlocks when it is done/i)
    expect(chat.queryByText(/Setting up your sandbox/i)).toBeNull()
  })

  // Both terminals: `turn_ended` is what makes the difference between "still thinking" and a
  // settled turn — exactly when the empty bubble would appear, since nothing is left to say.
  for (const status of ['completed', 'failed']) {
    it(`leaves no empty bubble behind once a ${status} answer has landed`, async () => {
      h.getBuild.mockResolvedValue({ id: 'build-X', kind: 'build', messages: [] })
      const turn = scriptReadTurn()
      renderBuilder({ deps: deps().deps })
      await send('What is the heading text on the page right now? One line.')
      await screen.findByTestId('stop-turn')

      await turn.frame(
        T_WORKSPACE('ready', 2),
        T_DELTA('It says "Gate Cleaning Log — T1".', 3),
        T_END(status),
      )
      await turn.end()

      expect(await screen.findByText(/Gate Cleaning Log — T1/)).toBeTruthy()
      // AN INERTNESS GUARD: `build-bubble` is the retired per-row avatar wrapped around an empty
      // answer; it must never appear for any turn state. Paired with the liveness assertion
      // above, so this cannot pass against a transcript that rendered nothing at all.
      expect(screen.queryByTestId('build-bubble')).toBeNull()
      expect(screen.queryByTestId('activity-group')).toBeNull() // a read turn ran no tools
    })
  }
})

// The other half of the same emptiness rule: a WRITE turn whose container never came up is
// terminal with no steps and no headline, so the transcript has no activity to draw.
describe('a build that dies before its first step shows no empty bubble', () => {
  it('renders the failure, not an empty assistant bubble', async () => {
    const turn = scriptedBuild({ opening: [T_WORKSPACE('unavailable', 1, 'The workspace service is not available right now.')] })
    renderBuilder({ deps: deps().deps })
    await sendPrompt()
    await awaitBuildTurn()
    await turn.frame(T_BUILD_END({ status: 'failed' }))
    await turn.end('failed')

    await waitFor(() => expect(screen.queryByTestId('build-bubble')).toBeNull())
    // LIVENESS for that absence: the surface is alive and the composer is back.
    expect(screen.getByTestId('composer-input')).toBeTruthy()
  })
})

// The platform's own sentences about the workspace, and the slot they share.
describe('what the platform says about the workspace itself', () => {
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

  // THE BANNER IS THE ONLY PLACE THIS SENTENCE CAN LIVE: putting an app back takes tens of
  // seconds, and a bubble that scrolls away takes "send your message again" with it.
  it('shows a recovery sentence above the composer, not in the transcript', async () => {
    h.getBuild.mockResolvedValue({ id: 'build-X', kind: 'build', messages: [] })
    const turn = scriptReadTurn()
    renderBuilder({ deps: deps().deps })
    await send('Add a column for the gate number.')
    // `resetTurnNarrative` clears the banner at the start of every turn, so a notice framed before
    // that lands would be wiped by setup, not by this test — wait on the composer's stop instead.
    await screen.findByTestId('stop-turn')

    await turn.frame(T_WORKSPACE('preparing', 2, null, 'Your workspace had been reset, so we are putting your app back.'))

    const banner = await screen.findByTestId('turn-banner')
    expect(banner.textContent).toMatch(/putting your app back/i)
  })

  // The ordinary phase machine ticks `preparing` -> `ready` on EVERY turn with no message; reading
  // those as platform speech would post an empty banner every time, and wipe a still-true notice.
  it('is not posted or cleared by the ordinary lifecycle frames', async () => {
    h.getBuild.mockResolvedValue({ id: 'build-X', kind: 'build', messages: [] })
    const turn = scriptReadTurn()
    renderBuilder({ deps: deps().deps })
    await send('Add a column for the gate number.')
    await screen.findByTestId('stop-turn')

    expect(screen.queryByTestId('turn-banner')).toBeNull()

    await turn.frame(T_WORKSPACE('preparing', 2, null, 'We could not check whether your workspace is intact.'))
    await screen.findByTestId('turn-banner')

    await turn.frame(T_WORKSPACE('ready', 3))

    // LIVENESS: a READ turn produces no phase headline — `turnPhase` reads the frames, and a turn
    // that touched nothing has nothing to say. The liveness proof is that the surface is still
    // running this turn at all, which a component that had thrown could not be.
    expect(screen.getByTestId('stop-turn')).toBeTruthy()
    // THE CLAIM ITSELF: the `ready` frame carries the ordinary lifecycle MESSAGE, and routing
    // that to the banner would post phase narration above the composer on every turn. Only a `notice` reaches it.
    expect(screen.getByTestId('turn-banner').textContent).toMatch(/could not check/i)
    expect(screen.getByTestId('turn-banner').textContent).not.toMatch(/getting your workspace ready/i)
  })
})
