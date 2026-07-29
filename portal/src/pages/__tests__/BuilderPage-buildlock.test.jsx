/**
 * The advisory build lock, seen from the page (KTD-7).
 *
 * `buildLock` is now the FAST cross-tab UX pre-check only — the authoritative one-build-per-user
 * barrier is C3 start's 409 (tested in BuilderPage-session.test.jsx). Here we pin that BuilderPage
 * still consults `blockedBy` before it starts (a second builder chat in the same project is warned
 * before it starts), that a different project is not blocked, that the claim is released when the
 * build ends, and that a planning chat is never blocked. Each BuilderPage owns its own manager over
 * the shared BroadcastChannel, so these two-page tests genuinely travel the wire.
 *
 * The pre-check hangs off the BRIEF CARD's confirmation, not off Send (003-U4). A send is just a
 * chat turn, and refusing to let someone TALK to the assistant because another tab is building
 * would be nonsense — refusing them a SECOND BUILD is the rule. So the warning lands on the card
 * (`role="alert"` inside `plan-options-card`), which re-arms as "Try again"; it is not a toast.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, waitFor, act, cleanup, within } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import {
  FakeEventSource, makeClient, primeClient, statusResp, ENDED,
  PLAN_CARD_ID, planReply, primeTurn, turnStreaming,
  waitForGateOpen,
} from './_builderSession.jsx'

const h = vi.hoisted(() => ({
  sendMessage: vi.fn(),
  startTurn: vi.fn(), readTurnStream: vi.fn(), buildFromPlan: vi.fn(),
  switchMode: vi.fn(), resolvePlanOptions: vi.fn(),
  loadBuilds: vi.fn(), newBuild: vi.fn(), createBuild: vi.fn(), getBuild: vi.fn(),
  deleteBuild: vi.fn(), listProjectConversations: vi.fn(), buildUserParts: vi.fn(),
  planLoadHistory: vi.fn(), planNewConversation: vi.fn(), planCreateConversation: vi.fn(),
  planGetConversation: vi.fn(), planDeleteConversation: vi.fn(),
  start: vi.fn(), stop: vi.fn(), getStatus: vi.fn(), forceEnd: vi.fn(),
  acquireLock: vi.fn(), renewLock: vi.fn(), releaseLock: vi.fn(), heartbeat: vi.fn(),
}))

// The relay serves BOTH pages here: BuilderPage's interview turns (which return the brief card) and
// ChatPage's planning turns.
vi.mock('../../hooks/useClaudeAPI', () => ({
  useClaudeAPI: () => ({ sendMessage: h.sendMessage, error: null, clearError: vi.fn(), abort: vi.fn() }),
  getContextLimits: () => ({ soft: 1e9, hard: 1e9 }),
  estimateConversationTokens: () => 0,
}))
vi.mock('../../utils/turnStreamApi', async (orig) => ({
  ...(await orig()),
  startTurn: (...a) => h.startTurn(...a),
  readTurnStream: (...a) => h.readTurnStream(...a),
  buildFromPlan: (...a) => h.buildFromPlan(...a),
  switchMode: (...a) => h.switchMode(...a),
  resolvePlanOptions: (...a) => h.resolvePlanOptions(...a),
}))
vi.mock('../../utils/builderHistory', () => ({
  loadBuilds: h.loadBuilds, newBuild: h.newBuild, createBuild: h.createBuild,
  getBuild: h.getBuild, deleteBuild: h.deleteBuild, deriveTitle: (t) => (t || '').slice(0, 40),
}))
vi.mock('../../utils/conversationApi', () => ({ listProjectConversations: h.listProjectConversations }))
vi.mock('../../utils/chatHistory', () => ({
  relativeTime: () => 'now', deriveTitle: (t) => (t || '').slice(0, 40),
  loadHistory: h.planLoadHistory, newConversation: h.planNewConversation,
  createConversation: h.planCreateConversation, getConversation: h.planGetConversation, deleteConversation: h.planDeleteConversation,
}))
vi.mock('../../components/layout/Navbar', () => ({ default: () => null }))
vi.mock('../../components/LivePreview', () => ({ default: () => null }))
vi.mock('../../utils/attachmentStore', async (orig) => ({ ...(await orig()), buildUserParts: h.buildUserParts }))

import BuilderPage from '../BuilderPage'
import ChatPage from '../ChatPage'

function renderBuilder(chatId, projectId = 'p1') {
  const fake = new FakeEventSource(chatId)
  const deps = { client: makeClient(h), eventSourceFactory: () => fake }
  const view = render(
    <MemoryRouter initialEntries={[`/chat/${chatId}`]}>
      <Routes>
        <Route path="/chat/:chatId" element={<BuilderPage projectId={projectId} projectName="VIP Movement" buildSessionDeps={deps} />} />
      </Routes>
    </MemoryRouter>,
  )
  return { ...view, fake }
}

/** A chat turn — the model answers with a brief, so a card appears. Starts nothing on its own. */
async function sendFrom(container, text = 'make it blue') {
  await waitForGateOpen()
  const textarea = within(container).getByPlaceholderText(/describe what you need/i)
  fireEvent.change(textarea, { target: { value: text } })
  fireEvent.keyDown(textarea, { key: 'Enter' })
}

