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
import { render, screen, fireEvent, waitFor, act, cleanup, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

const h = vi.hoisted(() => ({
  loadBuilds: vi.fn(), newBuild: vi.fn(), createBuild: vi.fn(), getBuild: vi.fn(),
  deleteBuild: vi.fn(), listProjectConversations: vi.fn(), buildUserParts: vi.fn(),
  startTurn: vi.fn(), readTurnStream: vi.fn(), buildFromPlan: vi.fn(),
  resolvePlanOptions: vi.fn(),
  start: vi.fn(), stop: vi.fn(), getStatus: vi.fn(), forceEnd: vi.fn(), relaunchPreview: vi.fn(),
  notifyUsageChanged: vi.fn(),
}))

vi.mock('../../utils/usage', () => ({ notifyUsageChanged: h.notifyUsageChanged }))

vi.mock('../../utils/builderHistory', () => ({
  loadBuilds: h.loadBuilds, newBuild: h.newBuild, createBuild: h.createBuild,
  getBuild: h.getBuild, deleteBuild: h.deleteBuild, deriveTitle: (t) => (t || '').slice(0, 40),
}))
// SPREAD THE ORIGINAL — `handleBuildIt` mints the new build chat's id through the shared
// `uuidv7` (ADR-0006), and a factory naming only `listProjectConversations` leaves every other
// export (including that one) undefined; Vitest now warns the moment a real caller reaches for
// it, which every Build-it press in this suite does.
vi.mock('../../utils/conversationApi', async (importOriginal) => ({
  ...(await importOriginal()),
  listProjectConversations: h.listProjectConversations,
}))
vi.mock('../../utils/chatHistory', () => ({ relativeTime: () => 'now' }))
vi.mock('../../components/layout/Navbar', () => ({ default: () => null }))
vi.mock('../../utils/attachmentStore', async (orig) => ({ ...(await orig()), buildUserParts: h.buildUserParts }))
// `switchMode` is GONE from this list (U1/U19): the route it posted to no longer exists, and a
// chat's kind can't change after creation, so there is nothing left for a mock to intercept.
vi.mock('../../utils/turnStreamApi', async (orig) => ({
  ...(await orig()),
  startTurn: (...a) => h.startTurn(...a),
  readTurnStream: (...a) => h.readTurnStream(...a),
  buildFromPlan: (...a) => h.buildFromPlan(...a),
  resolvePlanOptions: (...a) => h.resolvePlanOptions(...a),
}))

import ConversationSurface from '../../components/chat/ConversationSurface'
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
      <ConversationSurface chatId={chatId} projectId={projectId} projectName="VIP Movement" buildSessionDeps={sessionDeps} />
    </MemoryRouter>,
  )
}

const composer = () => screen.getByPlaceholderText(/ask for another change/i)
const sendButton = () => composer().parentElement.querySelector('button:last-of-type')
const type = (text) => fireEvent.change(composer(), { target: { value: text } })

/** A transcript whose newest assistant part anchors a build that may still be running. */
const withAnchor = (sessionId = 'live-7') => ({
  id: 'build-X',
  kind: 'build',
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
  it('mid-reply: the box takes input, attach is live, send is unavailable — the mode pill is gone entirely', async () => {
    h.readTurnStream.mockImplementation(() => new Promise(() => {})) // the reply never lands
    h.getBuild.mockResolvedValue({ id: 'build-X', kind: 'build', messages: [] })
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
    // AN INERTNESS GUARD, not the frozen-pill assertion it replaces (L8). This used to prove the
    // mode pill froze mid-run rather than let a switch retroactively mislabel work already in
    // flight (KTD-4) — `ModeSwitcher` and the ask/plan/write axis it switched are BOTH gone
    // (U1/U19: a chat's kind is fixed at creation and never changes), so there is no pill left to
    // freeze. The claim that replaces it is that no such control is on the surface at all, mid-
    // reply or otherwise — and the liveness assertions above already prove the page rendered
    // rather than threw, so this absence means what it says.
    expect(screen.queryByRole('button', { name: /^Mode:/ })).toBeNull()
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
    h.getBuild.mockResolvedValue({ id: 'build-X', kind: 'build', messages: [] })
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
    h.getBuild.mockResolvedValue({ id: 'build-X', kind: 'build', messages: [] })
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
    h.getBuild.mockResolvedValue({ id: 'build-X', kind: 'build', messages: [] })
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
    h.getBuild.mockResolvedValue({ id: 'build-X', kind: 'build', messages: [] })
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
    fireEvent.click(await screen.findByRole('button', { name: /^Build this plan$/ }))
    await waitFor(() => expect(screen.getByTestId('composer-gate-note').textContent).toMatch(/building your app/i))
  })
})

