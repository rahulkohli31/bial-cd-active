/**
 * The advisory build lock, seen from the page (KTD-7).
 *
 * `buildLock` is the FAST cross-tab UX pre-check only — the authoritative one-build-per-user
 * barrier is the server's 409 (tested in BuilderPage-session.test.jsx). Here we pin that
 * BuilderPage still CLAIMS the project when a build starts and consults `blockedBy` before it
 * starts another (a second builder chat in the same project is warned before it costs a round
 * trip), that a different project is not blocked, that the claim is released when the build ends,
 * and that a planning chat is never blocked. Each BuilderPage owns its own manager over the shared
 * BroadcastChannel, so these two-page tests genuinely travel the wire.
 *
 * WHAT A CLAIM IS has not changed across either migration: "this chat, in this project, is
 * building", held for the build's duration and retracted at its terminal, so a second tab can say
 * so instantly instead of discovering it from a 409 several seconds later. WHAT HAS CHANGED TWICE
 * is which chat holds it and where the acquire/release calls that say so now live:
 *
 *   - U5 made a build a Write TURN rather than a C3 session, but the claim was still taken and
 *     dropped by the SAME chat the button was pressed in (the deleted `watchBuildTurn`).
 *   - U12 made Build-it a HANDOFF: the press creates a brand-new build chat, seeds it with the
 *     plan, and starts the turn there — so the claim now has to be for THAT chat, not the one the
 *     button was in. `acquire` moved into `handleBuildIt`, right after the handoff call resolves,
 *     claiming `outcome.chatId`. `release` moved into `endGenerating`, the one point every turn
 *     path (send, reattach, reload-mid-build) settles through — because the chat that ends up
 *     watching the new build's turn is whichever page navigates there and reattaches to it
 *     (`reattachToTurn`), which is not necessarily the page that pressed the button.
 *
 * This file drives that reattach path directly: every "build starts" step here mints a fresh
 * chat id, registers it as the project's `listProjectConversations` would, and gives it a running
 * `activeTurn` so the SAME BuilderPage instance — now displaying the new chat, having navigated
 * there — reattaches and renders the live narrative (`build-progress`/`build-outcome`) exactly as
 * a reload mid-build already does (BuilderPage-thread.test.jsx's R8 suite).
 *
 * The pre-check hangs off the BRIEF CARD's confirmation, not off Send (003-U4). A send is just a
 * chat turn, and refusing to let someone TALK to the assistant because another tab is building
 * would be nonsense — refusing them a SECOND BUILD is the rule. So the warning lands on the card
 * (`role="alert"` inside `plan-options-card`), which re-arms as "Try again"; it is not a toast.
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
  loadBuilds: vi.fn(), newBuild: vi.fn(), createBuild: vi.fn(), getBuild: vi.fn(),
  deleteBuild: vi.fn(), listProjectConversations: vi.fn(), buildUserParts: vi.fn(),
  start: vi.fn(), stop: vi.fn(), getStatus: vi.fn(), forceEnd: vi.fn(),
}))

// THE LEGACY RELAY MOCK IS GONE WITH THE HOOK (Plan D U17). Both kinds of chat run on the turn
// stream now, so the mock below is the only transport this file needs — where it used to need two,
// one per page.
vi.mock('../../utils/turnStreamApi', async (orig) => ({
  ...(await orig()),
  startTurn: (...a) => h.startTurn(...a),
  readTurnStream: (...a) => h.readTurnStream(...a),
  buildFromPlan: (...a) => h.buildFromPlan(...a),
  stopTurn: (...a) => h.stopTurn(...a),
  resolvePlanOptions: (...a) => h.resolvePlanOptions(...a),
}))
vi.mock('../../utils/builderHistory', () => ({
  loadBuilds: h.loadBuilds, newBuild: h.newBuild, createBuild: h.createBuild,
  getBuild: h.getBuild, deleteBuild: h.deleteBuild, deriveTitle: (t) => (t || '').slice(0, 40),
}))
vi.mock('../../utils/conversationApi', () => ({
  listProjectConversations: h.listProjectConversations,
  uuidv7: (...a) => h.uuidv7(...a),
}))
vi.mock('../../components/layout/Navbar', () => ({ default: () => null }))
vi.mock('../../components/LivePreview', () => ({ default: () => null }))
vi.mock('../../utils/attachmentStore', async (orig) => ({ ...(await orig()), buildUserParts: h.buildUserParts }))

import ConversationSurface from '../../components/chat/ConversationSurface'

/**
 * The browser's Back button, as a thing a test can press. Rendered as a sibling of the routed
 * page so it survives the handoff's navigate — which is the only way to reach the state this
 * file's back-navigation test is about.
 */
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

// The project's build-chat directory, the way `listProjectConversations` would answer it — and
// which of those chats currently has a running turn a reattach would find via `activeTurn`. Both
// are reset fresh per test and grown by `mintBuild` below, because U12 means EVERY build in this
// file lands on a chat that did not exist when the test started.
let projectBuilds
let liveTurnByChat