/** Confirm the newest brief card — the page's only build trigger. Started cards read "Building…". */
async function confirmBrief(container) {
  const button = await within(container).findByRole('button', { name: /^Build it$/ })
  fireEvent.click(button)
  return button
}

/** The whole user-visible path to a build: ask, get a brief, confirm it. */
async function buildFrom(container, text = 'make it blue') {
  await sendFrom(container, text)
  await confirmBrief(container)
}

/** The card the newest turn produced, i.e. the one a confirmation's error lands on. */
async function lastCard(container) {
  const cards = await within(container).findAllByTestId('plan-options-card')
  return cards[cards.length - 1]
}

// BroadcastChannel delivery is queued on a task. A newly-mounted manager posts a `poll`; the holder
// answers with an `announce`; only after that round-trip does the new tab's `blockedBy` see the
// claim. Drain a few ticks so that handshake completes before the next build.
const flushChannel = () => act(async () => { for (let i = 0; i < 6; i += 1) await new Promise((r) => setTimeout(r, 0)) })

beforeEach(() => {
  vi.clearAllMocks()
  Element.prototype.scrollIntoView = vi.fn()
  primeClient(h)
  h.newBuild.mockReturnValue('build-N')
  h.createBuild.mockResolvedValue({ ok: true })
  h.loadBuilds.mockResolvedValue([])
  h.getBuild.mockImplementation(async (id) => ({ id, kind: 'builder', messages: [] }))
  h.buildUserParts.mockImplementation(async (text) => [{ type: 'text', text }])
  h.planLoadHistory.mockResolvedValue([])
  h.planGetConversation.mockResolvedValue(null)
  h.planCreateConversation.mockResolvedValue({ ok: true })
  h.planNewConversation.mockReturnValue('plan-N')
  h.listProjectConversations.mockResolvedValue([
    { id: 'build-A', kind: 'builder', title: 'First build', updatedAt: new Date().toISOString() },
    { id: 'build-B', kind: 'builder', title: 'Second build', updatedAt: new Date().toISOString() },
  ])
  // Every interview turn answers with a ready-to-build brief, so these suites reach the lock
  // mechanics in one send + one click.
  primeTurn(h)
})
afterEach(() => cleanup())

