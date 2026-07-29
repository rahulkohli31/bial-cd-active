/**
 * THE COMPOSER CONTRACT (U4). One gate, and its only term is turn state.
 *
 * Mode appears nowhere in it — a mode is a tool-access level on the same conversation, and using
 * it as a composer gate is what produced the Write dead end. What the gate withholds is *sending*,
 * not typing: the text box and attach stay live so the citizen can compose their next message
 * while they wait, and not disabling the textarea IS the focus fix (a `disabled` on the focused
 * element blurs it to `document.body`).
 *
 * Four defects live here and each has its own trap:
 *   N10 — the composer went dead mid-reply and stole focus mid-sentence.
 *   G1  — the gate read "open" while the adopt round-trip was unresolved, over a possibly-live
 *          build. Its fix has FOUR arms, and missing the no-anchor one bricks every ordinary chat.
 *   G2  — `generating` was global, so switching chats mid-stream gated the new chat on the old
 *          chat's turn.
 *   G3  — a typed draft died three different ways: a reload, a chat switch, and a refinement chip.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act, cleanup } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

const h = vi.hoisted(() => ({
  loadBuilds: vi.fn(), newBuild: vi.fn(), createBuild: vi.fn(), getBuild: vi.fn(),
  deleteBuild: vi.fn(), listProjectConversations: vi.fn(), buildUserParts: vi.fn(),
  startTurn: vi.fn(), readTurnStream: vi.fn(), buildFromPlan: vi.fn(),
  switchMode: vi.fn(), resolvePlanOptions: vi.fn(),
  start: vi.fn(), stop: vi.fn(), getStatus: vi.fn(), forceEnd: vi.fn(), relaunchPreview: vi.fn(),
  acquireLock: vi.fn(), renewLock: vi.fn(), releaseLock: vi.fn(), heartbeat: vi.fn(),
}))

vi.mock('../../utils/builderHistory', () => ({
  loadBuilds: h.loadBuilds, newBuild: h.newBuild, createBuild: h.createBuild,
  getBuild: h.getBuild, deleteBuild: h.deleteBuild, deriveTitle: (t) => (t || '').slice(0, 40),
}))
vi.mock('../../utils/conversationApi', () => ({ listProjectConversations: h.listProjectConversations }))
vi.mock('../../utils/chatHistory', () => ({ relativeTime: () => 'now' }))
vi.mock('../../components/layout/Navbar', () => ({ default: () => null }))
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
import { ApiError } from '../../utils/apiError'
import {
  FakeEventSource, makeClient, primeClient, primeTurn, statusResp, turnStreaming, planReply,
  waitForGateOpen, PREVIEW, ENDED,
} from './_builderSession.jsx'

const deps = () => {
  const fake = new FakeEventSource('x')
  return { fake, deps: { client: makeClient(h), eventSourceFactory: () => fake } }
}

/** Render at an explicit chat id, so a rerender can move the SAME instance to a sibling chat. */
function renderAt(chatId, sessionDeps, projectId = 'p1') {
  return render(
    <MemoryRouter initialEntries={['/x']}>
      <BuilderPage chatId={chatId} projectId={projectId} projectName="VIP Movement" buildSessionDeps={sessionDeps} />
    </MemoryRouter>,
  )
}

const composer = () => screen.getByPlaceholderText(/describe what you need/i)
const sendButton = () => composer().parentElement.querySelector('button:last-of-type')
const type = (text) => fireEvent.change(composer(), { target: { value: text } })

/** A transcript whose newest assistant part anchors a build that may still be running. */
const withAnchor = (sessionId = 'live-7') => ({
  id: 'build-X',
  kind: 'builder',
  mode: 'plan',
  messages: [
    { id: 'm0', role: 'user', seq: 0, parts: [{ type: 'text', text: 'a visitor app' }] },
    { id: 'srv_1_g', role: 'assistant', seq: 1, parts: [{ type: 'build_in_progress', sessionId }] },
  ],
})

