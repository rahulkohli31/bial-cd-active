/**
 * THE COMPOSER CONTRACT. One gate, and its only term is turn state. Mode appears nowhere in
 * it — a mode is a tool-access level on the same conversation, and using it as a composer gate is
 * what produced the Write dead end. The gate withholds *sending*, not typing: the box and attach
 * stay live so the citizen can compose while they wait.
 *
 * Four defects live here. The composer went dead mid-reply and stole focus. G1: the gate read
 * "open" while the adopt round-trip was unresolved over a possibly-live build, and its fix has
 * FOUR arms — miss the no-anchor one and every ordinary chat bricks. G2: `generating` was global,
 * so a mid-stream switch gated the new chat on the old chat's turn. G3: a typed draft died on a
 * reload, on a switch, and to a refinement chip; the chips are gone, so nothing canned may seed it.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act, cleanup, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

const h = vi.hoisted(() => ({
  loadBuilds: vi.fn(), getBuild: vi.fn(),
  listProjectConversations: vi.fn(), buildUserParts: vi.fn(),
  startTurn: vi.fn(), readTurnStream: vi.fn(), buildFromPlan: vi.fn(),
  resolvePlanOptions: vi.fn(),
  stop: vi.fn(), getStatus: vi.fn(), relaunchPreview: vi.fn(),
  notifyUsageChanged: vi.fn(), releaseUploadedAttachments: vi.fn(),
}))

vi.mock('../../utils/usage', () => ({ notifyUsageChanged: h.notifyUsageChanged }))

vi.mock('../../utils/builderHistory', () => ({
  loadBuilds: h.loadBuilds, getBuild: h.getBuild, deriveTitle: (t) => (t || '').slice(0, 40),
}))
// SPREAD THE ORIGINAL: `handleBuildIt` mints the new build chat's id through the shared `uuidv7`
// export, so a factory naming only `listProjectConversations` would leave it undefined.
vi.mock('../../utils/conversationApi', async (importOriginal) => ({
  ...(await importOriginal()),
  // The send path creates the chat before its first upload; stubbed so no network is reached.
  createConversation: async () => ({ id: 'conv-created' }),
  listProjectConversations: h.listProjectConversations,
}))
vi.mock('../../components/layout/Navbar', () => ({ default: () => null }))
vi.mock('../../utils/attachmentStore', async (orig) => ({
  ...(await orig()),
  buildUserParts: h.buildUserParts,
  releaseUploadedAttachments: (...a) => h.releaseUploadedAttachments(...a),
}))
// `switchMode` is GONE from this list: the route it posted to no longer exists, and a
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

/** BY ITS HANDLE, NOT BY ITS HINT: while a plan offer waits for an answer, the box's locked
 *  placeholder text is replaced by the reason — a lookup by hint stops finding the composer
 *  exactly when a test follows a build through to an offer. */
const composer = () => screen.getByTestId('composer-input')
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
  h.getBuild.mockResolvedValue(null)
  h.loadBuilds.mockResolvedValue([])
  h.listProjectConversations.mockResolvedValue([])
  h.buildUserParts.mockImplementation(async (t) => [{ type: 'text', text: t }])
})
afterEach(() => cleanup())