describe('BuilderPage — one build at a time, per project (advisory pre-check)', () => {
  it('warns a second builder chat in the SAME project before it starts, naming the holder', async () => {
    const a = renderBuilder('build-A')
    await buildFrom(a.container)
    await within(a.container).findByTestId('build-progress') // A's session is live → claim held

    const b = renderBuilder('build-B')
    await within(b.container).findByPlaceholderText(/describe what you need/i)
    await flushChannel() // let B learn about A's claim over the channel
    await buildFrom(b.container, 'and add a table')

    const card = await lastCard(b.container)
    const warning = await within(card).findByRole('alert')
    expect(/already building this project/i.test(warning.textContent)).toBe(true)
    expect(/First build/.test(warning.textContent)).toBe(true) // named the holder, not "some other tab"
    // B never started a build — only A's transition fired.
    expect(h.buildFromPlan).toHaveBeenCalledTimes(1)
  })

  it('does not block a builder chat in a DIFFERENT project', async () => {
    const a = renderBuilder('build-A', 'p1')
    await buildFrom(a.container)
    await within(a.container).findByTestId('build-progress')

    const b = renderBuilder('build-B', 'p2')
    await within(b.container).findByPlaceholderText(/describe what you need/i)
    await flushChannel()
    await buildFrom(b.container, 'different project')

    await waitFor(() => expect(h.buildFromPlan).toHaveBeenCalledTimes(2)) // both started
  })

  it('a second build RE-ACQUIRES the claim — a second chat stays blocked after the refine (finding #23)', async () => {
    const a = renderBuilder('build-A')
    await buildFrom(a.container, 'build it')
    await within(a.container).findByTestId('build-progress')
    expect(h.buildFromPlan).toHaveBeenCalledTimes(1)

    // Refine from A — POST-build now (U16: A's composer is shut while A's agent works, so the
    // refine can only be asked for once the build is over). Ending the build RETRACTS A's claim
    // and the join's reset drops it again transitionally, so the resolved second join must
    // re-assert it — otherwise A's new live session is claim-less and B sails past the check.
    act(() => { a.fake.open(); a.fake.emitEnvelope(ENDED(9)) })
    await within(a.container).findByTestId('build-outcome')
    h.readTurnStream.mockImplementation(turnStreaming(planReply('Make it dark.', 'opt-2')))
    await buildFrom(a.container, 'make it dark mode')
    await waitFor(() => expect(h.buildFromPlan).toHaveBeenCalledTimes(2))
    expect(h.stop).not.toHaveBeenCalled() // nothing live to stop — the terminal already landed
    await within(a.container).findByTestId('build-progress')

    const b = renderBuilder('build-B')
    await within(b.container).findByPlaceholderText(/describe what you need/i)
    await flushChannel() // let B learn about A's re-acquired claim
    await buildFrom(b.container, 'me too')

    const card = await lastCard(b.container)
    expect(/already building this project/i.test((await within(card).findByRole('alert')).textContent)).toBe(true)
    expect(h.buildFromPlan).toHaveBeenCalledTimes(2) // only A's two starts — B never started
  })

  it('a same-project already_built outcome RE-ACQUIRES the claim — a second chat is still warned', async () => {
    // A's transition answers already_built (a second tab beat it); A joins that session via
    // reattach. The reattach passes through reset() (whose transitional no-session state
    // releases the claim), so the resolved join must re-assert it — else A's live session is
    // claim-less and B sails past the advisory pre-check.
    h.buildFromPlan.mockResolvedValue({ outcome: 'already_built', sessionId: 'other-9' })
    h.getStatus.mockResolvedValue(statusResp({ sessionId: 'other-9', projectId: 'p1', status: 'building' }))
    const a = renderBuilder('build-A')
    await buildFrom(a.container)
    await within(a.container).findByTestId('build-progress') // reattached → A's session is live

    const b = renderBuilder('build-B')
    await within(b.container).findByPlaceholderText(/describe what you need/i)
    await flushChannel() // let B learn about A's re-acquired claim
    await buildFrom(b.container, 'me too')

    const card = await lastCard(b.container)
    expect(/already building this project/i.test((await within(card).findByRole('alert')).textContent)).toBe(true)
    expect(h.buildFromPlan).toHaveBeenCalledTimes(1) // only A's transition — B never started
  })

  it('releases the claim when the build ends, so a blocked second chat can then start', async () => {
    const a = renderBuilder('build-A')
    await buildFrom(a.container)
    await within(a.container).findByTestId('build-progress')

    const b = renderBuilder('build-B')
    await within(b.container).findByPlaceholderText(/describe what you need/i)
    await flushChannel()
    await buildFrom(b.container, 'wait for me')
    const card = await lastCard(b.container)
    expect(/already building this project/i.test((await within(card).findByRole('alert')).textContent)).toBe(true)
    expect(h.buildFromPlan).toHaveBeenCalledTimes(1)

    // A's build ends → its advisory claim retracts across the channel. The persisted outcome
    // message is A's terminal signal (the ephemeral status line has no terminal arm — the record
    // says it permanently instead).
    act(() => { a.fake.open(); a.fake.emitEnvelope(ENDED(9)) })
    await within(a.container).findByTestId('build-outcome')
    await flushChannel() // let the retract reach B

    // B's blocked card re-armed as a retry, so the brief it already holds is now buildable.
    fireEvent.click(within(card).getByRole('button', { name: /^Build it$/ }))
    await waitFor(() => expect(h.buildFromPlan).toHaveBeenCalledTimes(2))
    expect(h.buildFromPlan).toHaveBeenLastCalledWith('build-B', PLAN_CARD_ID)
  })
})

describe('ChatPage — a planning chat is never blocked by a build', () => {
  it('sends freely while a build session is live in the same project', async () => {
    const a = renderBuilder('build-A')
    await buildFrom(a.container)
    await within(a.container).findByTestId('build-progress')

    h.listProjectConversations.mockResolvedValue([])
    const plan = render(
      <MemoryRouter initialEntries={['/chat/plan-1?projectId=p1&kind=planning']}>
        <Routes>
          <Route path="/chat/:chatId" element={<ChatPage projectId="p1" projectName="VIP Movement" />} />
        </Routes>
      </MemoryRouter>,
    )
    // Both pages relay through the same mock, so A's interview turn would satisfy a bare
    // toHaveBeenCalled(). Only the planning turn may count.
    h.sendMessage.mockClear()
    h.sendMessage.mockResolvedValue('a plan')
    const textarea = await waitFor(() => plan.container.querySelector('textarea[placeholder*="thinking"]'))
    fireEvent.change(textarea, { target: { value: 'what should this do?' } })
    fireEvent.keyDown(textarea, { key: 'Enter' })

    await waitFor(() => expect(h.sendMessage).toHaveBeenCalled()) // the planning turn never consulted the lock
  })
})