describe('the gate waits for the adopt round-trip (G1)', () => {
  it('THE COMMON CASE: a chat with no build anchor resolves on mount and send is available', async () => {
    // The arm that would brick the whole product if missed. `reattachToLiveBuild` early-returns
    // when the transcript holds no `build_in_progress` part — which is every ordinary chat — so
    // keying resolution solely on `session.reattach` settling would leave send permanently
    // unavailable almost everywhere.
    h.getBuild.mockResolvedValue({ id: 'build-X', kind: 'build', messages: [] })
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
    h.getBuild.mockResolvedValue({ id: 'chat-A', kind: 'build', messages: [] })
    const { deps: d } = deps()
    const { rerender } = renderAt('chat-A', d)
    await waitForGateOpen()
    type('a question')
    fireEvent.keyDown(composer(), { key: 'Enter' })
    // FOUR ARGS NOW, ALWAYS (R-18, U13): `startTurn(id, message, deps, create)` is the ONE call
    // the send path makes, and both messages here are the FIRST in an empty transcript
    // (`messages: []`), so `create` rides along on every one of them as a real (non-`undefined`)
    // positional argument. `toHaveBeenCalledWith` compares argument COUNT as well as content, so
    // pinning only the first two args — as the old two-call protocol's assertion did — fails
    // against every real call now, not just a differently-shaped one. What this test is actually
    // about (chat A's turn reaches the server, chat B's does too, and they are not conflated) does
    // not need the message or create shape spelled out again here — `expect.anything()` for the
    // rest says "some call happened for this chat" without re-pinning R-18's payload a second time
    // in a suite that is not about it.
    await waitFor(() =>
      expect(h.startTurn).toHaveBeenCalledWith('chat-A', expect.anything(), expect.anything(), expect.anything()),
    )

    // The SAME instance moves to a sibling chat (flat routing — only the chatId prop changes).
    h.getBuild.mockResolvedValue({ id: 'chat-B', kind: 'build', messages: [] })
    h.readTurnStream.mockImplementation(turnStreaming(planReply('B plan', 'opt-B')))
    rerender(
      <MemoryRouter initialEntries={['/x']}>
        <ConversationSurface chatId="chat-B" projectId="p1" projectName="VIP Movement" buildSessionDeps={d} />
      </MemoryRouter>,
    )
    await waitFor(() => expect(h.getBuild).toHaveBeenCalledWith('chat-B'))
    await waitFor(() => expect(screen.queryByTestId('composer-gate-note')).toBeNull())

    h.startTurn.mockClear()
    type('a different question')
    fireEvent.keyDown(composer(), { key: 'Enter' })
    // Same four-arg shape as chat A's assertion above — chat B's transcript is empty too, so its
    // first message also carries a `create` block.
    await waitFor(() =>
      expect(h.startTurn).toHaveBeenCalledWith('chat-B', expect.anything(), expect.anything(), expect.anything()),
    )
  })
})