describe('the gate withholds SENDING, not typing', () => {
  it('mid-reply: the box takes input, attach is live, send is unavailable — the mode pill is gone entirely', async () => {
    h.readTurnStream.mockImplementation(() => new Promise(() => {}))
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
    // AN INERTNESS GUARD, not a frozen-pill check: `ModeSwitcher` and the axis it switched are
    // both gone, so no control exists to freeze. The liveness assertions above already prove
    // the page rendered rather than threw, so this absence means what it says.
    expect(screen.queryByRole('button', { name: /^Mode:/ })).toBeNull()
  })

  it('focus never leaves the box — not at the turn\'s start, not at its terminal', async () => {
    // HONEST LIMIT: jsdom does not implement blur-on-disable, so the `activeElement` assertions
    // below pin only the no-focus-grab half; the actual blur is covered by a browser run instead.
    h.getBuild.mockResolvedValue({ id: 'build-X', kind: 'build', messages: [] })
    // Hold the turn OPEN across the assertion — a same-tick settle re-enables the composer before
    // focus is read, hiding the exact defect this test catches.
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

    expect(screen.getByTestId('composer-gate-note').textContent).toMatch(/checking whether a build/i)
    await waitForGateOpen()
    expect(screen.queryByTestId('composer-gate-note')).toBeNull()

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

describe('the gate waits for the adopt round-trip', () => {
  it('THE COMMON CASE: a chat with no build anchor resolves on mount and send is available', async () => {
    // The arm that would brick the whole product if missed: `reattachToLiveBuild` early-returns
    // when there's no `build_in_progress` anchor — every ordinary chat — so send must not key on
    // `session.reattach` settling alone.
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
    // with only a vanishing toast would recreate the dead-end class.
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

    h.getStatus.mockResolvedValue(statusResp({ sessionId: 'live-7', projectId: 'p1', status: 'ended' }))
    fireEvent.click(screen.getByRole('button', { name: /^Retry$/ }))
    await waitFor(() => expect(screen.queryByTestId('composer-gate-note')).toBeNull())
    fireEvent.keyDown(composer(), { key: 'Enter' })
    await waitFor(() => expect(h.startTurn).toHaveBeenCalled())
  })
})

describe('an in-flight turn belongs to ONE chat', () => {
  it('a turn streaming in chat A does not gate chat B\'s send', async () => {
    h.readTurnStream.mockImplementation(() => new Promise(() => {})) // A's reply never lands
    h.getBuild.mockResolvedValue({ id: 'chat-A', kind: 'build', messages: [] })
    const { deps: d } = deps()
    const { rerender } = renderAt('chat-A', d)
    await waitForGateOpen()
    type('a question')
    fireEvent.keyDown(composer(), { key: 'Enter' })
    // TWO ARGS: `startTurn(id, message)`. The `create` block is gone — the row is created by its
    // own call before the upload now — and `deps` is left to its default. `toHaveBeenCalledWith`
    // checks argument COUNT too, so this also catches a call that quietly regrows a third.
    // `expect.anything()` for the payload: this test is about which chat the call belongs to.
    await waitFor(() =>
      expect(h.startTurn).toHaveBeenCalledWith('chat-A', expect.anything()),
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
    // Same two-arg shape as chat A's assertion above.
    await waitFor(() =>
      expect(h.startTurn).toHaveBeenCalledWith('chat-B', expect.anything()),
    )
  })

  it('★ a reply that ends after the reader has left says nothing in the chat they moved to', async () => {
    // THE ASYMMETRY THIS IS WRITTEN AGAINST: the re-attach path drops paint once the reader has
    // moved on, but the SEND path wrote its banners outside that guard — so a connection dying in
    // chat A after the citizen opened chat B put "The reply stalled" on chat B's screen instead.
    let dropTheConnection
    h.readTurnStream.mockImplementation(() => new Promise((resolve) => { dropTheConnection = resolve }))
    h.getBuild.mockResolvedValue({ id: 'chat-A', kind: 'build', messages: [] })
    const { deps: d } = deps()
    const { rerender } = renderAt('chat-A', d)
    await waitForGateOpen()
    type('a question')
    fireEvent.keyDown(composer(), { key: 'Enter' })
    await waitFor(() => expect(h.startTurn).toHaveBeenCalledWith('chat-A', expect.anything()))

    // They open a sibling while A's reply is still coming — the same instance, flat routing.
    h.getBuild.mockResolvedValue({
      id: 'chat-B',
      kind: 'build',
      messages: [{ id: 'b0', role: 'user', seq: 0, parts: [{ type: 'text', text: 'about the other app' }] }],
    })
    rerender(
      <MemoryRouter initialEntries={['/x']}>
        <ConversationSurface chatId="chat-B" projectId="p1" projectName="VIP Movement" buildSessionDeps={d} />
      </MemoryRouter>,
    )
    await waitFor(() => expect(h.getBuild).toHaveBeenCalledWith('chat-B'))
    await screen.findByText('about the other app')

    // …and only THEN does chat A's connection die.
    await act(async () => { dropTheConnection('stalled') })

    expect(screen.queryByText(/The reply stalled/i)).toBeNull()
    expect(screen.queryByText(/The connection dropped/i)).toBeNull()
    // LIVENESS: chat B is still on screen and still itself, so the two absences are absences
    // rather than a surface that threw its way to an empty page.
    expect(screen.getByText('about the other app')).toBeTruthy()
    expect(composer()).toBeTruthy()
  })
})

describe('a typed draft survives', () => {
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
    // A first message now makes TWO calls — `createConversation`, then `startTurn` — and this
    // test is about the second one failing. The create is stubbed to succeed at the top of the
    // file, so what is exercised here is `startTurn`'s catch, the same path every later
    // message's refusal takes. (The create's own refusal is pinned in
    // `ConversationSurface-projectfirst.test.jsx`.)
    h.getBuild.mockResolvedValue(null) // seq 0 → the FIRST message, so the create runs too
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
  // The three hardcoded refinement chips (dark mode, a data table, a mobile layout) are DELETED,
  // so what is pinned here is their absence — the regression this guards against is a well-meant
  // re-introduction, since a suggestion that cannot know what the app is has nothing to suggest.
  it('leaves the composer as the only way to ask for the next change', async () => {
    // BUILD-IT IS A HANDOFF, not a flip: the turn runs in a brand-new build chat the offer
    // creates, and only THAT chat's own hydration watches it — simulated here by a chatId prop
    // swap on the SAME instance, as every sibling-chat guard in this file does.
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

    // THE CANNED CHIPS NEVER COME BACK — no button offers a follow-up the model was
    // never asked about.
    expect(screen.queryByRole('button', { name: /dark mode|data table|mobile layout/i })).toBeNull()
    // The composer is how the next change gets asked for — proven by actually asking for one,
    // not by an empty textbox that was always going to be empty on a chat nobody typed in yet.
    h.startTurn.mockClear()
    type('add a dark mode toggle')
    fireEvent.keyDown(composer(), { key: 'Enter' })
    // Two args, as everywhere else: the turn carries the message and nothing about the row.
    // Not this chat's first message either (the reattached build turn already occupies seq 0),
    // so no create call precedes it — pinned below.
    await waitFor(() =>
      expect(h.startTurn).toHaveBeenCalledWith(
        NEW_BUILD_CHAT,
        expect.objectContaining({ text: 'add a dark mode toggle' }),
      ),
    )
  })
})


// The meter has to settle without a reload — the signal fires from the ONE function every turn
// terminal routes through, which is what makes the failed and stopped arms below free rather
// than three separate call sites to remember.
describe('the usage meter settles at every turn terminal', () => {
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


// CC1–CC4 — opening one chat must never damage another chat's live build, and reloading
// mid-build must not erase the story. All four live in this file's neighbourhood because they
// share the adopt/reattach predicates the composer gate is built on.
describe('cross-chat build scoping and reload fidelity', () => {
  const liveStatus = (sessionId) =>
    statusResp({ sessionId, projectId: 'p1', status: 'building' })

  it('adopting a SIBLING chat with a stale anchor does not tear down the live session', async () => {
    // Stamp the ownership refs BEFORE classifying — reversed, every same-session guard is
    // tautological, and worse here: `session.reattach()`'s first act is a synchronous `reset()`,
    // so a sibling's adopt would kill the running build's heartbeat and lock renewal.
    h.getBuild.mockResolvedValue(withAnchor('live-7'))
    h.getStatus.mockResolvedValue(liveStatus('live-7'))
    const d = deps()
    const { rerender } = renderAt('chat-A', d.deps)
    await waitFor(() => expect(h.getStatus).toHaveBeenCalledWith('live-7'))
    await waitFor(() => expect(screen.getByTestId('stop-turn')).toBeTruthy())

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

  it('the OWNING chat still reattaches on its own reload', async () => {
    // The other arm — the guard must not be so broad that it breaks legitimate reattach.
    h.getBuild.mockResolvedValue(withAnchor('live-7'))
    h.getStatus.mockResolvedValue(liveStatus('live-7'))
    const { deps: d } = deps()
    renderAt('build-X', d)

    await waitFor(() => expect(h.getStatus).toHaveBeenCalledWith('live-7'))
    await waitFor(() => expect(screen.getByTestId('composer-gate-note').textContent).toMatch(/building your app/i))
  })

  it('a reload mid-build still renders the stored step history', async () => {
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
    // ONE GROUP, NOT TWO — live/reload parity, not a styling preference. The projection stores
    // one MESSAGE per step while the live path streams every step onto one message, so without
    // merging, a build watched live shows one group and the same build after reload shows many.
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

  it('a sibling chat renders no live build bubble, and therefore no Stop button', async () => {
    // The narrative used to be project-scoped while the composer gate was chat-scoped, so a
    // sibling rendered another chat's build complete with a WORKING Stop. It is scoped by the
    // same per-chat predicate as the gate (`generatingChatId === buildId`) now.
    //
    // Build-it is a handoff: pressing it in `chat-A` no longer makes `chat-A` narrate the build —
    // a brand-new chat does — so "a sibling" here includes `chat-A` itself.
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
    // `getAllBy` because the relocated Stop control now sits on the composer beside the build
    // card's own, deliberately — what this guard is about is the SIBLING below, unchanged.
    expect(screen.getAllByRole('button', { name: /^Stop$/i }).length).toBeGreaterThan(0)
    expect(screen.getByTestId('stop-turn')).toBeTruthy()

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

  it('a reattached turn resubscribes ONCE on a truncation, then gives up honestly', async () => {
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

  it('a SECOND truncation is a real drop and says so', async () => {
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


// `startTurn` resolving is a 202: the message is persisted and the reply runs detached, so a
// failure AFTER that point is subscription plumbing, not a refused send — "could not be sent"
// over a persisted message would invite a duplicate resend.
describe('the send-failure catch splits on whether the turn was accepted', () => {
  it('a startTurn refusal rolls back BOTH bubbles and says the message was not sent', async () => {
    h.getBuild.mockResolvedValue({ id: 'build-X', kind: 'build', messages: [] })
    h.startTurn.mockRejectedValue(new Error('refused at the door'))
    const { deps: d } = deps()
    renderAt('build-X', d)
    await waitForGateOpen()

    type('build me a thing')
    fireEvent.keyDown(composer(), { key: 'Enter' })

    await waitFor(() => expect(screen.getByText(/could not be sent/i)).toBeTruthy())
    // SCOPED TO THE TRANSCRIPT: an unscoped `queryByText` also matches the composer, which
    // legitimately still holds the same words — a test that can't tell the two apart is how the
    // defect below survived.
    //
    // `waitFor`, NOT a bare assertion: the banner and the rollback are two separate state updates,
    // and under a loaded runner the rollback's commit can land a tick after the banner.
    await waitFor(() =>
      expect(within(screen.getByTestId('thread-messages')).queryByText('build me a thing')).toBeNull(),
    )

    // AND THE CITIZEN STILL HAS THEIR MESSAGE: `onSent` used to fire before `startTurn` was
    // attempted, so a refusal arrived after the composer had already cleared itself and the text
    // was gone — on the 429-over-the-daily-cap path above all others.
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
 * THE SEND PROMISE IS THE CONTRACT: `Composer.doSend` clears only on a resolved `onSubmit` and
 * keeps everything on a rejected one, so every way out of send must settle that promise, and
 * settle it correctly. Three paths did not — each test below pins the one it broke.
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
    // THE PATH THE BUG ACTUALLY TOOK: the first message's release is at least behind a network
    // call (`createBuild`); every message after it released the composer on nothing at all.
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
    // The other half: the fix must not hold the text hostage to the whole reply — a 202 means the
    // message is persisted, so the box may clear right there, well before the reply streams.
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

    // Resolved with the real 202 SHAPE, not with nothing: a 202 body now carries the chat's
    // occupancy beside the turn id, and a mock that resolves `undefined` stands in for
    // a contract this endpoint no longer has.
    releaseStart({ turnId: 't1', contextTokens: null })
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

    h.startTurn.mockResolvedValue({ turnId: 't1', contextTokens: null }) // the real 202 shape
    type('second attempt')
    fireEvent.keyDown(composer(), { key: 'Enter' })

    // It actually sent. Before the fix this press matched the stale guard and vanished.
    await waitFor(() => expect(h.startTurn).toHaveBeenCalledTimes(2))
  })

  it('an upload that fails after the reader has moved on frees Send without speaking over the chat they are in', async () => {
    // THE SAME WEDGE, ONE ARM EARLIER. `startTurn`'s abort was already unconditional; the upload
    // arm's was gated on `stillHere()`, so an upload that failed after a chat switch settled
    // nothing. `ComposerBox` is one long-lived instance, so its `sending` stayed true and greyed
    // Send in EVERY chat until the page was reloaded.
    h.getBuild.mockResolvedValue(continuing())
    let failUpload = () => {}
    h.buildUserParts.mockImplementationOnce(
      () => new Promise((_resolve, reject) => { failUpload = () => reject(new Error('the store refused it')) }),
    )
    const { deps: d } = deps()
    const { rerender } = renderAt('build-X', d)
    await waitForGateOpen()

    type('what does this say?')
    fireEvent.keyDown(composer(), { key: 'Enter' })

    rerender(
      <MemoryRouter initialEntries={['/x']}>
        <ConversationSurface chatId="build-Y" projectId="p1" projectName="VIP Movement" buildSessionDeps={d} />
      </MemoryRouter>,
    )
    await waitForGateOpen()
    await act(async () => { failUpload() })

    // THE CHAT THEY LEFT DOES NOT TALK OVER THE ONE THEY ARE READING. Asserted as an absence with
    // a liveness assertion beside it, so a surface that never rendered cannot pass by being empty.
    expect(composer()).toBeTruthy()
    expect(screen.queryByTestId('urgent-banner')).toBeNull()

    // AND SEND WORKS HERE, which is the half the gated abort broke.
    h.startTurn.mockResolvedValue({ turnId: 't1', contextTokens: null })
    type('a message in the chat I am actually in')
    fireEvent.keyDown(composer(), { key: 'Enter' })
    await waitFor(() => expect(h.startTurn).toHaveBeenCalledTimes(1))
  })

  it('an upload that lands after the reader has moved on is given back rather than left behind', async () => {
    // The files are on the server by then, linked to the chat that was left, and no message will
    // ever reference them — but they still count against that conversation's twenty. Four switches
    // and the next upload there is refused with "this conversation has reached its limit of 20
    // attachments" on a chat displaying none, with only an aged-out reclaimer to take them back.
    h.getBuild.mockResolvedValue(continuing())
    const uploaded = [
      { type: 'file', attachmentId: 'att_1', kind: 'document', name: 'a.pdf', mediaType: 'application/pdf' },
      { type: 'text', text: 'what does this say?' },
    ]
    let landUpload = () => {}
    h.buildUserParts.mockImplementationOnce(
      () => new Promise((resolve) => { landUpload = () => resolve(uploaded) }),
    )
    const { deps: d } = deps()
    const { rerender } = renderAt('build-X', d)
    await waitForGateOpen()

    type('what does this say?')
    fireEvent.keyDown(composer(), { key: 'Enter' })

    rerender(
      <MemoryRouter initialEntries={['/x']}>
        <ConversationSurface chatId="build-Y" projectId="p1" projectName="VIP Movement" buildSessionDeps={d} />
      </MemoryRouter>,
    )
    await waitForGateOpen()
    await act(async () => { landUpload() })

    await waitFor(() => expect(h.releaseUploadedAttachments).toHaveBeenCalledWith(uploaded))
    // AND NOTHING WAS SENT INTO THE CHAT THEY MOVED TO, which is the other half of abandoning.
    expect(h.startTurn).not.toHaveBeenCalled()
  })
})

/**
 * THE DOCUMENT CAP, AT THE SEAM.
 *
 * `validatePdfPerMessageCap` is unit-tested next door and stayed green through the whole life of
 * this defect, because the helper was never the missing piece — the CALL was. Delete the two lines
 * in `handleSubmit` that ask it and every unit test in `attachmentInput.test.js` still passes while
 * a citizen sends three PDFs into a turn the server will bounce. So this asserts the wiring:
 * a third document is refused HERE, in the composer, before a turn exists.
 *
 * AND IT ASSERTS WHICH REFUSAL, WHICH IS THE HALF THAT MOTIVATED THE FIX
 *
 * Without this check the citizen still gets stopped — one step later, by the token gate, which
 * says "start a new chat". That advice does not work: the cap counts documents PER MESSAGE, so the
 * new chat refuses the identical message. So "was a refusal shown" is not enough of a claim. The
 * refusal has to be THIS one, and it must not be the conversation cap's sentence — the two caps
 * answer different questions (per message vs cumulative) and a test that accepted either would go
 * green on the wrong one.
 */
// THE PER-MESSAGE DOCUMENT CAP IS GONE, and its tests with it. It was two, and it
// shipped as the stopgap that stopped a 61-page PDF blowing the context budget. The count was
// the belt beside the page cap's braces — and the page cap has since gone the same way, once
// the flat per-document charge it was sized against stopped existing. One rule governs a message
// now - five files, any mix - so a citizen never has to know which of their files the platform
// considers expensive.

describe('an upload the server refuses says WHY, not "try again"', () => {
  /* TWO EMITTERS, ONE BANNER, AND THE ONE THAT KNEW NOTHING WENT LAST.
     `fireRelayTurn` catches an upload failure and writes the server's own sentence to the urgent
     slot, then aborts the send. The abort used to reject with a plain `Error`, and a
     non-`SendRefusal` is not silence to `ComposerBox` — it is the GENERIC line. So the specific
     sentence was written and immediately overwritten.

     Found in a browser, not here: a real refused PDF, `413 POST /api/attachments` carrying the
     server's own sentence in the network log, and "That message did not send … try again." on
     screen. Retrying re-sends the identical file into the identical refusal, so the advice the
     citizen was actually given could never work — the failure `attachmentInput.ts` names as
     advice that leads nowhere.

     THE FIXTURE MOVED WITH THE CAP. It used to be the page-cap sentence, which no longer exists;
     what this test is really about is that ANY server sentence survives to the banner, so it now
     carries a refusal the door still emits. */

  const REFUSAL = 'That file is password-protected. Remove the password and attach it again.'

  it('shows the server’s sentence and keeps the message in the box', async () => {
    h.getBuild.mockResolvedValue({ id: 'build-X', kind: 'build', messages: [] })
    h.buildUserParts.mockRejectedValue(new Error(REFUSAL))
    const { deps: d } = deps()
    renderAt('build-X', d)
    await waitForGateOpen()

    type('what does this say?')
    fireEvent.keyDown(composer(), { key: 'Enter' })

    const banner = await screen.findByTestId('urgent-banner')
    expect(banner.textContent).toContain(REFUSAL)
    // THE MUTANT THIS CATCHES: reject the abort with a bare `Error` again and the generic line
    // replaces the one above. Asserted as an absence with the presence assertion beside it, so a
    // banner that never rendered cannot pass this by being empty.
    expect(banner.textContent).not.toMatch(/try again/i)

    // NOTHING WAS SENT AND NOTHING WAS TAKEN AWAY — the other half of a silent refusal. A resolve
    // here would have emptied the composer for a message the server never received.
    expect(h.startTurn).not.toHaveBeenCalled()
    expect(composer().value).toBe('what does this say?')
  })
})
