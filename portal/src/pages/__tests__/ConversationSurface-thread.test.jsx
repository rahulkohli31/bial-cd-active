/**
 * The unified-chat thread behaviors that only the PAGE can prove:
 *  - a send is a chat turn; the plan streams as PROSE beside the card — nothing builds until
 *    the card is clicked;
 *  - a text-only reply (a clarifying question) renders with NO card;
 *  - a restored thread re-renders each card from its STORED state, no local state to resync;
 *  - a used card cannot re-fire: Build it is now a HANDOFF, proven by navigation to a
 *    brand-new build chat, not by a settled local state;
 *  - the in-composer mode switcher is gone now that ChatKind is fixed at creation — see
 *    "the header" below for that guard.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup, within, act } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import {
  FakeEventSource, makeClient, primeClient, BRIEF, PLAN_CARD_ID, primeTurn,
  turnStreaming, textReply,
  waitForGateOpen,
} from './_builderSession.jsx'
import { ApiError } from '../../utils/apiError'

// The id `handleBuildIt` mints for every Build-it press in this file (client-minted via
// `uuidv7`, echoed back as `BuildFromPlanOutcome.chatId`). One fixed id is enough here because
// no test in this file presses Build it twice — see `ConversationSurface-buildlock.test.jsx`
// for the cross-tab suite that needs several handoffs told apart.
const MINTED_BUILD_CHAT_ID = 'minted-build-chat-1'

const h = vi.hoisted(() => ({
  loadBuilds: vi.fn(), getBuild: vi.fn(),
  listProjectConversations: vi.fn(), buildUserParts: vi.fn(), uuidv7: vi.fn(),
  startTurn: vi.fn(), readTurnStream: vi.fn(), buildFromPlan: vi.fn(),
  resolvePlanOptions: vi.fn(),
  stop: vi.fn(), getStatus: vi.fn(),
  relaunchPreview: vi.fn(),
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

const composer = () => screen.getByPlaceholderText(/ask for another change/i)
async function send(text = 'a visitor app') {
  await waitForGateOpen()
  fireEvent.change(composer(), { target: { value: text } })
  fireEvent.keyDown(composer(), { key: 'Enter' })
}

/** A stored plan-options projection message (what a reload hydrates). `PlanOptionsItem` is
 *  `{ type, seq, toolCallId, state }` only now — `mode` and `reason` are both gone. */
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
  h.getBuild.mockResolvedValue(null)
  h.loadBuilds.mockResolvedValue([])
  h.listProjectConversations.mockResolvedValue([])
  h.buildUserParts.mockImplementation(async (text) => [{ type: 'text', text }])
  h.uuidv7.mockReturnValue(MINTED_BUILD_CHAT_ID)
  primeTurn(h)
  // `buildFromPlan` hands off to a NEW chat and echoes the caller's minted id back as `chatId` —
  // the shared harness's `primeTurn` still answers the pre-handoff shape, so tests need this.
  h.buildFromPlan.mockResolvedValue({ outcome: 'started', chatId: MINTED_BUILD_CHAT_ID, turnId: 'bt-1' })
})
afterEach(() => cleanup())

