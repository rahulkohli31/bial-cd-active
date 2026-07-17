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
import { render, screen, fireEvent, waitFor, act, cleanup, within } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import {
  FakeEventSource, makeClient, primeClient, BRIEF, briefReply, relayReplying, PREVIEW, statusResp,
} from './_builderSession.jsx'
import { BuildSessionAlreadyActiveError } from '../../utils/buildSessionApi'

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

describe('only the newest brief may fire a build', () => {
  // The routing rule has a second half: a send never builds, AND only the brief the thread has
  // actually arrived at may build. Every card lives in the transcript forever, so a superseded
  // brief that keeps its button is one scroll and one click from rebuilding the app off an
  // obsolete spec — and finalize snapshots that straight over the good bundle, with no undo.
  // That is the same wrong-app-silently-built failure the card was introduced to close.
  const STALE = 'Build an app for BIAL that tracks incoming shipments.'
  const LIVE = 'Build an app for BIAL that tracks incoming shipments AND their vendors.'

  it('disables the older card once the thread moves on to a newer brief', async () => {
    h.sendMessage.mockImplementation(relayReplying(briefReply(STALE)))
    renderThread()
    send('track incoming shipments')
    await screen.findByTestId('build-brief-card')

    // The user did NOT confirm the first brief — they refined it. That is the ordinary path.
    h.sendMessage.mockImplementation(relayReplying(briefReply(LIVE)))
    send('also track the vendors')
    await waitFor(() => expect(screen.getAllByTestId('build-brief-card')).toHaveLength(2))

    const [stale, live] = screen.getAllByTestId('build-brief-card')
    expect(stale.dataset.superseded).toBe('true')
    expect(live.dataset.superseded).toBe('false')
    expect(within(stale).getByRole('button').disabled).toBe(true)
    expect(within(live).getByRole('button').disabled).toBe(false)
  })

  it('starts nothing when a superseded card is clicked', async () => {
    h.sendMessage.mockImplementation(relayReplying(briefReply(STALE)))
    renderThread()
    send('track incoming shipments')
    await screen.findByTestId('build-brief-card')
    h.sendMessage.mockImplementation(relayReplying(briefReply(LIVE)))
    send('also track the vendors')
    await waitFor(() => expect(screen.getAllByTestId('build-brief-card')).toHaveLength(2))

    fireEvent.click(within(screen.getAllByTestId('build-brief-card')[0]).getByRole('button'))

    // Armed, this fires a real build off STALE and reverts the vendor work the user just asked for.
    expect(h.start).not.toHaveBeenCalled()
  })

  it('re-arms only the newest card on a restored thread, not every card in it', async () => {
    // The worst case, and the one `startedCards` structurally cannot reach: it is per-mount state,
    // so a reload empties it and every historical card comes back armed — including one that has
    // already built. The live guard has to be derived from the transcript to survive this.
    h.getBuild.mockResolvedValue({
      id: 'thread-1',
      messages: [
        { id: 'm0', role: 'user', parts: [{ type: 'text', text: 'track incoming shipments' }], seq: 0 },
        { id: 'm1', role: 'assistant', parts: [{ type: 'text', text: briefReply(STALE) }], seq: 1 },
        { id: 'm2', role: 'user', parts: [{ type: 'text', text: 'also track the vendors' }], seq: 2 },
        { id: 'm3', role: 'assistant', parts: [{ type: 'text', text: briefReply(LIVE) }], seq: 3 },
      ],
    })
    renderThread()

    const cards = await screen.findAllByTestId('build-brief-card')
    expect(cards).toHaveLength(2)
    expect(within(cards[0]).getByRole('button').disabled).toBe(true)
    expect(within(cards[0]).getByRole('button').textContent).toMatch(/replaced by a newer brief/i)
    // …and the brief the thread actually arrived at is still buildable.
    expect(within(cards[1]).getByRole('button').disabled).toBe(false)
    expect(screen.getByText(LIVE)).toBeTruthy()
  })

  it('leaves a lone brief armed — supersede must not disarm the only card there is', async () => {
    h.sendMessage.mockImplementation(relayReplying(briefReply(STALE)))
    renderThread()
    send('track incoming shipments')

    const card = await screen.findByTestId('build-brief-card')
    expect(card.dataset.superseded).toBe('false')
    expect(within(card).getByRole('button').disabled).toBe(false)
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

  it('fires on a thread that ALREADY has turns — every build after the first', async () => {
    // THE REGRESSION. The thread is canonical and permanent, so it is empty exactly once in its
    // life: every "Generate App" from the second build onward arrives at a thread with turns.
    // Consuming the handoff only on the empty branch silently destroyed the user's typed prompt
    // and their attachments — the composer is cleared on adopt, and nothing else reads
    // `location.state.prompt`. No error, no toast, no draft: just gone.
    h.getBuild.mockResolvedValue({
      id: 'thread-1',
      messages: [
        { id: 'm0', role: 'user', parts: [{ type: 'text', text: 'the first app' }], seq: 0 },
        { id: 'm1', role: 'assistant', parts: [{ type: 'text', text: 'built it' }], seq: 1 },
      ],
    })
    h.sendMessage.mockImplementation(relayReplying(QUESTIONS))
    renderThread({ state: { prompt: 'now a totally different app', pendingAttachments: [] } })

    await waitFor(() => expect(h.sendMessage).toHaveBeenCalled())
    expect(await screen.findByText(QUESTIONS)).toBeTruthy()
    // …and it continued the existing transcript rather than replacing it.
    expect(screen.getByText('the first app')).toBeTruthy()
    expect(h.appendBuilderMessage.mock.calls[0][1].seq).toBe(2)
  })

  it('carries the handoff attachments into a non-empty thread', async () => {
    h.getBuild.mockResolvedValue({
      id: 'thread-1',
      messages: [{ id: 'm0', role: 'user', parts: [{ type: 'text', text: 'first' }], seq: 0 }],
    })
    h.sendMessage.mockImplementation(relayReplying(QUESTIONS))
    const files = [{ id: 'a1', name: 'sheet.xlsx', mediaType: 'application/vnd.ms-excel', base64: 'x' }]
    renderThread({ state: { prompt: 'build from this sheet', pendingAttachments: files } })

    await waitFor(() => expect(h.buildUserParts).toHaveBeenCalled())
    // The files the user attached in the composer must reach the turn, not be dropped on the floor.
    expect(h.buildUserParts).toHaveBeenCalledWith('build from this sheet', files)
  })

  it('does not replay on a RELOAD, and does not clobber the transcript it just restored', async () => {
    // A reload is a fresh mount over the SAME history entry, and the browser keeps router state
    // across it — so `initFiredRef` (a ref) cannot stop a replay on its own. Left unstripped, the
    // prompt re-sends every time the user refreshes a thread they were only reading, and the
    // re-fired turn overwrites the restored transcript (the send reads state from the same tick
    // as the restore). Stripping the state from history is what makes the handoff consume-once.
    h.getBuild.mockResolvedValue({
      id: 'thread-1',
      messages: [
        { id: 'm0', role: 'user', parts: [{ type: 'text', text: 'I need a visitor app' }], seq: 0 },
        { id: 'm1', role: 'assistant', parts: [{ type: 'text', text: QUESTIONS }], seq: 1 },
      ],
    })
    // The handoff already fired before the reload, so history no longer carries it.
    renderThread()

    await waitFor(() => expect(h.getBuild).toHaveBeenCalled())
    expect(await screen.findByText(QUESTIONS)).toBeTruthy()
    expect(screen.getByText('I need a visitor app')).toBeTruthy()
    expect(h.sendMessage).not.toHaveBeenCalled() // nothing re-sent
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

describe('nothing on this page can hijack the canonical thread', () => {
  it('offers no way to mint a new build chat', async () => {
    // A project has ONE build thread, so "a new build chat" is not a thing you can make. This
    // is not cosmetic: minting one would make the fresh empty row the project's canonical thread
    // (newest-wins) and orphan the transcript holding the app's whole design history — the exact
    // hijack this plan fixed on the planning chat's handoff. The button lived in this dropdown.
    h.listProjectConversations.mockResolvedValue([
      { id: 'thread-1', kind: 'builder', title: 'My build', updatedAt: new Date().toISOString() },
    ])
    renderThread()
    fireEvent.click(await screen.findByRole('button', { name: /recent/i }))

    await screen.findByText(/recent builds/i)
    expect(screen.queryByRole('button', { name: /\+ new/i })).toBeNull()
    expect(h.newBuild).not.toHaveBeenCalled()
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

  it('a reattach does NOT report this brief as building', async () => {
    // Two tabs, or one reloaded mid-build: this hook is fresh, so `sessionLive` is false, the stop
    // is skipped, and start 409s on the one-per-user lock. The session that answers is a DIFFERENT
    // build — one this brief did not start and whose app does not contain it. Reporting success
    // flipped this card to "Building…" forever, for a build that would never happen, while the
    // cockpit streamed somebody else's.
    h.start.mockRejectedValue(new BuildSessionAlreadyActiveError('busy', 'other-9'))
    h.getStatus.mockResolvedValue(statusResp({
      sessionId: 'other-9', projectId: 'p1', status: 'building',
    }))
    renderThread()
    send('a visitor app')
    fireEvent.click(await screen.findByRole('button', { name: /build this/i }))

    // The card tells the truth and re-arms, so the rebuild is where the user is looking...
    const retry = await screen.findByRole('button', { name: /try again/i })
    expect(retry.disabled).toBe(false)
    expect(screen.getByRole('alert').textContent).toMatch(/already running for this project/i)
    expect(screen.queryByRole('button', { name: /building/i })).toBeNull()
  })
})

describe('one send is one turn', () => {
  it('a double-Enter in the same tick sends once', async () => {
    // The synchronous gate went away when the composer started holding the draft until the server
    // confirms — so the second keydown reads the SAME text and fires again. `generating` cannot
    // catch it: both keydowns land before the first await resolves, so it is still false. The
    // result is two persisted turns, two model calls, and two brief cards for one request.
    renderThread()
    await screen.findByPlaceholderText(/describe what you need/i)

    fireEvent.change(composer(), { target: { value: 'a visitor app' } })
    fireEvent.keyDown(composer(), { key: 'Enter' })
    fireEvent.keyDown(composer(), { key: 'Enter' }) // same tick — nothing has awaited yet

    await waitFor(() => expect(h.sendMessage).toHaveBeenCalledTimes(1))
    const userTurns = h.appendBuilderMessage.mock.calls.filter(([, m]) => m.role === 'user')
    expect(userTurns).toHaveLength(1)
    await waitFor(() => expect(screen.getAllByTestId('build-brief-card')).toHaveLength(1))
  })

  it('releases the gate so the next turn still sends', async () => {
    // The guard must not become a one-send-per-mount latch — `finally` is what makes it a window.
    renderThread()
    await screen.findByPlaceholderText(/describe what you need/i)

    send('first')
    await waitFor(() => expect(h.sendMessage).toHaveBeenCalledTimes(1))
    send('second')

    await waitFor(() => expect(h.sendMessage).toHaveBeenCalledTimes(2))
  })
})
