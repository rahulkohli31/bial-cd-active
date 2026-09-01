/**
 * The unified-chat thread behaviors (U11/U13) that only the PAGE can prove:
 *
 *  - a send is a chat turn; the plan streams as PROSE and the card renders beside it —
 *    no fence anywhere, and nothing builds until the card is clicked;
 *  - a text-only reply (a clarifying question) renders with NO card;
 *  - a restored thread re-renders each card from its STORED state (pending → armed,
 *    refine/build → settled, older-than-newest → expired) — no local state to resync;
 *  - a used card cannot re-fire — Build it is now a HANDOFF, so "cannot re-fire" is proven
 *    by the press ending in a navigation to a brand-new build chat, not by a settled local state;
 *  - U19 deleted the in-composer mode switcher along with the `mode` it displayed (U1 collapsed
 *    ConversationMode into the fixed-at-creation ChatKind) — this file's own guard for that is
 *    below, under "the U13 header".
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import {
  FakeEventSource, makeClient, primeClient, BRIEF, PLAN_CARD_ID, primeTurn,
  turnStreaming, textReply,
  waitForGateOpen,
} from './_builderSession.jsx'
import { ApiError } from '../../utils/apiError'

// The id `handleBuildIt` mints for every Build-it press in this file (U12: the id is CLIENT
// minted via `uuidv7`, then echoed back by the server as `BuildFromPlanOutcome.chatId` — the
// mock below mirrors that echo). One fixed id is enough here because no single test in this
// file presses Build it twice — a second, distinguishable mint only matters for the cross-tab
// lock suite (BuilderPage-buildlock.test.jsx), which needs to tell several handoffs apart.
const MINTED_BUILD_CHAT_ID = 'minted-build-chat-1'

const h = vi.hoisted(() => ({
  loadBuilds: vi.fn(), newBuild: vi.fn(), createBuild: vi.fn(), getBuild: vi.fn(),
  deleteBuild: vi.fn(), listProjectConversations: vi.fn(), buildUserParts: vi.fn(), uuidv7: vi.fn(),
  startTurn: vi.fn(), readTurnStream: vi.fn(), buildFromPlan: vi.fn(),
  resolvePlanOptions: vi.fn(),
  start: vi.fn(), stop: vi.fn(), getStatus: vi.fn(), forceEnd: vi.fn(),
  relaunchPreview: vi.fn(),
}))

vi.mock('../../utils/builderHistory', () => ({
  loadBuilds: h.loadBuilds, newBuild: h.newBuild, createBuild: h.createBuild,
  getBuild: h.getBuild, deleteBuild: h.deleteBuild, deriveTitle: (t) => (t || '').slice(0, 40),
}))
vi.mock('../../utils/conversationApi', () => ({
  listProjectConversations: h.listProjectConversations,
  uuidv7: (...a) => h.uuidv7(...a),
}))
vi.mock('../../utils/chatHistory', () => ({ relativeTime: () => 'now' }))
vi.mock('../../components/layout/Navbar', () => ({ default: () => null }))
vi.mock('../../components/LivePreview', () => ({ default: () => null }))
vi.mock('../../components/AttachmentChips', () => ({ default: () => null }))
vi.mock('../../utils/attachmentStore', async (orig) => ({ ...(await orig()), buildUserParts: h.buildUserParts }))
vi.mock('../../utils/turnStreamApi', async (orig) => ({
  ...(await orig()),
  startTurn: (...a) => h.startTurn(...a),
  readTurnStream: (...a) => h.readTurnStream(...a),
  buildFromPlan: (...a) => h.buildFromPlan(...a),
  resolvePlanOptions: (...a) => h.resolvePlanOptions(...a),
}))

import ConversationSurface from '../../components/chat/ConversationSurface'

const QUESTIONS = 'Which terminals should this cover, and who approves a visitor?'

function renderThread({ state, chatId = 'thread-1' } = {}) {
  const fake = new FakeEventSource(chatId)
  const deps = { client: makeClient(h), eventSourceFactory: () => fake }
  const view = render(
    <MemoryRouter initialEntries={[{ pathname: `/chat/${chatId}`, state }]}>
      <Routes>
        <Route
          path="/chat/:chatId"
          element={<ConversationSurface projectId="p1" projectName="VIP Movement" buildSessionDeps={deps} />}
        />
      </Routes>
    </MemoryRouter>,
  )
  return { ...view, fake }
}

const composer = () => screen.getByPlaceholderText(/describe what you need/i)
async function send(text = 'a visitor app') {
  await waitForGateOpen()
  fireEvent.change(composer(), { target: { value: text } })
  fireEvent.keyDown(composer(), { key: 'Enter' })
}

/** A stored plan-options projection message (what a reload hydrates). `PlanOptionsItem` is
 *  `{ type, seq, toolCallId, state }` only now (turnStreamApi.ts) — `mode` and `reason` are
 *  both gone (the per-thread mode setting, and the failure-named re-arm respectively). */