describe('the routing rule — a send is a chat turn, never a build', () => {
  it('streams the plan as prose with the card beside it; nothing builds until the click', async () => {
    renderThread()
    await send('a visitor app')

    expect(await screen.findByText(new RegExp(BRIEF.slice(0, 30)))).toBeTruthy()
    const build = await screen.findByRole('button', { name: /^Build this plan$/ })
    expect(screen.getByRole('button', { name: /keep planning/i })).toBeTruthy()
    expect(h.buildFromPlan).not.toHaveBeenCalled()

    fireEvent.click(build)
    // Three positional args now: the third is the CLIENT-MINTED id of the brand-new build
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
      messages: [
        { id: 'm0', role: 'user', seq: 0, parts: [{ type: 'text', text: 'plan a visitors app' }] },
        storedCard(1, 'opt-old', 'pending'),
        { id: 'm2', role: 'user', seq: 2, parts: [{ type: 'text', text: 'add exports too' }] },
        storedCard(3, 'opt-new', 'pending'),
      ],
    })
    renderThread()

    // FLIPPED: two stored offers used to render as two cards, the older one "expired". There is
    // ONE control now, on the composer, so the newest offer is the only one on screen — a
    // stronger form of "only the newest is actionable": there is no dead button to still try.
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
      messages: [
        storedCard(1, 'opt-a', 'refine'),
        storedCard(2, 'opt-b', 'build'),
      ],
    })
    renderThread()

    // FLIPPED: settled cards used to lose their buttons. A spent strip stays PRESSABLE now —
    // marked by `data-spent`, not by removing the control — because "only one offer is live" is
    // about which one blocks the composer, never about which one a citizen may press.
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
    // AN INERTNESS GUARD, not a deleted test (L8): `build_failed` and its `reason` field are gone
    // from `PlanOptionsItem` — Build-it now fails inside the one handoff call that would have
    // produced this outcome, so there is nothing left to persist.
    //
    // What's left to pin, from a row a pre-migration project might still carry: `build_failed` is
    // not a recognised state, so the strip renders SPENT — it still draws (the liveness half of
    // the guard) but offers no re-arm.
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
    // NOTHING IS EVER `disabled` HERE, which is the assertion that had to change, not the
    // behaviour it guards: a real `disabled` on a focused control blurs it to `document.body`,
    // and an unrecognised stored state simply renders SPENT — pressable, not blocking the composer.
    expect(card.getAttribute('data-spent')).toBe('true')
    expect(card.querySelector('[disabled]')).toBeNull()
  })
})