/**
 * Arrange for the NEXT Build-it press to mint `id` (the CLIENT-MINTED chat `uuidv7()` hands
 * `handleBuildIt`), and register that chat as the server would once it exists: listed in the
 * project's directory under `title` (so `buildBlockedMessage` can name it to a sibling tab — see
 * BuilderPage.tsx), and carrying a running `activeTurn` (so the page that navigates there
 * reattaches to it via `reattachToTurn`, the same path a reload mid-build already takes, and
 * renders the live `build-progress`/`build-outcome` narrative this file asserts on).
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
  h.newBuild.mockReturnValue('build-N')
  h.createBuild.mockResolvedValue({ ok: true })
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
  // U12: buildFromPlan hands off to a NEW chat and echoes the caller's minted id back as
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

  it('a second build RE-ACQUIRES the claim — a second chat stays blocked after the refine (finding #23)', async () => {
    mintBuild('new-A', 'First build')
    const a = renderBuilder('build-A')
    await buildFrom(a.container, 'build it')
    await within(a.container).findByTestId('stop-turn')
    expect(h.buildFromPlan).toHaveBeenCalledTimes(1)

    // End A's first build — its claim on 'new-A' retracts once `endGenerating` runs at the
    // reattach's settle point (BuilderPage.tsx's `endGenerating` docblock: "the one point every
    // turn path settles through"). NOT `findByTestId('build-outcome')`: that card is a confirmed,
    // separately-tracked gap (BuilderPage-outcome.test.jsx's diagnostic note) — `showBuildOutcome`
    // has no call site on the turn-based path any more, so a live build's end currently clears the
    // narrative bubble and shows NOTHING until a reload. Waiting for the live bubble to clear is
    // the honest proxy: it is the one DOM change this page actually makes when the turn ends.
    await turn.frame(T_BUILD_END())
    await turn.end()
    await waitFor(() => expect(within(a.container).queryByTestId('stop-turn')).toBeNull())

    // Refine from A's now-adopted chat — POST-build (U16: A's composer is shut while A's agent
    // works, so the refine can only be asked for once the build is over). The press hands off
    // AGAIN, to a SECOND fresh chat: ending the first RETRACTED A's claim on 'new-A', so this
    // second build has to assert its own — otherwise it is claim-less and B sails past the check.
    const second = scriptBuildTurn({ plan: planReply('Make it dark.', 'opt-2') })
    h.readTurnStream.mockImplementation(second.impl)
    turn = second
    mintBuild('new-A2', 'First build (refined)')
    await buildFrom(a.container, 'make it dark mode')
    await waitFor(() => expect(h.buildFromPlan).toHaveBeenCalledTimes(2))
    // This page never provisions a C3 session any more (`session.start()` is dead in
    // BuilderPage.tsx — see its own docblock); `h.stop` pins that the retired stop-a-live-session
    // arm is never reached on this path, not that a candidate was found and skipped.
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
    // A's transition answers `already_started` (a double click, or a race with another tab, beat
    // it): the turn is already running in the chat A's OWN mint named, and this press simply
    // JOINS it. Joining is still building as far as every other tab is concerned, so this arm has
    // to claim exactly like `started` does — else A's live build is claim-less and B sails past
    // the advisory pre-check.
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

    // A's build ends → its advisory claim retracts once `endGenerating` runs at the reattach's
    // settle point. NOT `findByTestId('build-outcome')` — see the matching comment in "a second
    // build RE-ACQUIRES the claim" above: that card has no call site on this path yet
    // (BuilderPage-outcome.test.jsx), so the live bubble clearing is the honest signal that the
    // turn actually ended.
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
    // THE HANDOFF MADE THIS REACHABLE ON THE COMMONEST ACTION THERE IS. Build it pushes a
    // navigation to the new chat, and the arrival effect wipes the transcript on screen before
    // a byte of the new one arrives. Press Back before that fetch resolves and the effect runs
    // again for the chat we came from — where its own "already loaded" guard used to still
    // name it, because the guard was only ever updated on a SUCCESSFUL load. Hydration was
    // skipped, and the citizen sat looking at the empty transcript the outbound trip made,
    // with no way back to it short of reloading the page.
    //
    // The guard means "the chat whose transcript is on screen". Clearing the transcript has to
    // clear it too, and this is what says so.
    mintBuild('new-A', 'First build')
    // THE FETCH FOR THE NEW CHAT NEVER RESOLVES, which is the whole precondition: Back has to
    // land while the outbound hydration is still in flight. A resolved one would have moved
    // the guard on to the new chat, and the return trip would re-hydrate for the wrong reason.
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
    // THE LEAK THE HANDOFF OPENED. Acquire and release used to be one scope — the build ran in
    // the chat the button was pressed in, and that watcher's own settle retracted the claim.
    // Now the press claims a chat it is about to NAVIGATE to, and the release belongs to
    // whoever ends up watching that chat's turn. When there is no turn to watch — the build
    // ended before this page arrived and asked, or the read projection has not caught up —
    // nobody retracts it, and the 5s heartbeat goes on announcing a claim with nothing behind
    // it. The citizen's symptom is not subtle: every later Build press in this project, in
    // this tab or a sibling, is told "another chat is already building this project", and
    // only closing the tab clears it.
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
  // RE-POINTED, NOT DELETED (Plan D U17). This used to mount `ChatPage` beside the builder,
  // because a planning chat was a different component on a different transport and the claim was
  // that the build lock did not reach it. One surface serves both kinds now, so the same claim is
  // made the way it can be made: a SECOND conversation, mounted through the same surface, sends
  // while the first one's build holds the project's advisory claim.
  //
  // The claim is still worth pinning, and arguably more so: with one component the risk that a
  // project-scoped lock leaks into a sibling chat is higher, not lower, than when the two were
  // separate files.
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
