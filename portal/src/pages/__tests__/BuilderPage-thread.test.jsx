/**
 * The project thread: interview + build in ONE transcript (003-U4).
 *
 * This suite owns THE ROUTING RULE, the load-bearing decision of this page: every composer send
 * is a chat turn, and a build starts only from a confirmed brief card — first build and iteration
 * alike. The retired behaviour (send fires a build directly) is what let the agent silently guess
 * at a vague prompt and build the wrong app, so "a send did not start a build" is not a detail
 * here; it is the feature.
 *
 * The other suites (`-session`, `-buildlock`, `-persistence`, `-projectfirst`, `-visibility`) pin
 * the session mechanics BEHIND the card. This one pins what reaches the card.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act, cleanup } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import {
  FakeEventSource, makeClient, primeClient, BRIEF, briefReply, relayReplying, PREVIEW,
} from './_builderSession.jsx'

const h = vi.hoisted(() => ({
  loadBuilds: vi.fn(), newBuild: vi.fn(), appendBuilderMessage: vi.fn(), getBuild: vi.fn(),
  deleteBuild: vi.fn(), listProjectConversations: vi.fn(), buildUserParts: vi.fn(),
  sendMessage: vi.fn(),
  start: vi.fn(), stop: vi.fn(), getStatus: vi.fn(), forceEnd: vi.fn(),
  acquireLock: vi.fn(), renewLock: vi.fn(), releaseLock: vi.fn(), heartbeat: vi.fn(),
}))

vi.mock('../../utils/builderHistory', () => ({
  loadBuilds: h.loadBuilds, newBuild: h.newBuild, appendBuilderMessage: h.appendBuilderMessage,
  getBuild: h.getBuild, deleteBuild: h.deleteBuild, deriveTitle: (t) => (t || '').slice(0, 40),
}))
vi.mock('../../utils/conversationApi', () => ({ listProjectConversations: h.listProjectConversations }))
vi.mock('../../utils/chatHistory', () => ({ relativeTime: () => 'now' }))
vi.mock('../../components/layout/Navbar', () => ({ default: () => null }))
vi.mock('../../components/LivePreview', () => ({ default: () => null }))
vi.mock('../../components/AttachmentChips', () => ({ default: () => null }))
vi.mock('../../utils/attachmentStore', async (orig) => ({ ...(await orig()), buildUserParts: h.buildUserParts }))
vi.mock('../../hooks/useClaudeAPI', () => ({
  useClaudeAPI: () => ({ sendMessage: h.sendMessage, error: null }),
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
        <Route path="/projects" element={<div>projects index</div>} />
      </Routes>
    </MemoryRouter>,
  )
  return { ...view, fake }
}

const composer = () => screen.getByPlaceholderText(/describe what you need/i)
function send(text) {
  fireEvent.change(composer(), { target: { value: text } })
  fireEvent.keyDown(composer(), { key: 'Enter' })
}

beforeEach(() => {
  vi.clearAllMocks()
  Element.prototype.scrollIntoView = vi.fn()
  primeClient(h)
  h.appendBuilderMessage.mockResolvedValue({ ok: true })
  h.getBuild.mockResolvedValue(null)
  h.loadBuilds.mockResolvedValue([])
  h.listProjectConversations.mockResolvedValue([])
  h.buildUserParts.mockImplementation(async (text) => [{ type: 'text', text }])
  h.sendMessage.mockImplementation(relayReplying(briefReply()))
})
afterEach(cleanup)

describe('the routing rule — a send is a chat turn, never a build', () => {
  it('sends an interview turn to the relay and starts NOTHING', async () => {
    h.sendMessage.mockImplementation(relayReplying(QUESTIONS))
    renderThread()

    send('I need a visitor app')

    expect(await screen.findByText(QUESTIONS)).toBeTruthy()
    // The whole point: on a vague prompt the agent ASKS. It does not quietly build its guess.
    expect(h.start).not.toHaveBeenCalled()
    expect(screen.queryByTestId('build-brief-card')).toBeNull()
  })

  it('sends the thread conversationId so the server folds in the project context + protocol', async () => {
    h.sendMessage.mockImplementation(relayReplying(QUESTIONS))
    renderThread()

    send('I need a visitor app')

    await waitFor(() => expect(h.sendMessage).toHaveBeenCalled())
    // 4th arg is the conversationId — REQUIRED by the relay; without it the turn 400s and the
    // interview protocol never gets appended.
    expect(h.sendMessage.mock.calls[0][3]).toBe('thread-1')
  })

  it('persists the user turn BEFORE streaming, so the relay can resolve the row', async () => {
    const order = []
    h.appendBuilderMessage.mockImplementation(async () => { order.push('append'); return { ok: true } })
    h.sendMessage.mockImplementation(async (...args) => { order.push('stream'); return relayReplying(QUESTIONS)(...args) })
    renderThread()

    send('I need a visitor app')

    await waitFor(() => expect(h.sendMessage).toHaveBeenCalled())
    // The append upserts the header AND inserts the message, so the conversation exists when
    // POST /v1/claude looks it up. Reverse this and the FIRST turn of a thread silently loses
    // its project description and its interview protocol.
    expect(order.slice(0, 2)).toEqual(['append', 'stream'])
  })

  it('renders the brief as a card and hides the raw fence', async () => {
    renderThread()

    send('a visitor app with names, badge numbers and a host')

    expect(await screen.findByTestId('build-brief-card')).toBeTruthy()
    expect(screen.getByText(BRIEF)).toBeTruthy()
    // The fence is machinery, not content — a citizen developer must never see it.
    expect(screen.queryByText(/bial:build-brief/)).toBeNull()
    expect(screen.queryByText(/```/)).toBeNull()
    // Still nothing built: the card is a proposal.
    expect(h.start).not.toHaveBeenCalled()
  })

  it('starts the build with the refined brief only once the card is confirmed', async () => {
    renderThread()
    send('a visitor app with names, badge numbers and a host')
    await screen.findByTestId('build-brief-card')

    fireEvent.click(screen.getByRole('button', { name: /build this/i }))

    await waitFor(() =>
      expect(h.start).toHaveBeenCalledWith({ projectId: 'p1', prompt: BRIEF, conversationId: 'thread-1' }),
    )
  })

  it('keeps an assistant turn that carries no fence — a reply is never lost', async () => {
    h.sendMessage.mockImplementation(relayReplying('Sure — visitor passes are a good fit for this.'))
    renderThread()

    send('will this work?')

    expect(await screen.findByText(/visitor passes are a good fit/i)).toBeTruthy()
  })
})

describe('iteration goes down the same path (R5)', () => {
  it('a post-build follow-up is a relay turn, and its card drives the refine', async () => {
    const { fake } = renderThread()
    send('a visitor app')
    fireEvent.click(await screen.findByRole('button', { name: /build this/i }))
    await waitFor(() => expect(h.start).toHaveBeenCalled())
    act(() => { fake.open(); fake.emitEnvelope(PREVIEW(3)) })

    h.start.mockClear()
    h.sendMessage.mockImplementation(relayReplying(briefReply('Build it, now with a chart.')))
    send('add a chart')

    // The follow-up went to the assistant — the live build was NOT torn down on the send.
    await waitFor(() => expect(h.sendMessage).toHaveBeenCalledTimes(2))
    expect(h.stop).not.toHaveBeenCalled()
    expect(h.start).not.toHaveBeenCalled()

    // …and the card is what performs the stop+start refine.
    fireEvent.click(await screen.findByRole('button', { name: /rebuild with these changes/i }))
    await waitFor(() => expect(h.stop).toHaveBeenCalled())
    await waitFor(() =>
      expect(h.start).toHaveBeenCalledWith({ projectId: 'p1', prompt: 'Build it, now with a chart.', conversationId: 'thread-1' }),
    )
  })

  it('a card cannot re-fire its build once used', async () => {
    renderThread()
    send('a visitor app')
    fireEvent.click(await screen.findByRole('button', { name: /build this/i }))
    await waitFor(() => expect(h.start).toHaveBeenCalledTimes(1))

    // The card stays in the transcript forever; without this guard a user could scroll up and
    // restart an old brief over a live build.
    const action = screen.getByRole('button', { name: /building/i })
    expect(action.disabled).toBe(true)
    fireEvent.click(action)
    expect(h.start).toHaveBeenCalledTimes(1)
  })
})

describe('the handed-off prompt', () => {
  it('fires as the thread’s first relay turn — not as a build', async () => {
    h.sendMessage.mockImplementation(relayReplying(QUESTIONS))
    renderThread({ state: { prompt: 'I need a visitor app', theme: 'bial', pendingAttachments: [] } })

    // The project composer's Generate lands here. It must still run the interview: the user
    // typed one sentence, which is exactly the case that needs asking.
    await waitFor(() => expect(h.sendMessage).toHaveBeenCalled())
    expect(await screen.findByText(QUESTIONS)).toBeTruthy()
    expect(h.start).not.toHaveBeenCalled()
  })

  it('is sent once, not once per render', async () => {
    h.sendMessage.mockImplementation(relayReplying(QUESTIONS))
    const { rerender } = renderThread({ state: { prompt: 'I need a visitor app', pendingAttachments: [] } })
    await waitFor(() => expect(h.sendMessage).toHaveBeenCalledTimes(1))

    rerender(
      <MemoryRouter initialEntries={[{ pathname: '/chat/thread-1', state: { prompt: 'I need a visitor app', pendingAttachments: [] } }]}>
        <Routes>
          <Route path="/chat/:chatId" element={<BuilderPage projectId="p1" buildSessionDeps={{ client: makeClient(h), eventSourceFactory: () => new FakeEventSource('x') }} />} />
        </Routes>
      </MemoryRouter>,
    )

    expect(h.sendMessage).toHaveBeenCalledTimes(1)
  })
})

describe('restoring a thread', () => {
  it('re-renders the build card from the persisted transcript, so a reload can still build', async () => {
    // The fence lives in the persisted assistant text and is parsed at RENDER — that is what
    // makes a restored thread behave like a live one, with no extra persisted state to sync.
    h.getBuild.mockResolvedValue({
      id: 'thread-1',
      messages: [
        { id: 'm0', role: 'user', parts: [{ type: 'text', text: 'a visitor app' }], seq: 0 },
        { id: 'm1', role: 'assistant', parts: [{ type: 'text', text: briefReply() }], seq: 1 },
      ],
    })
    renderThread()

    expect(await screen.findByTestId('build-brief-card')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: /build this/i }))
    await waitFor(() =>
      expect(h.start).toHaveBeenCalledWith({ projectId: 'p1', prompt: BRIEF, conversationId: 'thread-1' }),
    )
  })

  it('continues the transcript from the highest persisted seq, never the array length', async () => {
    // A gap (a failed append, a pruned turn) makes length ≠ next seq; minting from length would
    // collide with an existing turn and the server would idempotently swallow the write — the
    // message would just vanish.
    h.getBuild.mockResolvedValue({
      id: 'thread-1',
      messages: [
        { id: 'm0', role: 'user', parts: [{ type: 'text', text: 'hi' }], seq: 0 },
        { id: 'm1', role: 'assistant', parts: [{ type: 'text', text: 'hello' }], seq: 5 },
      ],
    })
    h.sendMessage.mockImplementation(relayReplying(QUESTIONS))
    renderThread()
    await screen.findByText('hello')

    send('a visitor app')

    await waitFor(() => expect(h.appendBuilderMessage).toHaveBeenCalled())
    expect(h.appendBuilderMessage.mock.calls[0][1].seq).toBe(6)
  })

  it('seeds a welcome bubble on an empty thread, and never sends it to the model', async () => {
    h.getBuild.mockResolvedValue({ id: 'thread-1', messages: [] })
    h.sendMessage.mockImplementation(relayReplying(QUESTIONS))
    renderThread()
    expect(await screen.findByText(/Tell me what you'd like to build/i)).toBeTruthy()

    send('a visitor app')

    await waitFor(() => expect(h.sendMessage).toHaveBeenCalled())
    // The greeting is chrome, not a turn: replaying it as history would have the model
    // answering its own hello.
    const sent = h.sendMessage.mock.calls[0][0]
    expect(sent).toHaveLength(1)
    expect(JSON.stringify(sent)).not.toContain('Citizen Developer AI')
  })
})

describe('failure surfaces', () => {
  it('drops the empty bubble when the relay turn fails', async () => {
    h.sendMessage.mockResolvedValue(null) // useClaudeAPI's failure contract (429 / network / abort)
    renderThread()

    send('a visitor app')

    await waitFor(() => expect(h.sendMessage).toHaveBeenCalled())
    // No blank assistant bubble left behind, and nothing persisted for it.
    await waitFor(() => expect(h.appendBuilderMessage).toHaveBeenCalledTimes(1)) // the user turn only
    expect(screen.queryByTestId('build-brief-card')).toBeNull()
  })

  it('re-arms the card with a retry when the start fails', async () => {
    h.start.mockRejectedValue(new Error('nope'))
    renderThread()
    send('a visitor app')
    fireEvent.click(await screen.findByRole('button', { name: /build this/i }))

    // A dead card would strand the user holding a brief with no way to build it.
    const retry = await screen.findByRole('button', { name: /try again/i })
    expect(retry.disabled).toBe(false)
    expect(screen.getByRole('alert')).toBeTruthy()
  })
})