beforeEach(() => {
  vi.clearAllMocks()
  sessionStorage.clear()
  Element.prototype.scrollIntoView = vi.fn()
  primeClient(h)
  primeTurn(h)
  h.newBuild.mockReturnValue('build-Y')
  h.createBuild.mockResolvedValue({ ok: true })
  h.getBuild.mockResolvedValue(null)
  h.loadBuilds.mockResolvedValue([])
  h.listProjectConversations.mockResolvedValue([])
  h.buildUserParts.mockImplementation(async (t) => [{ type: 'text', text: t }])
})
afterEach(() => cleanup())

describe('the gate withholds SENDING, not typing (N10)', () => {
  it('mid-reply: the box takes input, attach is live, send is unavailable, the pill is frozen', async () => {
    h.readTurnStream.mockImplementation(() => new Promise(() => {})) // the reply never lands
    h.getBuild.mockResolvedValue({ id: 'build-X', kind: 'builder', mode: 'plan', messages: [] })
    const { deps: d } = deps()
    renderAt('build-X', d)
    await waitForGateOpen()
    type('first')
    fireEvent.keyDown(composer(), { key: 'Enter' })
    await waitFor(() => expect(h.startTurn).toHaveBeenCalled())

    expect(composer().disabled).toBe(false)
    fireEvent.change(composer(), { target: { value: 'typed while it thinks' } })
    expect(composer().value).toBe('typed while it thinks')
    expect(screen.getByTitle(/Attach images/i).disabled).toBe(false)
    expect(sendButton().getAttribute('aria-disabled')).toBe('true')
    // The pill freezes for a SERVER rule, not a composer one: mid-run rows are stamped with the
    // conversation's mode, so a switch would retroactively mislabel work in flight (KTD-4).
    expect(screen.getByRole('button', { name: /^Mode: Plan\./ }).disabled).toBe(true)
  })

  it('focus never leaves the box — not at the turn\'s start, not at its terminal (the N10 complaint)', async () => {
    // `disabled` on the currently-focused element blurs it to `document.body`. That single line was
    // the whole "blurs mid-sentence, focus never restored" report. There is also no programmatic
    // focus GRAB at either edge — stealing focus at an async moment is its own bug class.
    //
    // HONEST LIMIT: jsdom does not implement blur-on-disable, so the `activeElement` assertions
    // below cannot by themselves catch a reintroduced `disabled` — they pin the no-focus-grab half.
    // The mechanism itself is pinned by asserting the attribute, here and in the sibling test
    // above; only a real browser reproduces the blur (`.mythos` scenarios cover that).
    h.getBuild.mockResolvedValue({ id: 'build-X', kind: 'builder', mode: 'plan', messages: [] })
    // Hold the turn OPEN across the assertion. With a stream that settles in the same tick the
    // composer is already re-enabled by the time focus is read, and the blur goes unseen — the
    // exact reason this defect survived a green suite.
    let finish = () => {}
    h.readTurnStream.mockImplementation(async ({ onFrame }) => {
      onFrame({ type: 'text_delta', seq: 1, text: 'thinking…' })
      await new Promise((resolve) => { finish = resolve })
      onFrame({ type: 'turn_ended', seq: 9, turnId: 't1', status: 'completed' })
      return 'completed'
    })
    const { deps: d } = deps()
    renderAt('build-X', d)
    await waitForGateOpen()

    composer().focus()
    expect(document.activeElement).toBe(composer())

    type('hello')
    fireEvent.keyDown(composer(), { key: 'Enter' })
    await waitFor(() => expect(screen.getByTestId('composer-gate-note').textContent).toMatch(/^Replying/i))
    expect(composer().disabled).toBe(false) // the mechanism — what jsdom CAN see
    expect(document.activeElement).toBe(composer()) // …during, with the turn genuinely in flight

    await act(async () => { finish(); await Promise.resolve() })
    await waitFor(() => expect(screen.queryByTestId('composer-gate-note')).toBeNull())
    expect(document.activeElement).toBe(composer()) // …and after, with no focus grab either way
  })

  it('Enter is refused by handleSend itself, not by an attribute', async () => {
    h.readTurnStream.mockImplementation(() => new Promise(() => {}))
    h.getBuild.mockResolvedValue({ id: 'build-X', kind: 'builder', mode: 'plan', messages: [] })
    const { deps: d } = deps()
    renderAt('build-X', d)
    await waitForGateOpen()
    type('first')
    fireEvent.keyDown(composer(), { key: 'Enter' })
    await waitFor(() => expect(h.startTurn).toHaveBeenCalledTimes(1))

    type('second')
    fireEvent.keyDown(composer(), { key: 'Enter' })
    fireEvent.click(sendButton()) // the button is focusable AND clickable — only handleSend stops it
    await act(async () => { await Promise.resolve() })
    expect(h.startTurn).toHaveBeenCalledTimes(1)
  })

  it('send exposes aria-disabled rather than disabled, so a tabbed-to Send is never blurred to body', async () => {
    h.readTurnStream.mockImplementation(() => new Promise(() => {}))
    h.getBuild.mockResolvedValue({ id: 'build-X', kind: 'builder', mode: 'plan', messages: [] })
    const { deps: d } = deps()
    renderAt('build-X', d)
    await waitForGateOpen()
    type('first')
    sendButton().focus()
    fireEvent.click(sendButton())
    await waitFor(() => expect(h.startTurn).toHaveBeenCalled())

    expect(sendButton().disabled).toBe(false)
    expect(sendButton().getAttribute('aria-disabled')).toBe('true')
    expect(document.activeElement).toBe(sendButton())
  })
})

