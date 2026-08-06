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
 *   G3  — a typed draft died three different ways: a reload, a chat switch, and a refinement chip
 *          that overwrote it. The chips themselves are gone now (2026-07-30), so the third way is
 *          pinned from the other side: nothing canned may appear to seed the composer at all.
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
  notifyUsageChanged: vi.fn(),
}))

vi.mock('../../utils/usage', () => ({ notifyUsageChanged: h.notifyUsageChanged }))

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
  waitForGateOpen, scriptBuildTurn, BUILD_TURN_ID, T_PREVIEW, T_BUILD_END,
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

describe('a finished build offers no canned follow-ups (2026-07-30)', () => {
  // The three hardcoded refinement chips are DELETED, so what is pinned here is their absence.
  // They were a fixed list — dark mode, a real-time data table, a mobile layout — appended to
  // every build regardless of what had been built, and the composer is the one place a follow-up
  // belongs. The regression this guards is a well-meant re-introduction: a suggestion that cannot
  // know what the app is has nothing to suggest.
  it('leaves the composer as the only way to ask for the next change', async () => {
    const turn = scriptBuildTurn()
    h.readTurnStream.mockImplementation(turn.impl)
    renderAt('build-X', deps().deps)
    await waitForGateOpen()
    type('a visitor app')
    fireEvent.keyDown(composer(), { key: 'Enter' })
    fireEvent.click(await screen.findByRole('button', { name: /^Build it$/ }))
    await waitFor(() =>
      expect(h.readTurnStream).toHaveBeenCalledWith(expect.objectContaining({ turnId: BUILD_TURN_ID })),
    )
    await turn.frame(T_PREVIEW(), T_BUILD_END())
    await turn.end()
    await waitFor(() => expect(screen.queryByTestId('composer-gate-note')).toBeNull())

    expect(screen.queryByRole('button', { name: /dark mode|data table|mobile layout/i })).toBeNull()
    expect(composer().value).toBe('')
  })
})


// N4 — the meter has to settle without a reload. `notifyUsageChanged` had exactly one caller,
// in the retiring relay hook, so the turn transport never signalled: a user could spend their
// whole daily budget watching a number that only ever moved on a page load. The signal now fires
// from the ONE function every turn terminal routes through, which is what makes the failed and
// stopped arms below free rather than three separate call sites to remember.
describe('the usage meter settles at every turn terminal (N4)', () => {
  it('a completed turn signals the meter', async () => {
    h.getBuild.mockResolvedValue({ id: 'build-X', kind: 'builder', mode: 'plan', messages: [] })
    const { deps: d } = deps()
    renderAt('build-X', d)
    await waitForGateOpen()
    expect(h.notifyUsageChanged).not.toHaveBeenCalled()

    type('what does this app do?')
    fireEvent.keyDown(composer(), { key: 'Enter' })
    await waitFor(() => expect(h.notifyUsageChanged).toHaveBeenCalled())
  })

  it('a FAILED turn settles it too, rather than leaving the meter stale', async () => {
    // The arm most worth pinning: a turn that dies still billed for the tokens it spent, so
    // skipping the signal here understates the budget exactly when the user is closest to it.
    h.getBuild.mockResolvedValue({ id: 'build-X', kind: 'builder', mode: 'plan', messages: [] })
    h.startTurn.mockRejectedValue(new Error('the turn could not start'))
    const { deps: d } = deps()
    renderAt('build-X', d)
    await waitForGateOpen()

    type('this will not fly')
    fireEvent.keyDown(composer(), { key: 'Enter' })
    await waitFor(() => expect(h.notifyUsageChanged).toHaveBeenCalled())
  })

  it('a stopped/truncated stream settles it as well', async () => {
    h.getBuild.mockResolvedValue({ id: 'build-X', kind: 'builder', mode: 'plan', messages: [] })
    h.readTurnStream.mockImplementation(turnStreaming([], 'truncated'))
    const { deps: d } = deps()
    renderAt('build-X', d)
    await waitForGateOpen()

    type('half a reply')
    fireEvent.keyDown(composer(), { key: 'Enter' })
    await waitFor(() => expect(h.notifyUsageChanged).toHaveBeenCalled())
  })
})