const storedCard = (seq, toolCallId, state) => ({
  id: `srv_${seq}_p`,
  role: 'assistant',
  seq,
  parts: [{ type: 'plan_options', item: { type: 'plan_options', seq, toolCallId, state } }],
})

beforeEach(() => {
  vi.clearAllMocks()
  Element.prototype.scrollIntoView = vi.fn()
  primeClient(h)
  h.newBuild.mockReturnValue('thread-1')
  h.createBuild.mockResolvedValue({ ok: true })
  h.getBuild.mockResolvedValue(null)
  h.loadBuilds.mockResolvedValue([])
  h.listProjectConversations.mockResolvedValue([])
  h.buildUserParts.mockImplementation(async (text) => [{ type: 'text', text }])
  h.uuidv7.mockReturnValue(MINTED_BUILD_CHAT_ID)
  primeTurn(h)
  // U12: buildFromPlan hands off to a NEW chat and echoes the caller's minted id back as
  // `chatId` — the shared harness's `primeTurn` still answers the pre-handoff shape (a bare
  // `turnId`, no `chatId`), so every test in this file needs the real contract's shape.
  h.buildFromPlan.mockResolvedValue({ outcome: 'started', chatId: MINTED_BUILD_CHAT_ID, turnId: 'bt-1' })
})
afterEach(() => cleanup())

describe('the routing rule — a send is a chat turn, never a build', () => {
  it('streams the plan as prose with the card beside it; nothing builds until the click', async () => {
    renderThread()
    await send('a visitor app')

    // The plan text is a normal assistant bubble (no fence, no hidden markup)…
    expect(await screen.findByText(new RegExp(BRIEF.slice(0, 30)))).toBeTruthy()
    // …with the actionable card.
    const build = await screen.findByRole('button', { name: /^Build this plan$/ })
    expect(screen.getByRole('button', { name: /keep planning/i })).toBeTruthy()
    expect(h.buildFromPlan).not.toHaveBeenCalled()

    fireEvent.click(build)
    // Three positional args now (U12): the third is the CLIENT-MINTED id of the brand-new build
    // chat this press hands off to.
    await waitFor(() =>
      expect(h.buildFromPlan).toHaveBeenCalledWith('thread-1', PLAN_CARD_ID, MINTED_BUILD_CHAT_ID),
    )
  })

  it('a clarifying reply renders with NO card — a question is a legitimate planning turn', async () => {
    h.readTurnStream.mockImplementation(turnStreaming(textReply(QUESTIONS)))
    renderThread()
    await send('something vague')

    expect(await screen.findByText(new RegExp(QUESTIONS.slice(0, 25)))).toBeTruthy()
    expect(screen.queryByRole('button', { name: /^Build this plan$/ })).toBeNull()
    expect(h.buildFromPlan).not.toHaveBeenCalled()
  })
})