describe('the closed gate always states its reason', () => {
  it('names the reply, the build, and the check as three different waits', async () => {
    h.readTurnStream.mockImplementation(() => new Promise(() => {}))
    h.getBuild.mockResolvedValue({ id: 'build-X', kind: 'builder', mode: 'plan', messages: [] })
    const { deps: d } = deps()
    renderAt('build-X', d)

    // …the check (before the adopt round-trip settles).
    expect(screen.getByTestId('composer-gate-note').textContent).toMatch(/checking whether a build/i)
    await waitForGateOpen()
    expect(screen.queryByTestId('composer-gate-note')).toBeNull()

    // …the reply.
    type('hi')
    fireEvent.keyDown(composer(), { key: 'Enter' })
    await waitFor(() => expect(screen.getByTestId('composer-gate-note').textContent).toMatch(/^Replying/i))
  })

  it('the build wait says the app is being built', async () => {
    const d = deps()
    renderAt('build-X', d.deps)
    await waitForGateOpen()
    type('a visitor app')
    fireEvent.keyDown(composer(), { key: 'Enter' })
    fireEvent.click(await screen.findByRole('button', { name: /^Build it$/ }))
    await waitFor(() => expect(screen.getByTestId('composer-gate-note').textContent).toMatch(/building your app/i))
  })
})