// CC1–CC4 (U10) — opening one chat must never damage another chat's live build, and reloading
// mid-build must not erase the story. All four live in this file's neighbourhood because they
// share the adopt/reattach predicates the composer gate is built on.
describe('cross-chat build scoping and reload fidelity (CC1–CC4)', () => {
  const liveStatus = (sessionId) =>
    statusResp({ sessionId, projectId: 'p1', status: 'building' })

  it('CC1: adopting a SIBLING chat with a stale anchor does not tear down the live session', async () => {
    // This is a regression of a class the repo already fixed once and wrote down: stamp the
    // ownership refs before classifying and every same-session guard becomes tautological. Here
    // it is worse than tautological — `session.reattach()`'s first act is a synchronous
    // `reset()`, so the sibling's adopt killed the running build's heartbeat and lock renewal.
    //
    // A build is a TURN now, so the live session this guard protects is the one a reload adopts
    // from its own anchor (the last remaining producer of one, alongside Relaunch) — which is
    // exactly the case with a heartbeat and a lock renewal to lose.
    h.getBuild.mockResolvedValue(withAnchor('live-7'))
    h.getStatus.mockResolvedValue(liveStatus('live-7'))
    const d = deps()
    const { rerender } = renderAt('chat-A', d.deps)
    await waitFor(() => expect(h.getStatus).toHaveBeenCalledWith('live-7'))
    await waitFor(() => expect(screen.getByTestId('build-progress')).toBeTruthy())

    // Chat B carries a STALE anchor naming a different session.
    h.getBuild.mockResolvedValue({
      id: 'chat-B',
      kind: 'builder',
      mode: 'plan',
      messages: [
        { id: 'm0', role: 'user', seq: 0, parts: [{ type: 'text', text: 'older build' }] },
        { id: 'srv_1_g_1', role: 'assistant', seq: 1, parts: [{ type: 'build_in_progress', sessionId: 'stale-9' }] },
      ],
    })
    h.getStatus.mockClear()
    rerender(
      <MemoryRouter initialEntries={['/x']}>
        <BuilderPage chatId="chat-B" projectId="p1" projectName="VIP Movement" buildSessionDeps={d.deps} />
      </MemoryRouter>,
    )
    await waitFor(() => expect(h.getBuild).toHaveBeenCalledWith('chat-B'))
    await waitFor(() => expect(screen.queryByTestId('composer-gate-note')).toBeNull())

    // The stale session was never reattached, so A's keep-alive was never reset.
    expect(h.getStatus).not.toHaveBeenCalledWith('stale-9')
  })

  it('CC1: the OWNING chat still reattaches on its own reload', async () => {
    // The other arm — the guard must not be so broad that it breaks legitimate reattach.
    h.getBuild.mockResolvedValue(withAnchor('live-7'))
    h.getStatus.mockResolvedValue(liveStatus('live-7'))
    const { deps: d } = deps()
    renderAt('build-X', d)

    await waitFor(() => expect(h.getStatus).toHaveBeenCalledWith('live-7'))
    await waitFor(() => expect(screen.getByTestId('composer-gate-note').textContent).toMatch(/building your app/i))
  })

  it('CC2: a reload mid-build still renders the stored step history', async () => {
    // `reattach()` resets `envelopes` and subscribes to the LIVE feed — it replays nothing — so
    // suppressing every stored row "because the live bubble re-tells them" blanked the whole
    // transcript. Assert a COUNT, not merely the absence of a crash.
    h.getBuild.mockResolvedValue({
      id: 'build-X',
      kind: 'builder',
      mode: 'write',
      // The REAL ordering: the anchor is written when the build starts, the steps arrive after
      // it. Putting the steps before it would leave them outside the suppression range entirely
      // and the test would pass against the very bug it is meant to catch.
      messages: [
        { id: 'm0', role: 'user', seq: 0, parts: [{ type: 'text', text: 'a visitor app' }] },
        { id: 'g1', role: 'assistant', seq: 1, parts: [{ type: 'build_in_progress', sessionId: 'live-7' }] },
        { id: 's2', role: 'assistant', seq: 2, parts: [{ type: 'step', step: { tool: 'write_file', label: 'Updated the home page', state: 'ok' } }] },
        { id: 's3', role: 'assistant', seq: 3, parts: [{ type: 'step', step: { tool: 'write_file', label: 'Added the form', state: 'ok' } }] },
      ],
    })
    h.getStatus.mockResolvedValue(liveStatus('live-7'))
    const { deps: d } = deps()
    renderAt('build-X', d)

    await waitFor(() => expect(h.getStatus).toHaveBeenCalledWith('live-7'))
    await waitFor(() => expect(document.querySelectorAll('[data-kind="stored-step"]')).toHaveLength(2))
    // …and the past-tense anchor STILL stays hidden: the build is live, the bubble speaks for it.
    expect(document.querySelector('[data-kind="build-in-progress"]')).toBeNull()
    expect(screen.getByTestId('build-progress')).toBeTruthy()
  })

  it('CC3: a sibling chat renders no live build bubble, and therefore no Stop button', async () => {
    // The narrative used to be project-scoped while the composer gate was chat-scoped, so the
    // sibling rendered another chat's build complete with a WORKING Stop — one click ending a
    // build the reader never started. The turn narrative is scoped by the same per-chat predicate
    // as the gate (`generatingChatId === buildId`), which is what keeps the two from diverging.
    h.getBuild.mockResolvedValue({ id: 'chat-A', kind: 'builder', mode: 'plan', messages: [] })
    const turn = scriptBuildTurn()
    h.readTurnStream.mockImplementation(turn.impl)
    const d = deps()
    const { rerender } = renderAt('chat-A', d.deps)
    await waitForGateOpen()
    type('build me a thing')
    fireEvent.keyDown(composer(), { key: 'Enter' })
    fireEvent.click(await screen.findByRole('button', { name: /^Build it$/ }))
    await waitFor(() => expect(h.buildFromPlan).toHaveBeenCalled())
    await turn.frame(T_PREVIEW())
    await waitFor(() => expect(screen.getByTestId('build-progress')).toBeTruthy())
    expect(screen.getByRole('button', { name: /^Stop$/i })).toBeTruthy() // A's own Stop is real

    h.getBuild.mockResolvedValue({ id: 'chat-B', kind: 'builder', mode: 'plan', messages: [] })
    rerender(
      <MemoryRouter initialEntries={['/x']}>
        <BuilderPage chatId="chat-B" projectId="p1" projectName="VIP Movement" buildSessionDeps={d.deps} />
      </MemoryRouter>,
    )
    await waitFor(() => expect(h.getBuild).toHaveBeenCalledWith('chat-B'))

    expect(screen.queryByTestId('build-progress')).toBeNull()
    expect(screen.queryByRole('button', { name: /^Stop$/i })).toBeNull()
  })

  it('CC4: a reattached turn resubscribes ONCE on a truncation, then gives up honestly', async () => {
    // `fireRelayTurn` has had resume-once since the streamed-reply learning; this path mapped any
    // throw to 'truncated' and stopped, so one dropped socket after a reload reported "the
    // connection dropped" about a turn that was still running server-side.
    h.getBuild.mockResolvedValue({
      id: 'build-X',
      kind: 'builder',
      mode: 'plan',
      messages: [{ id: 'm0', role: 'user', seq: 0, parts: [{ type: 'text', text: 'hi' }] }],
      activeTurn: { turnId: 't1' },
    })
    let reads = 0
    h.readTurnStream.mockImplementation(async ({ onFrame }) => {
      reads += 1
      if (reads === 1) return 'truncated'
      onFrame({ type: 'text_delta', seq: 1, text: 'recovered' })
      onFrame({ type: 'turn_ended', seq: 9, turnId: 't1', status: 'completed' })
      return 'completed'
    })
    const { deps: d } = deps()
    renderAt('build-X', d)

    await waitFor(() => expect(reads).toBe(2))
    expect(screen.queryByText(/the connection dropped/i)).toBeNull()
  })

  it('CC4: a SECOND truncation is a real drop and says so', async () => {
    h.getBuild.mockResolvedValue({
      id: 'build-X',
      kind: 'builder',
      mode: 'plan',
      messages: [{ id: 'm0', role: 'user', seq: 0, parts: [{ type: 'text', text: 'hi' }] }],
      activeTurn: { turnId: 't1' },
    })
    h.readTurnStream.mockResolvedValue('truncated')
    const { deps: d } = deps()
    renderAt('build-X', d)

    expect(await screen.findByText(/the connection dropped/i)).toBeTruthy()
    expect(h.readTurnStream).toHaveBeenCalledTimes(2) // once + one resume, never a third
  })
})


