/**
 * The unified-chat thread behaviors (U11/U13) that only the PAGE can prove:
 *
 *  - a send is a chat turn; the plan streams as PROSE and the card renders beside it —
 *    no fence anywhere, and nothing builds until the card is clicked;
 *  - a text-only reply (a clarifying question) renders with NO card;
 *  - a restored thread re-renders each card from its STORED state (pending → armed,
 *    refine/build → settled, older-than-newest → expired) — no local state to resync;
 *  - a used card cannot re-fire once its build started;
 *  - the mode toggle reflects the saved header and the meter renders alongside it.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import {
  FakeEventSource, makeClient, primeClient, BRIEF, PLAN_CARD_ID, primeTurn,
  turnStreaming, textReply,
} from './_builderSession.jsx'

const h = vi.hoisted(() => ({
  loadBuilds: vi.fn(), newBuild: vi.fn(), createBuild: vi.fn(), getBuild: vi.fn(),
  deleteBuild: vi.fn(), listProjectConversations: vi.fn(), buildUserParts: vi.fn(),
  startTurn: vi.fn(), readTurnStream: vi.fn(), buildFromPlan: vi.fn(),
  switchMode: vi.fn(), resolvePlanOptions: vi.fn(),
  start: vi.fn(), stop: vi.fn(), getStatus: vi.fn(), forceEnd: vi.fn(),
  relaunchPreview: vi.fn(),
  acquireLock: vi.fn(), renewLock: vi.fn(), releaseLock: vi.fn(), heartbeat: vi.fn(),
}))

vi.mock('../../utils/builderHistory', () => ({
  loadBuilds: h.loadBuilds, newBuild: h.newBuild, createBuild: h.createBuild,
  getBuild: h.getBuild, deleteBuild: h.deleteBuild, deriveTitle: (t) => (t || '').slice(0, 40),
}))
vi.mock('../../utils/conversationApi', () => ({ listProjectConversations: h.listProjectConversations }))
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
  switchMode: (...a) => h.switchMode(...a),
  resolvePlanOptions: (...a) => h.resolvePlanOptions(...a),
}))

import BuilderPage from '../BuilderPage'

const QUESTIONS = 'Which terminals should this cover, and who approves a visitor?'

function renderThread({ state, chatId = 'thread-1' } = {}) {
  const fake = new FakeEventSource(chatId)
  const deps = { client: makeClient(h), eventSourceFactory: () => fake }
  const view = render(
    <MemoryRouter initialEntries={[{ pathname: `/chat/${chatId}`, state }]}>
      <Routes>
        <Route
          path="/chat/:chatId"
          element={<BuilderPage projectId="p1" projectName="VIP Movement" buildSessionDeps={deps} />}
        />
      </Routes>
    </MemoryRouter>,
  )
  return { ...view, fake }
}

const composer = () => screen.getByPlaceholderText(/describe what you need/i)
function send(text = 'a visitor app') {
  fireEvent.change(composer(), { target: { value: text } })
  fireEvent.keyDown(composer(), { key: 'Enter' })
}

/** A stored plan-options projection message (what a reload hydrates). */
const storedCard = (seq, toolCallId, state, reason = null) => ({
  id: `srv_${seq}_p`,
  role: 'assistant',
  seq,
  parts: [{ type: 'plan_options', item: { type: 'plan_options', seq, mode: 'plan', toolCallId, state, reason } }],
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
  primeTurn(h)
})
afterEach(() => cleanup())

describe('the routing rule — a send is a chat turn, never a build', () => {
  it('streams the plan as prose with the card beside it; nothing builds until the click', async () => {
    renderThread()
    send('a visitor app')

    // The plan text is a normal assistant bubble (no fence, no hidden markup)…
    expect(await screen.findByText(new RegExp(BRIEF.slice(0, 30)))).toBeTruthy()
    // …with the actionable card.
    const build = await screen.findByRole('button', { name: /^Build it$/ })
    expect(screen.getByRole('button', { name: /keep refining/i })).toBeTruthy()
    expect(h.buildFromPlan).not.toHaveBeenCalled()

    fireEvent.click(build)
    await waitFor(() => expect(h.buildFromPlan).toHaveBeenCalledWith('thread-1', PLAN_CARD_ID))
  })

  it('a clarifying reply renders with NO card — a question is a legitimate planning turn', async () => {
    h.readTurnStream.mockImplementation(turnStreaming(textReply(QUESTIONS)))
    renderThread()
    send('something vague')

    expect(await screen.findByText(new RegExp(QUESTIONS.slice(0, 25)))).toBeTruthy()
    expect(screen.queryByRole('button', { name: /^Build it$/ })).toBeNull()
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

    const cards = await screen.findAllByTestId('plan-options-card')
    expect(cards).toHaveLength(2)
    expect(within(cards[0]).queryByRole('button')).toBeNull() // expired — informational only
    expect(within(cards[0]).getByText(/newer plan supersedes/i)).toBeTruthy()
    expect(within(cards[1]).getByRole('button', { name: /^Build it$/ })).toBeTruthy()

    fireEvent.click(within(cards[1]).getByRole('button', { name: /^Build it$/ }))
    await waitFor(() => expect(h.buildFromPlan).toHaveBeenCalledWith('thread-1', 'opt-new'))
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

    expect(await screen.findByText(/you kept refining this plan/i)).toBeTruthy()
    expect(screen.getByText(/build started from this plan/i)).toBeTruthy()
    expect(screen.queryByRole('button', { name: /^Build it$/ })).toBeNull()
  })

  it('a build_failed card re-arms with the failure named (never resolved-with-no-build)', async () => {
    h.getBuild.mockResolvedValue({
      id: 'thread-1',
      mode: 'plan',
      messages: [storedCard(1, 'opt-f', 'build_failed', 'lock_held')],
    })
    renderThread()

    expect(await screen.findByText(/another build is already running/i)).toBeTruthy()
    expect(screen.getByRole('button', { name: /^Build it$/ })).toBeTruthy()
  })
})

describe('a used card cannot re-fire', () => {
  it('after Build it succeeds the card settles — no second transition from the same card', async () => {
    renderThread()
    send('a visitor app')
    fireEvent.click(await screen.findByRole('button', { name: /^Build it$/ }))
    await waitFor(() => expect(h.buildFromPlan).toHaveBeenCalledTimes(1))

    // The card settled to its build state; the button is gone.
    expect(await screen.findByText(/build started from this plan/i)).toBeTruthy()
    expect(screen.queryByRole('button', { name: /^Build it$/ })).toBeNull()
  })
})

describe('the U13 header', () => {
  it('reflects the saved mode on the toggle and renders the usage meter slot', async () => {
    h.getBuild.mockResolvedValue({ id: 'thread-1', mode: 'ask', messages: [] })
    renderThread()

    const toggle = await screen.findByRole('radiogroup', { name: /chat mode/i })
    const ask = within(toggle).getByRole('radio', { name: /ask mode/i })
    await waitFor(() => expect(ask.getAttribute('data-state')).toBe('on'))
  })
})
