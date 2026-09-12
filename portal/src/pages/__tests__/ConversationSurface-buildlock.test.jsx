/**
 * WHY THIS EXISTS: the advisory build lock, seen from the page.
 *
 * `buildLock` is the FAST cross-tab UX pre-check only — the authoritative one-build-per-user
 * barrier is the server's 409 (tested in ConversationSurface-session.test.jsx). Pinned here: the
 * page CLAIMS the project when a build starts and consults `blockedBy` before starting another,
 * a different project is not blocked, the claim releases when the build ends, and a planning chat
 * is never blocked. Each page owns its own manager over the shared BroadcastChannel, so these
 * two-page tests genuinely travel the wire.
 *
 * A CLAIM is "this chat, in this project, is building", held for the build's duration. WHICH
 * chat holds it has changed: Build-it is now a HANDOFF — the press creates a brand-new build
 * chat and starts the turn there, so `acquire` lives in `handleBuildIt` (claiming the new
 * `outcome.chatId`) and `release` lives in `endGenerating`, the one point every turn path (send,
 * reattach, reload-mid-build) settles through, since the page that ends up reattached to the new
 * build's turn may not be the page that pressed the button.
 *
 * This file drives that reattach path directly: every "build starts" step mints a fresh chat id
 * and gives it a running `activeTurn`, so the SAME page instance reattaches and renders the live
 * narrative exactly as a reload mid-build already does (ConversationSurface-thread.test.jsx's
 * suite).
 *
 * The pre-check hangs off the BRIEF CARD's confirmation, not off Send — refusing a chat TURN
 * would be nonsense, refusing a SECOND BUILD is the rule. The warning lands on the card
 * (`role="alert"` inside `plan-options-card`), not a toast.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, waitFor, act, cleanup, within } from '@testing-library/react'
import { MemoryRouter, Routes, Route, useNavigate } from 'react-router-dom'
import {
  FakeEventSource, makeClient, primeClient,
  PLAN_CARD_ID, planReply, primeTurn,
  waitForGateOpen, scriptBuildTurn, T_BUILD_END, BUILD_TURN_ID,
} from './_builderSession.jsx'

const h = vi.hoisted(() => ({
  startTurn: vi.fn(), readTurnStream: vi.fn(), buildFromPlan: vi.fn(), stopTurn: vi.fn(),
  resolvePlanOptions: vi.fn(), uuidv7: vi.fn(),
  loadBuilds: vi.fn(), getBuild: vi.fn(),
  listProjectConversations: vi.fn(), buildUserParts: vi.fn(),
  stop: vi.fn(), getStatus: vi.fn(),
}))

// Both kinds of chat run on the turn stream now, so the mock below is the only transport this
// file needs.
vi.mock('../../utils/turnStreamApi', async (orig) => ({
  ...(await orig()),
  startTurn: (...a) => h.startTurn(...a),
  readTurnStream: (...a) => h.readTurnStream(...a),
  buildFromPlan: (...a) => h.buildFromPlan(...a),
  stopTurn: (...a) => h.stopTurn(...a),
  resolvePlanOptions: (...a) => h.resolvePlanOptions(...a),
}))
vi.mock('../../utils/builderHistory', () => ({
  loadBuilds: h.loadBuilds, getBuild: h.getBuild, deriveTitle: (t) => (t || '').slice(0, 40),
}))
vi.mock('../../utils/conversationApi', () => ({
  // The send path creates the chat before its first upload; stubbed so no network is reached.
  createConversation: async () => ({ id: 'conv-created' }),
  listProjectConversations: h.listProjectConversations,
  uuidv7: (...a) => h.uuidv7(...a),
}))
vi.mock('../../components/layout/Navbar', () => ({ default: () => null }))
vi.mock('../../components/LivePreview', () => ({ default: () => null }))
vi.mock('../../utils/attachmentStore', async (orig) => ({ ...(await orig()), buildUserParts: h.buildUserParts }))

import ConversationSurface from '../../components/chat/ConversationSurface'

/** The browser's Back button, as a thing a test can press — rendered as a sibling of the routed
 *  page so it survives the handoff's navigate. */