describe('a typed draft survives (G3)', () => {
  it('a reload restores it', async () => {
    h.getBuild.mockResolvedValue({ id: 'build-X', kind: 'build', messages: [] })
    const { deps: d } = deps()
    renderAt('build-X', d)
    await waitForGateOpen()
    type('half a thought about gate assignments')

    cleanup()
    renderAt('build-X', deps().deps)
    await waitFor(() => expect(composer().value).toBe('half a thought about gate assignments'))
  })

  it('each chat keeps its own — switching never leaks A\'s text into B', async () => {
    h.getBuild.mockResolvedValue({ id: 'chat-A', kind: 'build', messages: [] })
    const { deps: d } = deps()
    const { rerender } = renderAt('chat-A', d)
    await waitForGateOpen()
    type("A's draft")

    const goTo = async (chatId) => {
      h.getBuild.mockResolvedValue({ id: chatId, kind: 'build', messages: [] })
      rerender(
        <MemoryRouter initialEntries={['/x']}>
          <ConversationSurface chatId={chatId} projectId="p1" projectName="VIP Movement" buildSessionDeps={d} />
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
    h.getBuild.mockResolvedValue({ id: 'build-X', kind: 'build', messages: [] })
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
    // R-18/U13: there is no separate create call left to fail. The row's parentage rides the
    // turn's OWN request now, so a refused first message takes `startTurn`'s catch — the same
    // path every later message's refusal takes — and it is what this test rejects.
    h.getBuild.mockResolvedValue(null) // seq 0 → the FIRST message, which carries `create`
    h.startTurn.mockRejectedValue(new Error('network down'))
    const { deps: d } = deps()
    renderAt('build-X', d)
    await waitForGateOpen()
    type('please do not eat this')
    fireEvent.keyDown(composer(), { key: 'Enter' })

    // "Could not start this thread" was the retired create call's OWN failure sentence. A
    // generic (non-`TurnStartError`) rejection from `startTurn` falls to the fallback
    // `fireRelayTurn` catch already uses for every other refused send — see "a startTurn refusal
    // rolls back BOTH bubbles…" above, which pins the same copy for the same reason.
    expect(await screen.findByText(/could not be sent/i)).toBeTruthy()
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
    // BUILD-IT IS A HANDOFF NOW (U5/U12), not a flip: the turn runs in a brand-new build chat the
    // offer creates, and only THAT chat's own hydration watches it — this page never subscribes to
    // a build from the chat Build-it was pressed in. Simulate arriving there the same way every
    // other sibling-chat guard in this file does: a chatId prop swap on the SAME instance.
    const NEW_BUILD_CHAT = 'build-live'
    const turn = scriptBuildTurn()
    h.readTurnStream.mockImplementation(turn.impl)
    h.buildFromPlan.mockResolvedValue({ outcome: 'started', chatId: NEW_BUILD_CHAT, turnId: BUILD_TURN_ID })
    const d = deps()
    const { rerender } = renderAt('build-X', d.deps)
    await waitForGateOpen()
    type('a visitor app')
    fireEvent.keyDown(composer(), { key: 'Enter' })
    fireEvent.click(await screen.findByRole('button', { name: /^Build this plan$/ }))
    await waitFor(() => expect(h.buildFromPlan).toHaveBeenCalled())

    // The new chat's own adopt reattaches to the turn the read projection carries.
    h.getBuild.mockResolvedValue({
      id: NEW_BUILD_CHAT, kind: 'build', messages: [],
      activeTurn: { turnId: BUILD_TURN_ID, lastSeq: 0 },
    })
    rerender(
      <MemoryRouter initialEntries={['/x']}>
        <ConversationSurface chatId={NEW_BUILD_CHAT} projectId="p1" projectName="VIP Movement" buildSessionDeps={d.deps} />
      </MemoryRouter>,
    )
    await waitFor(() => expect(h.getBuild).toHaveBeenCalledWith(NEW_BUILD_CHAT))
    await waitFor(() =>
      expect(h.readTurnStream).toHaveBeenCalledWith(expect.objectContaining({ turnId: BUILD_TURN_ID })),
    )
    await turn.frame(T_PREVIEW(), T_BUILD_END())
    await turn.end()
    await waitFor(() => expect(screen.queryByTestId('composer-gate-note')).toBeNull())

    // THE CANNED CHIPS NEVER COME BACK (2026-07-30) — no button offers a follow-up the model was
    // never asked about.
    expect(screen.queryByRole('button', { name: /dark mode|data table|mobile layout/i })).toBeNull()
    // The composer is how the next change gets asked for — proven by actually asking for one,
    // not by an empty textbox that was always going to be empty on a chat nobody typed in yet.
    h.startTurn.mockClear()
    type('add a dark mode toggle')
    fireEvent.keyDown(composer(), { key: 'Enter' })
    // FOUR ARGS ALWAYS (R-18, U13) — see the G2 suite above for the full reasoning. This send is
    // not this chat's first message (the reattached build turn already occupies seq 0), so `create`
    // is `undefined` rather than a parentage object here — but `undefined` is still passed as a
    // real 4th positional argument, so `toHaveBeenCalledWith` still needs a slot for it.
    await waitFor(() =>
      expect(h.startTurn).toHaveBeenCalledWith(
        NEW_BUILD_CHAT,
        expect.objectContaining({ text: 'add a dark mode toggle' }),
        expect.anything(),
        undefined,
      ),
    )
  })
})


// N4 — the meter has to settle without a reload. `notifyUsageChanged` had exactly one caller,
// in the retiring relay hook, so the turn transport never signalled: a user could spend their
// whole daily budget watching a number that only ever moved on a page load. The signal now fires
// from the ONE function every turn terminal routes through, which is what makes the failed and
// stopped arms below free rather than three separate call sites to remember.
describe('the usage meter settles at every turn terminal (N4)', () => {
  it('a completed turn signals the meter', async () => {
    h.getBuild.mockResolvedValue({ id: 'build-X', kind: 'build', messages: [] })
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
    h.getBuild.mockResolvedValue({ id: 'build-X', kind: 'build', messages: [] })
    h.startTurn.mockRejectedValue(new Error('the turn could not start'))
    const { deps: d } = deps()
    renderAt('build-X', d)
    await waitForGateOpen()

    type('this will not fly')
    fireEvent.keyDown(composer(), { key: 'Enter' })
    await waitFor(() => expect(h.notifyUsageChanged).toHaveBeenCalled())
  })

  it('a stopped/truncated stream settles it as well', async () => {
    h.getBuild.mockResolvedValue({ id: 'build-X', kind: 'build', messages: [] })
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
    await waitFor(() => expect(screen.getByTestId('stop-turn')).toBeTruthy())

    // Chat B carries a STALE anchor naming a different session.
    h.getBuild.mockResolvedValue({
      id: 'chat-B',
      kind: 'build',
      messages: [
        { id: 'm0', role: 'user', seq: 0, parts: [{ type: 'text', text: 'older build' }] },
        { id: 'srv_1_g_1', role: 'assistant', seq: 1, parts: [{ type: 'build_in_progress', sessionId: 'stale-9' }] },
      ],
    })
    h.getStatus.mockClear()
    rerender(
      <MemoryRouter initialEntries={['/x']}>
        <ConversationSurface chatId="chat-B" projectId="p1" projectName="VIP Movement" buildSessionDeps={d.deps} />
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
      kind: 'build',
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
    // ONE GROUP, NOT TWO — and this is the live/reload parity assertion (AE43), not a styling
    // preference. The projection stores one MESSAGE per step while the live path puts every step
    // of a turn on a single streaming message, so without the surface merging a run of stored step
    // rows a build watched live would show one group of nine and the same build after a reload
    // would show nine groups of one.
    await waitFor(() => expect(screen.getAllByTestId('activity-group')).toHaveLength(1))
    fireEvent.click(screen.getByTestId('activity-group-trigger'))
    const rows = within(await screen.findByTestId('activity-group-rows'))
    expect(rows.getByText(/Updated the home page/i)).toBeTruthy()
    expect(rows.getByText(/Added the form/i)).toBeTruthy()
    // …and the past-tense anchor stays out of the transcript: `build_in_progress` maps to no
    // rendered part at all now, so the sentence cannot appear whether a build is live or not.
    expect(document.querySelector('[data-kind="build-in-progress"]')).toBeNull()
    expect(screen.getByTestId('stop-turn')).toBeTruthy()
  })

  it('CC3: a sibling chat renders no live build bubble, and therefore no Stop button', async () => {
    // The narrative used to be project-scoped while the composer gate was chat-scoped, so a
    // sibling rendered another chat's build complete with a WORKING Stop — one click ending a
    // build the reader never started. The turn narrative is scoped by the same per-chat predicate
    // as the gate (`generatingChatId === buildId`), which is what keeps the two from diverging.
    //
    // BUILD-IT IS A HANDOFF NOW (U5/U12): pressing it in `chat-A` no longer makes `chat-A` itself
    // narrate the build — a brand-new chat does, and `chat-A`'s own hydration never watches it.
    // So "the OWNING chat" this guard is really about is the chat the handoff lands in, and "a
    // sibling" is any OTHER chat, `chat-A` (the plan chat that made the offer) included.
    const LIVE_BUILD_CHAT = 'chat-A-live'
    h.getBuild.mockImplementation(async (id) =>
      id === LIVE_BUILD_CHAT
        ? { id, kind: 'build', messages: [], activeTurn: { turnId: BUILD_TURN_ID, lastSeq: 0 } }
        : { id, kind: 'build', messages: [] },
    )
    h.buildFromPlan.mockResolvedValue({ outcome: 'started', chatId: LIVE_BUILD_CHAT, turnId: BUILD_TURN_ID })
    const turn = scriptBuildTurn()
    h.readTurnStream.mockImplementation(turn.impl)
    const d = deps()
    const { rerender } = renderAt('chat-A', d.deps)
    await waitForGateOpen()
    type('build me a thing')
    fireEvent.keyDown(composer(), { key: 'Enter' })
    fireEvent.click(await screen.findByRole('button', { name: /^Build this plan$/ }))
    await waitFor(() => expect(h.buildFromPlan).toHaveBeenCalled())

    // Arrive at the chat the handoff actually runs in (a chatId prop swap on the SAME instance,
    // mirroring how every other sibling-chat guard in this file simulates navigation).
    rerender(
      <MemoryRouter initialEntries={['/x']}>
        <ConversationSurface chatId={LIVE_BUILD_CHAT} projectId="p1" projectName="VIP Movement" buildSessionDeps={d.deps} />
      </MemoryRouter>,
    )
    await waitFor(() => expect(h.getBuild).toHaveBeenCalledWith(LIVE_BUILD_CHAT))
    await waitFor(() =>
      expect(h.readTurnStream).toHaveBeenCalledWith(expect.objectContaining({ turnId: BUILD_TURN_ID })),
    )
    await turn.frame(T_PREVIEW())
    await waitFor(() => expect(screen.getByTestId('stop-turn')).toBeTruthy())
    // The owning chat's own Stop is real. `getAllBy` because R55's relocated control now sits on
    // the composer beside the build card's own — deliberately, and only until U17 deletes the
    // card. What this guard is about is the SIBLING, and that assertion below is unchanged.
    expect(screen.getAllByRole('button', { name: /^Stop$/i }).length).toBeGreaterThan(0)
    expect(screen.getByTestId('stop-turn')).toBeTruthy()

    // A THIRD, unrelated chat inherits none of it.
    h.getBuild.mockResolvedValue({ id: 'chat-B', kind: 'build', messages: [] })
    rerender(
      <MemoryRouter initialEntries={['/x']}>
        <ConversationSurface chatId="chat-B" projectId="p1" projectName="VIP Movement" buildSessionDeps={d.deps} />
      </MemoryRouter>,
    )
    await waitFor(() => expect(h.getBuild).toHaveBeenCalledWith('chat-B'))

    expect(screen.queryByTestId('stop-turn')).toBeNull()
    expect(screen.queryByRole('button', { name: /^Stop$/i })).toBeNull()
    // The relocated control is scoped by the same per-chat predicate, so it must be absent here
    // too — an unscoped one would hand a sibling a working Stop for a build it never started,
    // which is the exact defect this guard was written for.
    expect(screen.queryByTestId('stop-turn')).toBeNull()
    // Liveness: the sibling chat did render, so the two absences above are absences and not a
    // crashed tree.
    expect(composer()).toBeTruthy()
  })

  it('CC4: a reattached turn resubscribes ONCE on a truncation, then gives up honestly', async () => {
    // `fireRelayTurn` has had resume-once since the streamed-reply learning; this path mapped any
    // throw to 'truncated' and stopped, so one dropped socket after a reload reported "the
    // connection dropped" about a turn that was still running server-side.
    h.getBuild.mockResolvedValue({
      id: 'build-X',
      kind: 'build',
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
      kind: 'build',
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
    h.getBuild.mockResolvedValue({ id: 'build-X', kind: 'build', messages: [] })
    h.startTurn.mockRejectedValue(new Error('refused at the door'))
    const { deps: d } = deps()
    renderAt('build-X', d)
    await waitForGateOpen()

    type('build me a thing')
    fireEvent.keyDown(composer(), { key: 'Enter' })

    await waitFor(() => expect(screen.getByText(/could not be sent/i)).toBeTruthy())
    // The server persisted NOTHING, so the optimistic user bubble rolls back too — the
    // transcript must agree with the database (N8).
    //
    // SCOPED TO THE TRANSCRIPT, because the composer legitimately still holds the same words and
    // an unscoped `queryByText` matches the textarea too. That is not a detail of the assertion:
    // the two halves are the whole contract, and a test that could not tell them apart is how the
    // defect below survived.
    //
    // `waitFor`, NOT a bare assertion. The banner and the rollback are two state updates and the
    // banner is the one this test waits on; under a loaded runner the rollback's commit can land
    // a tick later. A bare read here passed alone and failed in the full suite, which is the
    // definition of a flake rather than a finding.
    await waitFor(() =>
      expect(within(screen.getByTestId('thread-messages')).queryByText('build me a thing')).toBeNull(),
    )

    // AND THE CITIZEN STILL HAS THEIR MESSAGE. This is the assertion the suite was missing, and
    // without it a P0 shipped: `onSent` used to fire before `startTurn` was attempted, so the
    // composer's send promise had already resolved by the time the refusal arrived. It emptied
    // itself, the later `onAbort` rejected a settled promise and did nothing, and the text and any
    // staged files were gone — on the 429-over-the-daily-cap path above all others.
    expect(composer().value).toBe('build me a thing')
  })

  it('a subscribe failure AFTER the 202 keeps the user bubble and says reload, not resend', async () => {
    h.getBuild.mockResolvedValue({ id: 'build-X', kind: 'build', messages: [] })
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

/**
 * THE SEND PROMISE IS THE CONTRACT, and these are the paths that broke it.
 *
 * `Composer.doSend` empties the box when `onSubmit` RESOLVES and keeps everything when it REJECTS.
 * That is the whole of R58/R59 — there is no optimistic clear and no restore path. So every way out
 * of the send has to settle the promise, and settle it the right way. Three did not:
 *
 *   - `onSent` fired before `startTurn` was attempted, so a refusal arrived at an already-resolved
 *     promise. The composer had emptied; the later `onAbort` did nothing. On a CONTINUING thread it
 *     fired on no network call whatsoever, which is every message after the first;
 *   - the double-Enter guard RETURNED, and a return is a resolve — so the second press emptied the
 *     composer while the first send was still in flight;
 *   - a bail-out taken after the reader had moved on settled nothing at all, so `handleSubmit`'s
 *     `finally` never ran, `sendingRef` kept naming that chat, and every later press there matched
 *     the stale guard and returned as though it had sent.
 */
describe('a refused send leaves the citizen holding their message', () => {
  /** A conversation that already has a turn in it — so the next send is NOT the first. */
  const continuing = () => ({
    id: 'build-X',
    kind: 'build',
    messages: [
      { id: 'm0', role: 'user', seq: 0, parts: [{ type: 'text', text: 'a visitor app' }] },
      { id: 'm1', role: 'assistant', seq: 1, parts: [{ type: 'text', text: 'Here you go.' }] },
    ],
  })

  it('keeps the text when startTurn refuses the SECOND message in a thread', async () => {
    // THE PATH THE P0 ACTUALLY TOOK. The first message goes through `createBuild`, which at least
    // had a network call behind its premature release; every message after it released the
    // composer on nothing at all.
    h.getBuild.mockResolvedValue(continuing())
    h.startTurn.mockRejectedValue(new Error('429 over the daily cap'))
    renderAt('build-X', deps().deps)
    await waitForGateOpen()

    type('and add a search box')
    fireEvent.keyDown(composer(), { key: 'Enter' })

    await waitFor(() => expect(screen.getByText(/could not be sent/i)).toBeTruthy())
    expect(composer().value).toBe('and add a search box')
    // The optimistic bubble still rolls back — the server persisted nothing.
    await waitFor(() =>
      expect(within(screen.getByTestId('thread-messages')).queryByText('and add a search box')).toBeNull(),
    )
  })

  it('empties the composer once the server has ACCEPTED, not before', async () => {
    // The other half: the fix must not hold the text hostage to the whole reply. A 202 means the
    // message is persisted and the turn runs detached, so that is the moment the box may clear —
    // well before any of the reply has streamed.
    h.getBuild.mockResolvedValue(continuing())
    h.readTurnStream.mockImplementation(() => new Promise(() => {})) // accepted, and still streaming
    renderAt('build-X', deps().deps)
    await waitForGateOpen()

    type('and add a search box')
    fireEvent.keyDown(composer(), { key: 'Enter' })

    await waitFor(() => expect(composer().value).toBe(''))
    expect(h.startTurn).toHaveBeenCalled()
  })

  it('a double-Enter in one tick does not empty the composer for the press it swallowed', async () => {
    // The second keydown lands in the SAME tick, so it hits the dedup guard. That guard used to
    // return — and a return resolves — so it cleared the box while the first send was still in
    // flight. If that first send then failed, the message it was holding was already gone.
    h.getBuild.mockResolvedValue(continuing())
    let releaseStart
    h.startTurn.mockImplementation(() => new Promise((resolve) => { releaseStart = resolve }))
    renderAt('build-X', deps().deps)
    await waitForGateOpen()

    type('do not lose this')
    fireEvent.keyDown(composer(), { key: 'Enter' })
    fireEvent.keyDown(composer(), { key: 'Enter' })

    // `waitFor` because the send awaits the attachment build before it reaches the wire — both
    // keydowns land first, which is the whole point of the guard being synchronous.
    await waitFor(() => expect(h.startTurn).toHaveBeenCalledTimes(1))
    // The swallowed press said nothing and cleared nothing.
    expect(composer().value).toBe('do not lose this')
    expect(screen.queryByText(/did not send/i)).toBeNull()

    releaseStart()
    await waitFor(() => expect(composer().value).toBe(''))
  })

  it('a refusal after the reader has moved on still frees that chat’s Send', async () => {
    // The wedge. Nothing settled the promise on this path, so `handleSubmit`'s `finally` never ran
    // and `sendingRef` went on naming this chat forever — every later press there matched the
    // stale double-Enter guard and returned as though it had sent, silently, for the session.
    h.getBuild.mockResolvedValue(continuing())
    h.startTurn.mockRejectedValueOnce(new Error('refused at the door'))
    const { deps: d } = deps()
    const { rerender } = renderAt('build-X', d)
    await waitForGateOpen()

    type('first attempt')
    fireEvent.keyDown(composer(), { key: 'Enter' })
    // Away and back while the refusal is in flight.
    rerender(
      <MemoryRouter initialEntries={['/x']}>
        <ConversationSurface chatId="build-Y" projectId="p1" projectName="VIP Movement" buildSessionDeps={d} />
      </MemoryRouter>,
    )
    rerender(
      <MemoryRouter initialEntries={['/x']}>
        <ConversationSurface chatId="build-X" projectId="p1" projectName="VIP Movement" buildSessionDeps={d} />
      </MemoryRouter>,
    )
    await waitForGateOpen()

    h.startTurn.mockResolvedValue(undefined)
    type('second attempt')
    fireEvent.keyDown(composer(), { key: 'Enter' })

    // It actually sent. Before the fix this press matched the stale guard and vanished.
    await waitFor(() => expect(h.startTurn).toHaveBeenCalledTimes(2))
  })
})