describe('a restored thread re-renders every card from its STORED state', () => {
  it('an older card renders expired; the newest pending card is the only armed one', async () => {
    h.getBuild.mockResolvedValue({
      id: 'thread-1',
      mode: 'plan',
      messages: [
        { id: 'm0', role: 'user', seq: 0, parts: [{ type: 'text', text: 'plan a visitors app' }] },
        storedCard(1, 'opt-old', 'pending'),
        { id: 'm2', role: 'user', seq: 2, parts: [{ type: 'text', text: 'add exports too' }] },
        storedCard(3, 'opt-new', 'pending'),
      ],
    })
    renderThread()

    // FLIPPED (Plan D U16/U17). Two stored offers used to render as two cards in the transcript,
    // the older one drawn "expired" and informational. There is ONE control now and it lives on
    // the composer, so the newest offer is the only one on screen — which is a stronger form of
    // the same rule ("only the newest is actionable"): an expired card is a dead button a reader
    // can still see and try, and this is none.
    const cards = await screen.findAllByTestId('offer-strip')
    expect(cards).toHaveLength(1)
    expect(screen.queryByText(/newer plan supersedes/i)).toBeNull()
    expect(within(cards[0]).getByRole('button', { name: /^Build this plan$/ })).toBeTruthy()

    // …and it is the NEWEST offer's tool call it answers, not the older one's.
    fireEvent.click(within(cards[0]).getByRole('button', { name: /^Build this plan$/ }))
    await waitFor(() =>
      expect(h.buildFromPlan).toHaveBeenCalledWith('thread-1', 'opt-new', MINTED_BUILD_CHAT_ID),
    )
  })

  it('settled cards render settled — refine and build states carry no buttons', async () => {
    h.getBuild.mockResolvedValue({
      id: 'thread-1',
      mode: 'write',
      messages: [
        storedCard(1, 'opt-a', 'refine'),
        storedCard(2, 'opt-b', 'build'),
      ],
    })
    renderThread()

    // FLIPPED (D2, Plan D U16). Settled cards used to render settled COPY and lose their buttons.
    // A spent strip stays and stays PRESSABLE now — pressing it again is an ordinary request that
    // creates another Build chat — so what marks it is `data-spent`, not the removal of the
    // control. That is the deliberate change: "only one offer is live" is about which one blocks
    // the composer, never about which one a citizen is allowed to press.
    const strip = await screen.findByTestId('offer-strip')
    expect(strip.getAttribute('data-spent')).toBe('true')
    expect(within(strip).getByRole('button', { name: /^Build this plan$/ })).toBeTruthy()
    // The retired settled copy is gone with the card that carried it.
    expect(screen.queryByText(/you kept refining this plan/i)).toBeNull()
    expect(screen.queryByText(/build started from this plan/i)).toBeNull()
    // …and a spent offer does NOT block the composer: Send is free.
    expect(screen.queryByTestId('composer-gate-note')).toBeNull()
  })

  it('an inertness guard: a stored build_failed record never re-arms with the failure named — the state and its re-arm copy are both gone', async () => {
    // AN INERTNESS GUARD, not a deleted test (L8). This used to prove a failed Build-it press
    // left the card in a special `build_failed` state that showed the failure ("another build
    // is already running") and let the citizen retry from the SAME card. Neither half of that
    // survives U12: `build_failed` and its `reason` field are gone from `PlanOptionsItem`
    // (turnStreamApi.ts), because Build-it now fails inside the one handoff call that would
    // have produced this outcome — there is nothing left to persist. The deleted card's own
    // docblock said why: "a press that fails records nothing — the card was never spent, and
    // there is nothing to un-spend. A failure is said once, in the error line below the buttons,
    // by the caller that actually saw it."
    //
    // What's left to pin, from a row a pre-migration project might still carry: `build_failed` is
    // not one of the offer's recognised states, so the strip renders SPENT — it still draws (this
    // is the liveness half of the guard; a crash would leave nothing to query) but it offers no
    // re-arm, which is what this test used to require.
    h.getBuild.mockResolvedValue({
      id: 'thread-1',
      messages: [storedCard(1, 'opt-f', 'build_failed')],
    })
    renderThread()

    const card = await screen.findByTestId('offer-strip')
    // Liveness: the strip rendered its ordinary shell, not a blank tree.
    expect(within(card).getByRole('button', { name: /^Build this plan$/ })).toBeTruthy()
    // The retired failure-named copy is gone…
    expect(screen.queryByText(/another build is already running/i)).toBeNull()
    // …and so is the shell copy the card used to carry around its buttons.
    expect(screen.queryByText(/ready to build this plan/i)).toBeNull()
    // NOTHING IS EVER `disabled` HERE (R45/R64), which is the assertion that had to change rather
    // than the behaviour it guards. The old card rendered a real `disabled` for an unrecognised
    // state; a real `disabled` on a focused control blurs it to `document.body`, and this surface
    // does not ship one anywhere. An unrecognised stored state simply is not `pending`, so the
    // strip renders SPENT — pressable, and not blocking the composer.
    expect(card.getAttribute('data-spent')).toBe('true')
    expect(card.querySelector('[disabled]')).toBeNull()
  })
})