function BackButton() {
  const navigate = useNavigate()
  return <button data-testid="go-back" onClick={() => navigate(-1)} />
}

function renderBuilder(chatId, projectId = 'p1') {
  const fake = new FakeEventSource(chatId)
  const deps = { client: makeClient(h), eventSourceFactory: () => fake }
  const view = render(
    <MemoryRouter initialEntries={[`/chat/${chatId}`]}>
      <BackButton />
      <Routes>
        <Route path="/chat/:chatId" element={<ConversationSurface projectId={projectId} projectName="VIP Movement" buildSessionDeps={deps} />} />
      </Routes>
    </MemoryRouter>,
  )
  return { ...view, fake }
}

/** A chat turn — the model answers with a brief, so a card appears. Starts nothing on its own. */
async function sendFrom(container, text = 'make it blue') {
  await waitForGateOpen()
  const textarea = within(container).getByPlaceholderText(/ask for another change/i)
  fireEvent.change(textarea, { target: { value: text } })
  fireEvent.keyDown(textarea, { key: 'Enter' })
}

/** Confirm the newest brief card — the page's only build trigger. Started cards read "Building…". */
async function confirmBrief(container) {
  const button = await within(container).findByRole('button', { name: /^Build this plan$/ })
  fireEvent.click(button)
  return button
}

/** The whole user-visible path to a build: ask, get a brief, confirm it. */
async function buildFrom(container, text = 'make it blue') {
  await sendFrom(container, text)
  await confirmBrief(container)
}

/**
 * The build turn every page in this file shares (only one build is ever meant to be live at a
 * time — that is the rule under test). Rebuilt per test; the socket stays open until `end()`,
 * because a HELD claim is precisely a build that has not finished.
 */
let turn

/** The card the newest turn produced, i.e. the one a confirmation's error lands on. */
async function lastCard(container) {
  const cards = await within(container).findAllByTestId('offer-strip')
  return cards[cards.length - 1]
}

// BroadcastChannel delivery is queued on a task. A newly-mounted manager posts a `poll`; the holder
// answers with an `announce`; only after that round-trip does the new tab's `blockedBy` see the
// claim. Drain a few ticks so that handshake completes before the next build.
const flushChannel = () => act(async () => { for (let i = 0; i < 6; i += 1) await new Promise((r) => setTimeout(r, 0)) })

// The project's build-chat directory and which of those chats has a running `activeTurn`. Both
// reset fresh per test and grow via `mintBuild` below, since the handoff means every build here
// lands on a chat that didn't exist when the test started.
let projectBuilds
let liveTurnByChat

/**
 * Arrange for the NEXT Build-it press to mint `id` (the client-minted chat `uuidv7()` hands
 * `handleBuildIt`), and register that chat as the server would: listed in the project's
 * directory under `title` (so `buildBlockedMessage` can name it to a sibling tab), and carrying
 * a running `activeTurn` so the page that navigates there reattaches to it.
 */
function mintBuild(id, title, { turnId = BUILD_TURN_ID } = {}) {
  h.uuidv7.mockReturnValueOnce(id)
  projectBuilds = [...projectBuilds, { id, kind: 'build', title, updatedAt: new Date().toISOString() }]
  liveTurnByChat.set(id, { turnId, lastSeq: 0 })
}

beforeEach(() => {
  vi.clearAllMocks()
  Element.prototype.scrollIntoView = vi.fn()
  primeClient(h)
  h.loadBuilds.mockResolvedValue([])
  liveTurnByChat = new Map()
  h.getBuild.mockImplementation(async (id) => ({
    id,
    kind: 'build',
    messages: [],
    activeTurn: liveTurnByChat.get(id) ?? null,
  }))
  h.buildUserParts.mockImplementation(async (text) => [{ type: 'text', text }])
  projectBuilds = []
  h.listProjectConversations.mockImplementation(async () => projectBuilds)
  // Every interview turn answers with a ready-to-build brief, so these suites reach the lock
  // mechanics in one send + one click; the build turn it confirms into stays open.
  primeTurn(h)
  // buildFromPlan hands off to a NEW chat and echoes the caller's minted id back as
  // `chatId` (turnStreamApi.ts's BuildFromPlanOutcome docblock: "Echoed back rather than
  // assumed... the same id on a double-press and the thing to navigate to either way").
  h.buildFromPlan.mockImplementation(async (_conversationId, _toolCallId, chatId) => ({
    outcome: 'started',
    chatId,
    turnId: BUILD_TURN_ID,
  }))
  turn = scriptBuildTurn()
  h.readTurnStream.mockImplementation(turn.impl)
})
afterEach(() => cleanup())