describe('the gate waits for the adopt round-trip (G1)', () => {
  it('THE COMMON CASE: a chat with no build anchor resolves on mount and send is available', async () => {
    // The arm that would brick the whole product if missed. `reattachToLiveBuild` early-returns
    // when the transcript holds no `build_in_progress` part — which is every ordinary chat — so
    // keying resolution solely on `session.reattach` settling would leave send permanently
    // unavailable almost everywhere.
    h.getBuild.mockResolvedValue({ id: 'build-X', kind: 'builder', mode: 'plan', messages: [] })
    const { deps: d } = deps()
    renderAt('build-X', d)

    await waitFor(() => expect(screen.queryByTestId('composer-gate-note')).toBeNull())
    type('hello')
    fireEvent.keyDown(composer(), { key: 'Enter' })
    await waitFor(() => expect(h.startTurn).toHaveBeenCalled())
  })

  it('an unresolved anchor keeps send shut until the round-trip settles', async () => {
    let settle = () => {}
    h.getBuild.mockResolvedValue(withAnchor())
    h.getStatus.mockImplementation(
      () => new Promise((resolve) => { settle = () => resolve(statusResp({ sessionId: 'live-7', projectId: 'p1', status: 'ended' })) }),
    )
    const { deps: d } = deps()
    renderAt('build-X', d)

    await waitFor(() => expect(h.getStatus).toHaveBeenCalledWith('live-7'))
    expect(screen.getByTestId('composer-gate-note').textContent).toMatch(/checking whether a build/i)
    type('too early')
    fireEvent.keyDown(composer(), { key: 'Enter' })
    await act(async () => { await Promise.resolve() })
    expect(h.startTurn).not.toHaveBeenCalled()

    await act(async () => { settle(); await Promise.resolve() })
    await waitFor(() => expect(screen.queryByTestId('composer-gate-note')).toBeNull())
  })

  it('a 404 on reattach — the ordinary retention lapse — resolves the gate quietly', async () => {
    h.getBuild.mockResolvedValue(withAnchor())
    h.getStatus.mockRejectedValue(new ApiError('gone', 404))
    const { deps: d } = deps()
    renderAt('build-X', d)

    await waitFor(() => expect(screen.queryByTestId('composer-gate-note')).toBeNull())
    expect(screen.queryByText(/couldn’t check/i)).toBeNull() // quiet: there is nothing to report
    type('carry on')
    fireEvent.keyDown(composer(), { key: 'Enter' })
    await waitFor(() => expect(h.startTurn).toHaveBeenCalled())
  })

  it('a NON-404 failure leaves send shut AND renders a Retry that re-runs the check', async () => {
    // The one arm that stays closed, because the page genuinely could not ask. Leaving it closed
    // with only a vanishing toast would recreate the dead-end class this plan exists to remove.
    h.getBuild.mockResolvedValue(withAnchor())
    h.getStatus.mockRejectedValue(new ApiError('upstream exploded', 500))
    const { deps: d } = deps()
    renderAt('build-X', d)

    const note = await screen.findByTestId('composer-gate-note')
    await waitFor(() => expect(note.textContent).toMatch(/couldn’t check whether a build is running/i))
    type('let me in')
    fireEvent.keyDown(composer(), { key: 'Enter' })
    await act(async () => { await Promise.resolve() })
    expect(h.startTurn).not.toHaveBeenCalled()

    // The way out.
    h.getStatus.mockResolvedValue(statusResp({ sessionId: 'live-7', projectId: 'p1', status: 'ended' }))
    fireEvent.click(screen.getByRole('button', { name: /^Retry$/ }))
    await waitFor(() => expect(screen.queryByTestId('composer-gate-note')).toBeNull())
    fireEvent.keyDown(composer(), { key: 'Enter' })
    await waitFor(() => expect(h.startTurn).toHaveBeenCalled())
  })
})

describe('an in-flight turn belongs to ONE chat (G2)', () => {
  it('a turn streaming in chat A does not gate chat B\'s send', async () => {
    h.readTurnStream.mockImplementation(() => new Promise(() => {})) // A's reply never lands
    h.getBuild.mockResolvedValue({ id: 'chat-A', kind: 'builder', mode: 'plan', messages: [] })
    const { deps: d } = deps()
    const { rerender } = renderAt('chat-A', d)
    await waitForGateOpen()
    type('a question')
    fireEvent.keyDown(composer(), { key: 'Enter' })
    await waitFor(() => expect(h.startTurn).toHaveBeenCalledWith('chat-A', expect.anything()))

    // The SAME instance moves to a sibling chat (flat routing — only the chatId prop changes).
    h.getBuild.mockResolvedValue({ id: 'chat-B', kind: 'builder', mode: 'plan', messages: [] })
    h.readTurnStream.mockImplementation(turnStreaming(planReply('B plan', 'opt-B')))
    rerender(
      <MemoryRouter initialEntries={['/x']}>
        <BuilderPage chatId="chat-B" projectId="p1" projectName="VIP Movement" buildSessionDeps={d} />
      </MemoryRouter>,
    )
    await waitFor(() => expect(h.getBuild).toHaveBeenCalledWith('chat-B'))
    await waitFor(() => expect(screen.queryByTestId('composer-gate-note')).toBeNull())

    h.startTurn.mockClear()
    type('a different question')
    fireEvent.keyDown(composer(), { key: 'Enter' })
    await waitFor(() => expect(h.startTurn).toHaveBeenCalledWith('chat-B', expect.anything()))
  })
})