describe('a used card cannot re-fire', () => {
  it('after Build it succeeds it hands off to a NEW build chat — no second transition from the same card', async () => {
    // WHAT THIS USED TO PROVE: Build-it flipped THIS conversation into Write and streamed the
    // build here, so "no second transition" meant the card settled to its stored `build` state
    // and its buttons vanished while everything else about the chat stayed put.
    //
    // U12 changes what "no second transition" is EVIDENCE OF. There is no in-place settle to
    // observe any more — the press ends by LEAVING this chat for a brand-new one seeded with the
    // plan (handleBuildIt's own docblock: "the only place [the build's] narrative can honestly be
    // watched is there"). So the card itself is gone from the screen not because it settled, but
    // because the whole thread it lived on is. What's left to pin is the one-shot nature of the
    // button: exactly one handoff call, carrying the id `handleBuildIt` minted, and the page
    // actually following it.
    h.getBuild.mockImplementation(async (id) =>
      id === MINTED_BUILD_CHAT_ID
        ? {
            id,
            messages: [
              { id: 'm0', role: 'assistant', seq: 0, parts: [{ type: 'text', text: 'NEW BUILD CHAT TRANSCRIPT' }] },
            ],
          }
        : null,
    )
    renderThread()
    await send('a visitor app')
    fireEvent.click(await screen.findByRole('button', { name: /^Build this plan$/ }))

    await waitFor(() => expect(h.buildFromPlan).toHaveBeenCalledTimes(1))
    expect(h.buildFromPlan).toHaveBeenCalledWith('thread-1', PLAN_CARD_ID, MINTED_BUILD_CHAT_ID)
    // The page followed the handoff to the new chat (liveness: real content rendered there,
    // not a blank/crashed tree)…
    expect(await screen.findByText('NEW BUILD CHAT TRANSCRIPT')).toBeTruthy()
    // …which is why a second press from the same card is not merely refused — the card and the
    // thread it was on are gone.
    expect(screen.queryByRole('button', { name: /^Build this plan$/ })).toBeNull()
  })
})

describe('the reload half of the build narrative (U15)', () => {
  it('renders stored friendly steps and the in-progress truth line from the projection', async () => {
    h.getBuild.mockResolvedValue({
      id: 'thread-1',
      mode: 'write',
      messages: [
        { id: 'm0', role: 'user', seq: 0, parts: [{ type: 'text', text: 'build it' }] },
        {
          id: 'srv_1_s',
          role: 'assistant',
          seq: 1,
          parts: [{ type: 'step', step: { type: 'step', seq: 1, tool: 'write_file', label: 'Updated app/page.tsx', state: 'ok', hidden: false } }],
        },
        { id: 'srv_2_g', role: 'assistant', seq: 2, parts: [{ type: 'build_in_progress', sessionId: 'gone-1' }] },
      ],
    })
    // The premise, made REAL rather than assumed: the page now reattaches to any session the
    // transcript says was running, so "gone-1" has to actually be gone. A 404 is the ordinary
    // way that happens (server restart, or the ended-session retention lapsing).
    h.getStatus.mockRejectedValue(new ApiError('Build session not found.', 404))
    const { container } = renderThread()

    // The stored step renders through the SAME activity group the live path uses (AE43) — one
    // converter, one renderer, so a build read back looks like the build watched.
    fireEvent.click(await screen.findByTestId('activity-group-trigger'))
    const step = await screen.findByText('Updated app/page.tsx')
    expect(step.closest('[data-state]')?.getAttribute('data-state')).toBe('ok')
    expect(h.getStatus).toHaveBeenCalledWith('gone-1') // it DID try to rejoin
    // Nothing live re-tells this build, so the durable truth line renders instead of a dead
    // spinner — and no toast, because a vanished session is expected, not an error.
    //
    // IT IS PROSE IN THE TRANSCRIPT NOW, not a `data-kind` row. `build_in_progress` maps to no
    // rendered part, so the surface turns an unsuperseded anchor into the sentence itself — the
    // alternative was the row vanishing entirely and a citizen finding a transcript that simply
    // stops with no account of the build they started.
    await waitFor(() =>
      expect(container.textContent).toMatch(
        /a build was running here/i,
      ),
    )
    expect(screen.queryByText(/could not check on the build/i)).toBeNull()
  })

  it('groups a RUN of consecutive stored steps into ONE collapsed dropdown, and starts a new group after an interruption', async () => {
    h.getBuild.mockResolvedValue({
      id: 'thread-1',
      mode: 'write',
      messages: [
        { id: 'm0', role: 'user', seq: 0, parts: [{ type: 'text', text: 'build it' }] },
        { id: 's1', role: 'assistant', seq: 1, parts: [{ type: 'step', step: { tool: 'write_file', label: 'Step one', state: 'ok' } }] },
        { id: 's2', role: 'assistant', seq: 2, parts: [{ type: 'step', step: { tool: 'write_file', label: 'Step two', state: 'ok' } }] },
        { id: 's3', role: 'assistant', seq: 3, parts: [{ type: 'step', step: { tool: 'write_file', label: 'Step three', state: 'ok' } }] },
        { id: 'm4', role: 'assistant', seq: 4, parts: [{ type: 'text', text: 'Here is an update on the build.' }] },
        { id: 's5', role: 'assistant', seq: 5, parts: [{ type: 'step', step: { tool: 'write_file', label: 'Step five', state: 'ok' } }] },
      ],
    })
    renderThread()

    // RE-POINTED AT THE ACTIVITY GROUP, and the claim is unchanged: a real chat message
    // interrupts the run, so this is TWO groups rather than one merged one. It is the assertion
    // that keeps the merge honest — a rule that swept every stored step row into one group would
    // put "Step five" above prose that was written before it.
    const groups = await screen.findAllByTestId('activity-group')
    expect(groups).toHaveLength(2)

    fireEvent.click(within(groups[0]).getByTestId('activity-group-trigger'))
    const first = within(within(groups[0]).getByTestId('activity-group-rows'))
    expect(first.getByText('Step one')).toBeTruthy()
    expect(first.getByText('Step three')).toBeTruthy()

    fireEvent.click(within(groups[1]).getByTestId('activity-group-trigger'))
    const second = within(within(groups[1]).getByTestId('activity-group-rows'))
    expect(second.getByText('Step five')).toBeTruthy()
    // …and the interrupted run did NOT absorb it.
    expect(second.queryByText('Step three')).toBeNull()
  })
})