describe('BuilderPage — one build at a time, per project (advisory pre-check)', () => {
  it('warns a second builder chat in the SAME project before it starts, naming the holder', async () => {
    mintBuild('new-A', 'First build')
    const a = renderBuilder('build-A')
    await buildFrom(a.container)
    // A's handoff carried the minted id, and its page followed it — the claim is for THAT chat,
    // not the one the button was pressed in.
    await waitFor(() => expect(h.buildFromPlan).toHaveBeenCalledWith('build-A', PLAN_CARD_ID, 'new-A'))
    await within(a.container).findByTestId('stop-turn') // A's build is live → claim held on 'new-A'

    const b = renderBuilder('build-B')
    await within(b.container).findByPlaceholderText(/ask for another change/i)
    await flushChannel() // let B learn about A's claim over the channel
    await buildFrom(b.container, 'and add a table')

    await lastCard(b.container) // wait for the offer to be on screen before reading the refusal
    const warning = await within(b.container).findByTestId('urgent-banner')
    expect(/already building this project/i.test(warning.textContent)).toBe(true)
    expect(/First build/.test(warning.textContent)).toBe(true) // named the holder, not "some other tab"
    // B never started a build — only A's handoff fired.
    expect(h.buildFromPlan).toHaveBeenCalledTimes(1)
  })

  it('does not block a builder chat in a DIFFERENT project', async () => {
    mintBuild('new-A', 'A build')
    const a = renderBuilder('build-A', 'p1')
    await buildFrom(a.container)
    await within(a.container).findByTestId('stop-turn')

    mintBuild('new-B', 'B build')
    const b = renderBuilder('build-B', 'p2')
    await within(b.container).findByPlaceholderText(/ask for another change/i)
    await flushChannel()
    await buildFrom(b.container, 'different project')

    await waitFor(() => expect(h.buildFromPlan).toHaveBeenCalledTimes(2)) // both started
  })

  it('a second build RE-ACQUIRES the claim — a second chat stays blocked after the refine', async () => {
    mintBuild('new-A', 'First build')
    const a = renderBuilder('build-A')
    await buildFrom(a.container, 'build it')
    await within(a.container).findByTestId('stop-turn')
    expect(h.buildFromPlan).toHaveBeenCalledTimes(1)

    // End A's first build — its claim retracts once `endGenerating` runs at the reattach's settle
    // point. NOT `findByTestId('build-outcome')`: `showBuildOutcome` has no call site on the
    // turn-based path (a confirmed, separately-tracked gap — ConversationSurface-outcome.test.jsx),
    // so waiting for the live bubble to clear is the one DOM change this page actually makes.
    await turn.frame(T_BUILD_END())
    await turn.end()
    await waitFor(() => expect(within(a.container).queryByTestId('stop-turn')).toBeNull())

    // Refine from A's now-adopted chat (POST-build only — the composer is shut while the agent
    // works). The press hands off AGAIN to a SECOND fresh chat, so this build has to assert its
    // own claim — the first was already retracted when its build ended.
    const second = scriptBuildTurn({ plan: planReply('Make it dark.', 'opt-2') })
    h.readTurnStream.mockImplementation(second.impl)
    turn = second
    mintBuild('new-A2', 'First build (refined)')
    await buildFrom(a.container, 'make it dark mode')
    await waitFor(() => expect(h.buildFromPlan).toHaveBeenCalledTimes(2))
    // `session.start()` is deleted, not merely unused — `h.stop` pins that the retired
    // stop-a-live-session arm is never reached on this path, not that a candidate was skipped.
    expect(h.stop).not.toHaveBeenCalled()
    await within(a.container).findByTestId('stop-turn')

    const b = renderBuilder('build-B')
    await within(b.container).findByPlaceholderText(/ask for another change/i)
    await flushChannel() // let B learn about A's re-acquired claim
    await buildFrom(b.container, 'me too')

    await lastCard(b.container) // wait for the offer to be on screen before reading the refusal
    expect(/already building this project/i.test((await within(b.container).findByTestId('urgent-banner')).textContent)).toBe(true)
    expect(h.buildFromPlan).toHaveBeenCalledTimes(2) // only A's two starts — B never started
  })

  it('a same-project already_started outcome CLAIMS the project too — a second chat is still warned', async () => {
    // A's transition answers `already_started` (a double click, or a race, beat it): the turn is
    // already running, and this press simply JOINS it. Joining still has to claim exactly like
    // `started` does — else A's live build is claim-less and B sails past the pre-check.
    mintBuild('new-A', 'First build')
    h.buildFromPlan.mockResolvedValueOnce({ outcome: 'already_started', chatId: 'new-A', turnId: BUILD_TURN_ID })
    const a = renderBuilder('build-A')
    await buildFrom(a.container)
    await within(a.container).findByTestId('stop-turn') // joined → A's build is live

    const b = renderBuilder('build-B')
    await within(b.container).findByPlaceholderText(/ask for another change/i)
    await flushChannel() // let B learn about A's claim
    await buildFrom(b.container, 'me too')

    await lastCard(b.container) // wait for the offer to be on screen before reading the refusal
    expect(/already building this project/i.test((await within(b.container).findByTestId('urgent-banner')).textContent)).toBe(true)
    expect(h.buildFromPlan).toHaveBeenCalledTimes(1) // only A's transition — B never started
  })

  it('releases the claim when the build ends, so a blocked second chat can then start', async () => {
    mintBuild('new-A', 'First build')
    const a = renderBuilder('build-A')
    await buildFrom(a.container)
    await within(a.container).findByTestId('stop-turn')

    const b = renderBuilder('build-B')
    await within(b.container).findByPlaceholderText(/ask for another change/i)
    await flushChannel()
    await buildFrom(b.container, 'wait for me')
    await lastCard(b.container) // wait for the offer to be on screen before reading the refusal
    expect(/already building this project/i.test((await within(b.container).findByTestId('urgent-banner')).textContent)).toBe(true)
    expect(h.buildFromPlan).toHaveBeenCalledTimes(1)

    // A's build ends → its claim retracts once `endGenerating` runs (see "a second build
    // RE-ACQUIRES the claim" above for why this waits on the live bubble, not `build-outcome`).
    await turn.frame(T_BUILD_END())
    await turn.end()
    await waitFor(() => expect(within(a.container).queryByTestId('stop-turn')).toBeNull())
    await flushChannel() // let the retract reach B

    // B's offer was never spent by the refused press, so the plan it already holds is buildable
    // again the moment the claim retracts — which is itself another press, so it mints (and
    // claims) a fresh chat too.
    mintBuild('new-B', 'Second build')
    fireEvent.click(within(await lastCard(b.container)).getByRole('button', { name: /^Build this plan$/ }))
    await waitFor(() => expect(h.buildFromPlan).toHaveBeenCalledTimes(2))
    expect(h.buildFromPlan).toHaveBeenLastCalledWith('build-B', PLAN_CARD_ID, 'new-B')
  })

  it('★ Back, straight after a handoff, does not leave the chat it returns to blank', async () => {
    // The "already loaded" guard means "the chat whose transcript is on screen" — clearing the
    // transcript on arrival has to clear the guard too, or a Back press mid-hydration returns to
    // a chat the guard still calls loaded, and its transcript never re-fetches.
    mintBuild('new-A', 'First build')
    // THE WHOLE PRECONDITION: the new chat's fetch must NEVER resolve, so Back lands while the
    // outbound hydration is still in flight — a resolved one would move the guard on and the
    // return trip would re-hydrate for the wrong reason.
    const settled = h.getBuild.getMockImplementation()
    h.getBuild.mockImplementation(async (id) => (id === 'new-A' ? new Promise(() => {}) : settled(id)))

    const a = renderBuilder('build-A')
    await buildFrom(a.container)
    await waitFor(() => expect(h.buildFromPlan).toHaveBeenCalledWith('build-A', PLAN_CARD_ID, 'new-A'))
    await waitFor(() => expect(h.getBuild).toHaveBeenCalledWith('new-A'))

    const before = h.getBuild.mock.calls.filter(([id]) => id === 'build-A').length
    fireEvent.click(within(a.container).getByTestId('go-back'))

    // Asked the server again for the chat we came back to — the hydration was not skipped.
    await waitFor(() =>
      expect(h.getBuild.mock.calls.filter(([id]) => id === 'build-A').length).toBeGreaterThan(before),
    )
    // And it is a working chat, not an empty shell: the composer is live again.
    await within(a.container).findByPlaceholderText(/ask for another change/i)
  })

  it('★ releases the claim when the chat it handed off to has nothing running', async () => {
    // THE LEAK THE HANDOFF OPENED: the press claims a chat it is about to NAVIGATE to, and
    // release belongs to whoever ends up watching that chat's turn. When there is no turn to
    // watch — the build ended before this page arrived, or the read projection lagged — nobody
    // retracts it, and every later Build press in this project is told "another chat is already
    // building" until the tab closes.
    //
    // Registered in the project directory but deliberately NOT in `liveTurnByChat`, which is
    // exactly "arrived, asked, and nothing is running here".
    h.uuidv7.mockReturnValueOnce('new-A')
    projectBuilds = [
      ...projectBuilds,
      { id: 'new-A', kind: 'build', title: 'First build', updatedAt: new Date().toISOString() },
    ]

    const a = renderBuilder('build-A')
    await buildFrom(a.container)
    await waitFor(() => expect(h.buildFromPlan).toHaveBeenCalledWith('build-A', PLAN_CARD_ID, 'new-A'))
    // The page followed the handoff and hydrated the new chat — this is the moment the claim
    // has to go, because nothing after it will.
    await waitFor(() => expect(h.getBuild).toHaveBeenCalledWith('new-A'))

    mintBuild('new-B', 'Second build')
    const b = renderBuilder('build-B')
    await within(b.container).findByPlaceholderText(/ask for another change/i)
    await flushChannel()
    await buildFrom(b.container, 'and add a table')

    // Not blocked: B's press reaches the server and hands off to its own new chat. With the
    // claim leaked, B is refused locally by `buildBlockedMessage` and never gets this far.
    await waitFor(() => expect(h.buildFromPlan).toHaveBeenCalledTimes(2))
    expect(h.buildFromPlan).toHaveBeenLastCalledWith('build-B', PLAN_CARD_ID, 'new-B')
  })
})

describe('a SIBLING conversation is never blocked by another chat\u2019s build', () => {
  // One surface now serves both a planning chat and a builder, so the claim is made the way it
  // can be: a SECOND conversation, mounted through the same surface, sends while the first one's
  // build holds the project's advisory claim. Still worth pinning, arguably more so — one
  // component raises the risk of a project-scoped lock leaking into a sibling chat, not lowers it.
  it('sends freely while a build is live in another chat of the same project', async () => {
    mintBuild('new-A', 'First build')
    const a = renderBuilder('build-A')
    await buildFrom(a.container)
    await waitFor(() => expect(h.buildFromPlan).toHaveBeenCalled())

    h.listProjectConversations.mockResolvedValue([])
    h.startTurn.mockClear()
    const sibling = renderBuilder('chat-B')

    const box = await within(sibling.container).findByTestId('composer-input')
    fireEvent.change(box, { target: { value: 'what should this do?' } })
    fireEvent.keyDown(box, { key: 'Enter' })

    // The sibling's turn reached the server: the lock gates BUILD presses, never sends.
    await waitFor(() => expect(h.startTurn).toHaveBeenCalled())
  })
})