describe('a typed draft survives (G3)', () => {
  it('a reload restores it', async () => {
    h.getBuild.mockResolvedValue({ id: 'build-X', kind: 'builder', mode: 'plan', messages: [] })
    const { deps: d } = deps()
    renderAt('build-X', d)
    await waitForGateOpen()
    type('half a thought about gate assignments')

    cleanup()
    renderAt('build-X', deps().deps)
    await waitFor(() => expect(composer().value).toBe('half a thought about gate assignments'))
  })

  it('each chat keeps its own — switching never leaks A\'s text into B', async () => {
    h.getBuild.mockResolvedValue({ id: 'chat-A', kind: 'builder', mode: 'plan', messages: [] })
    const { deps: d } = deps()
    const { rerender } = renderAt('chat-A', d)
    await waitForGateOpen()
    type("A's draft")

    const goTo = async (chatId) => {
      h.getBuild.mockResolvedValue({ id: chatId, kind: 'builder', mode: 'plan', messages: [] })
      rerender(
        <MemoryRouter initialEntries={['/x']}>
          <BuilderPage chatId={chatId} projectId="p1" projectName="VIP Movement" buildSessionDeps={d} />
        </MemoryRouter>,
      )
      await waitFor(() => expect(h.getBuild).toHaveBeenCalledWith(chatId))
    }

    await goTo('chat-B')
    await waitFor(() => expect(composer().value).toBe(''))
    type("B's draft")

    await goTo('chat-A')
    await waitFor(() => expect(composer().value).toBe("A's draft"))
  })

  it('a SUCCESSFUL send clears it, so a reload does not re-offer the message just sent', async () => {
    // An uncleared draft re-populates the composer with the text that was already sent, which is
    // easy to send twice by accident — the failure mode that makes persistence worse than nothing.
    h.getBuild.mockResolvedValue({ id: 'build-X', kind: 'builder', mode: 'plan', messages: [] })
    const { deps: d } = deps()
    renderAt('build-X', d)
    await waitForGateOpen()
    type('ship it')
    fireEvent.keyDown(composer(), { key: 'Enter' })
    await waitFor(() => expect(h.startTurn).toHaveBeenCalled())
    await waitFor(() => expect(composer().value).toBe(''))

    cleanup()
    renderAt('build-X', deps().deps)
    await waitForGateOpen()
    expect(composer().value).toBe('')
  })

  it('a FAILED send keeps it — the toast says try again, so the text has to still be there', async () => {
    h.getBuild.mockResolvedValue(null) // seq 0 → the create branch, which is the one that can fail
    h.createBuild.mockRejectedValue(new Error('network down'))
    const { deps: d } = deps()
    renderAt('build-X', d)
    await waitForGateOpen()
    type('please do not eat this')
    fireEvent.keyDown(composer(), { key: 'Enter' })

    expect(await screen.findByText(/could not start this thread/i)).toBeTruthy()
    expect(composer().value).toBe('please do not eat this')
  })
})

describe('the refinement chips do not eat the draft', () => {
  const showChips = async () => {
    const d = deps()
    renderAt('build-X', d.deps)
    await waitForGateOpen()
    type('a visitor app')
    fireEvent.keyDown(composer(), { key: 'Enter' })
    fireEvent.click(await screen.findByRole('button', { name: /^Build it$/ }))
    await waitFor(() => expect(h.buildFromPlan).toHaveBeenCalled())
    act(() => { d.fake.open(); d.fake.emitEnvelope(PREVIEW(3)); d.fake.emitEnvelope(ENDED(9)) })
    await waitFor(() => expect(screen.queryByTestId('composer-gate-note')).toBeNull())
    return screen.getAllByRole('button', { name: /^(Make it|Add|Change)/i })[0]
  }

  it('APPENDS to a non-empty composer rather than replacing it', async () => {
    const chip = await showChips()
    type('and please keep the export button')
    fireEvent.click(chip)
    expect(composer().value).toMatch(/^and please keep the export button /)
    expect(composer().value.length).toBeGreaterThan('and please keep the export button '.length)
  })

  it('inserts straight into an empty composer', async () => {
    const chip = await showChips()
    const label = chip.textContent
    fireEvent.click(chip)
    expect(composer().value).toBe(label)
  })
})