describe('a used card cannot re-fire', () => {
  it('after Build it succeeds it hands off to a NEW build chat — no second transition from the same card', async () => {
    // "No second transition" no longer means the card settling in place — Build-it now LEAVES
    // this chat for a brand-new one seeded with the plan, so the card is gone because the whole
    // thread is. What's left to pin: exactly one handoff call, carrying the id `handleBuildIt`
    // minted, and the page actually following it.
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

describe('the reload half of the build narrative', () => {
  it('renders stored friendly steps and the in-progress truth line from the projection', async () => {
    h.getBuild.mockResolvedValue({
      id: 'thread-1',
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
    // The page now reattaches to any session the transcript says was running, so "gone-1" has to
    // actually be gone — a 404 is the ordinary way that happens.
    h.getStatus.mockRejectedValue(new ApiError('Build session not found.', 404))
    const { container } = renderThread()

    // The stored step renders through the SAME activity group the live path uses — one
    // converter, one renderer, so a build read back looks like the build watched.
    fireEvent.click(await screen.findByTestId('activity-group-trigger'))
    const step = await screen.findByText('Updated app/page.tsx')
    expect(step.closest('[data-state]')?.getAttribute('data-state')).toBe('ok')
    expect(h.getStatus).toHaveBeenCalledWith('gone-1') // it DID try to rejoin
    // Nothing live re-tells this build, so the durable truth line renders instead of a dead
    // spinner. `build_in_progress` maps to no rendered part, so the surface turns the anchor into
    // prose itself, rather than the transcript simply stopping with no account of the build.
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

    // RE-POINTED AT THE ACTIVITY GROUP: a real chat message still interrupts the run into TWO
    // groups rather than one — the rule that keeps a merge from putting "Step five" above earlier prose.
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

describe('the header ignores a legacy mode field and mounts no mode control', () => {
  it('an inertness guard: no mode control mounts, and a legacy `mode` field on the header is never read', async () => {
    // AN INERTNESS GUARD, not a deleted test (L8): the in-composer switcher and its `mode` label
    // are gone with `ModeSwitcher` (see ModeSwitcher.test.tsx for the tree-wide guard) — there is
    // no per-thread setting left to display or switch. What THIS page's render can still pin: a
    // header payload carrying a leftover `mode` key is simply ignored, never read into a control.
    //
    // KEPT DELIBERATELY: this fixture's `mode: 'ask'` is the one place that key is still the
    // subject, not residue — removing it would leave this test's assertion vacuous.
    h.getBuild.mockResolvedValue({ id: 'thread-1', mode: 'ask', messages: [] })
    renderThread()

    // Liveness: the composer actually mounted (a crash would leave nothing here to query).
    await screen.findByPlaceholderText(/ask for another change/i)
    expect(screen.queryByRole('button', { name: /Mode:/i })).toBeNull()
    expect(screen.queryByText(/Mode:/i)).toBeNull()
  })
})

describe('a reload MID-TURN re-attaches to the running reply', () => {
  it('re-subscribes to the running turn and lands its text in the transcript', async () => {
    // `getBuild` already returned `activeTurn` and nothing consumed it: the reload showed a
    // frozen transcript while the server kept generating, and the next send 409'd against it.
    h.getBuild.mockResolvedValue({
      id: 'thread-1',
      activeTurn: { turnId: 't-live', lastSeq: 4 },
      messages: [{ id: 'm0', role: 'user', seq: 0, parts: [{ type: 'text', text: 'what does this app do?' }] }],
    })
    h.readTurnStream.mockImplementation(
      turnStreaming([
        { type: 'snapshot', seq: 4, turnId: 't-live', turnStatus: 'running', parts: [{ type: 'text', text: 'It tracks visitor' }], working: false, items: [] },
        { type: 'text_delta', seq: 5, text: ' passes.', newBlock: false },
        { type: 'turn_ended', seq: 6, turnId: 't-live', status: 'completed' },
      ]),
    )
    renderThread()

    // The snapshot's own block plus the tail that CONTINUES it — the whole reply, not two
    // paragraphs where the model wrote one. `newBlock: false` says the delta belongs to it.
    expect(await screen.findByText(/It tracks visitor passes\./)).toBeTruthy()
    const [args] = h.readTurnStream.mock.calls[0]
    expect(args.conversationId).toBe('thread-1')
    expect(args.turnId).toBe('t-live')
    // Cursor-0 on purpose: `lastSeq` counts frames this tab never saw, so replaying from it
    // would silently drop the prefix the snapshot is carrying.
    expect(args.cursor).toBe(0)
  })

  it('tells the re-attached turn ONCE, prose included — and still does after it ENDS', async () => {
    // The reload hydrates the turn's persisted rows AND re-tells the same turn into a fresh
    // streaming message; prose beside a tool call is stored now, so the stored copy and the
    // re-told copy are both on screen unless in-flight suppression covers text too.
    //
    // ★ THE TURN IS RUN TO ITS TERMINAL HERE — the point of the fixture: the old suppression
    // switched off at `turn_ended` while the re-told message stayed, so the second copy came
    // back permanently. Asserting mid-stream could never catch that.
    h.getBuild.mockResolvedValue({
      id: 'thread-1',
      activeTurn: { turnId: 't-live', lastSeq: 3 },
      messages: [
        { id: 'srv_1_u_0', role: 'user', seq: 1, parts: [{ type: 'text', text: 'add a page' }] },
        // ★ THE EQUAL CASE: the boundary IS the user row's seq, so every other stored row sits
        // STRICTLY above it, and `>` vs `>=` cannot be told apart by them. A stored agent row
        // sharing that seq is the one row the difference decides.
        { id: 'srv_1_a_0', role: 'assistant', seq: 1, parts: [{ type: 'text', text: 'Reading the page first.' }] },
        { id: 'srv_2_a_0', role: 'assistant', seq: 2, parts: [{ type: 'text', text: 'Let me look at the page.' }] },
      ],
    })
    h.readTurnStream.mockImplementation(
      turnStreaming([
        {
          type: 'snapshot',
          seq: 3,
          turnId: 't-live',
          turnStatus: 'running',
          parts: [
            { type: 'text', text: 'Reading the page first.' },
            { type: 'text', text: 'Let me look at the page.' },
          ],
          working: false,
          items: [],
        },
        // The tail exists ONLY in the re-telling, so waiting for it makes the count below
        // deterministic — `findAllByText` alone resolves on the FIRST (stored) match, which is
        // why this assertion used to pass or fail depending on what else ran.
        { type: 'text_delta', seq: 4, text: ' Adding it now.', newBlock: false },
        { type: 'turn_ended', seq: 5, turnId: 't-live', status: 'completed' },
      ]),
    )
    renderThread()

    await screen.findByText(/Let me look at the page\. Adding it now\./)
    // Once, not twice — and `getAllByText` rather than `getByText` so the assertion is about the
    // COUNT: `getByText` throws on multiple matches, which reads as a broken query.
    expect(screen.getAllByText(/Let me look at the page\./)).toHaveLength(1)
    // ★ INCLUDING THE ROW AT EXACTLY THE BOUNDARY: the re-telling carries this sentence too, so
    // the stored copy AT the boundary seq is a second copy like any other — a boundary drawn as
    // `>` rather than `>=` would leave it on screen beside its own re-telling.
    expect(screen.getAllByText(/Reading the page first\./)).toHaveLength(1)
    // ★ AND THE CITIZEN'S OWN MESSAGE SURVIVED IT: the stored user row sits at exactly the
    // boundary seq, so the suppression could have deleted it — nothing re-tells a prompt, so
    // this is the assertion that tells de-duplication apart from loss.
    expect(screen.getByText('add a page')).toBeTruthy()
  })

  it('draws the working status BELOW prose already on screen, not above it', async () => {
    // A build thinks again between tool calls (adaptive thinking), so `working` goes true again
    // after a paragraph is already on screen. The status row used to be pinned to index 0,
    // pushing read text down the screen. Asserted on ORDER, because presence passes either way.
    h.getBuild.mockResolvedValue({
      id: 'thread-1',
      activeTurn: { turnId: 't-live', lastSeq: 0 },
      messages: [{ id: 'm0', role: 'user', seq: 0, parts: [{ type: 'text', text: 'build it' }] }],
    })
    h.readTurnStream.mockImplementation(async ({ onFrame }) => {
      onFrame({ type: 'snapshot', seq: 1, turnId: 't-live', turnStatus: 'running', parts: [], working: false, items: [] })
      onFrame({ type: 'text_delta', seq: 2, text: 'Let me look at the page.', newBlock: true })
      onFrame({ type: 'working', seq: 3, working: true })
      return new Promise(() => {})
    })
    const { container } = renderThread()

    await screen.findByText(/Let me look at the page\./)
    await screen.findByTestId('working-status')
    const rendered = Array.from(
      container.querySelectorAll('[data-testid="assistant-message"] p, [data-testid="working-status"]'),
    ).map((node) => node.getAttribute('data-testid') ?? node.textContent)
    expect(rendered).toEqual(['Let me look at the page.', 'working-status'])
  })

  it('stops saying the agent is working when the stream dies without a terminal', async () => {
    // A dropped connection during a reasoning burst leaves the reader without a `turn_ended`
    // frame — the one arm that ever cleared the status. Nothing re-reads the transcript
    // afterward, so a stale "Working on your app" would sit above the next message sent.
    h.getBuild.mockResolvedValue({
      id: 'thread-1',
      activeTurn: { turnId: 't-live', lastSeq: 1 },
      messages: [{ id: 'srv_1_u_0', role: 'user', seq: 1, parts: [{ type: 'text', text: 'add a page' }] }],
    })
    // HELD OPEN, then dropped: the turn must be genuinely mid-burst when the connection dies,
    // since the flag is only stale if set while the reader was alive.
    let dropTheConnection
    h.readTurnStream.mockImplementation(async ({ onFrame }) => {
      onFrame({ type: 'snapshot', seq: 2, turnId: 't-live', turnStatus: 'running', parts: [{ type: 'text', text: 'Let me look at the page.' }], working: false, items: [] })
      onFrame({ type: 'working', seq: 3, working: true })
      await new Promise((resolve) => { dropTheConnection = resolve })
      // …and it dies there: no `turn_ended`, no reason, nothing more to read.
      return 'stalled'
    })
    renderThread()

    // The turn is on screen and VISIBLY thinking before anything is asserted about how it ends —
    // otherwise the absence below would also pass on a surface that never drew the status at all.
    await screen.findByText(/Let me look at the page\./)
    await screen.findByTestId('working-status')
    await act(async () => dropTheConnection())

    // ASSERTED ON THE SETTLED SHAPE, synchronously after the flush: the banner is written on the
    // exit that clears the status, so its presence proves the stale-status repaint already landed.
    expect(screen.getByText(/The reply stalled\. Reload to catch up\./)).toBeTruthy()
    expect(screen.queryByTestId('working-status')).toBeNull()
    // LIVENESS, because an empty transcript would also have no status row: what the turn DID say
    // is still on screen under the banner.
    expect(screen.getByText(/Let me look at the page\./)).toBeTruthy()
  })

  it('does not re-subscribe when no turn is running', async () => {
    h.getBuild.mockResolvedValue({ id: 'thread-1', activeTurn: null, messages: [] })
    renderThread()

    // Liveness: the composer mounted — this used to wait on the now-retired mode pill, which
    // served the same "hydration settled" role; see the header guard above for why it's gone.
    await screen.findByPlaceholderText(/ask for another change/i)
    await waitForGateOpen()
    expect(h.readTurnStream).not.toHaveBeenCalled()
  })
})