describe('the U13 header', () => {
  it('an inertness guard: no mode control mounts, and a legacy `mode` field on the header is never read', async () => {
    // AN INERTNESS GUARD, not a deleted test (L8). This used to prove the in-composer switcher
    // (F5/U6) showed the server-saved `mode` as its trigger label ("Mode: Ask"). U1 collapsed the
    // three-valued ConversationMode into a ChatKind fixed at creation, and U19 deleted
    // `ModeSwitcher` with it (see ModeSwitcher.test.tsx for the tree-wide "nothing imports or
    // mounts it" guard). There is no per-thread setting left to display or switch.
    //
    // What THIS page's own render can still pin: a header payload that happens to carry a
    // leftover `mode` key (a pre-migration record, or a stale server response) is simply
    // ignored — never read into any control — rather than the page tripping over an unexpected
    // field.
    h.getBuild.mockResolvedValue({ id: 'thread-1', mode: 'ask', messages: [] })
    renderThread()

    // Liveness: the composer actually mounted (a crash would leave nothing here to query).
    await screen.findByPlaceholderText(/describe what you need/i)
    // No mode control of any name mounted.
    expect(screen.queryByRole('button', { name: /Mode:/i })).toBeNull()
    expect(screen.queryByText(/Mode:/i)).toBeNull()
  })
})

describe('R8 live clause — a reload MID-TURN re-attaches to the running reply', () => {
  it('re-subscribes to the running turn and lands its text in the transcript', async () => {
    // `getBuild` already returned `activeTurn` and nothing consumed it: the reload showed a
    // transcript frozen at the user's message while the server kept generating, and the next
    // send 409'd against a turn this tab had forgotten.
    h.getBuild.mockResolvedValue({
      id: 'thread-1',
      mode: 'ask',
      activeTurn: { turnId: 't-live', lastSeq: 4 },
      messages: [{ id: 'm0', role: 'user', seq: 0, parts: [{ type: 'text', text: 'what does this app do?' }] }],
    })
    h.readTurnStream.mockImplementation(
      turnStreaming([
        { type: 'snapshot', seq: 4, turnId: 't-live', turnStatus: 'running', textSoFar: 'It tracks visitor', items: [], steps: [] },
        { type: 'text_delta', seq: 5, text: ' passes.' },
        { type: 'turn_ended', seq: 6, turnId: 't-live', status: 'completed' },
      ]),
    )
    renderThread()

    // The snapshot's textSoFar plus the tail — the whole reply, not just what arrived after.
    expect(await screen.findByText(/It tracks visitor passes\./)).toBeTruthy()
    const [args] = h.readTurnStream.mock.calls[0]
    expect(args.conversationId).toBe('thread-1')
    expect(args.turnId).toBe('t-live')
    // Cursor-0 on purpose: `lastSeq` counts frames this tab never saw, so replaying from it
    // would silently drop the prefix the snapshot is carrying.
    expect(args.cursor).toBe(0)
  })

  it('does not re-subscribe when no turn is running', async () => {
    h.getBuild.mockResolvedValue({ id: 'thread-1', activeTurn: null, messages: [] })
    renderThread()

    // Liveness: the composer mounted — this used to wait on the now-retired mode pill, which
    // served the same "hydration settled" role; see the U13-header guard above for why it's gone.
    await screen.findByPlaceholderText(/describe what you need/i)
    await waitForGateOpen()
    expect(h.readTurnStream).not.toHaveBeenCalled()
  })
})