// The send-failure catch must tell the truth about what the server has. `startTurn` resolving is
// a 202: the user's message is persisted and the reply runs detached, so a failure AFTER that
// point is subscription plumbing, not a refused send — and "could not be sent" over a persisted
// message invites a duplicate resend.
describe('the send-failure catch splits on whether the turn was accepted (N8)', () => {
  it('a startTurn refusal rolls back BOTH bubbles and says the message was not sent', async () => {
    h.getBuild.mockResolvedValue({ id: 'build-X', kind: 'builder', mode: 'plan', messages: [] })
    h.startTurn.mockRejectedValue(new Error('refused at the door'))
    const { deps: d } = deps()
    renderAt('build-X', d)
    await waitForGateOpen()

    type('build me a thing')
    fireEvent.keyDown(composer(), { key: 'Enter' })

    await waitFor(() => expect(screen.getByText(/could not be sent/i)).toBeTruthy())
    // The server persisted NOTHING, so the optimistic user bubble rolls back too — the
    // transcript must agree with the database (N8).
    expect(screen.queryByText('build me a thing')).toBeNull()
  })

  it('a subscribe failure AFTER the 202 keeps the user bubble and says reload, not resend', async () => {
    h.getBuild.mockResolvedValue({ id: 'build-X', kind: 'builder', mode: 'plan', messages: [] })
    h.readTurnStream.mockRejectedValue(new Error('the stream never opened'))
    const { deps: d } = deps()
    renderAt('build-X', d)
    await waitForGateOpen()

    type('build me a thing')
    fireEvent.keyDown(composer(), { key: 'Enter' })

    await waitFor(() => expect(screen.getByText(/message was received/i)).toBeTruthy())
    // The message IS in the database — its bubble stays, and no "could not be sent" copy
    // appears to invite a duplicate resend of a turn the server is already running.
    expect(screen.getByText('build me a thing')).toBeTruthy()
    expect(screen.queryByText(/could not be sent/i)).toBeNull()
  })
})
