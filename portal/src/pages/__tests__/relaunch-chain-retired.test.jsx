/**
 * THE RELAUNCH CHAIN IS INERT — the characterization this unit was allowed to delete against.
 *
 * WHY THIS EXISTS
 *
 * `RelaunchAffordance` and its four render sites went in, and `LivePreview` was left holding
 * an unread `onRelaunch` prop. Everything above that unread prop — `handleRelaunch`, the
 * session hook's `relaunch()`, the 409 arms that set `blocked`, and the block banner with its
 * Force-end button — was reachable-looking code hanging off a callback nobody consumes.
 *
 * Written BEFORE that deletion, and it stays green after it: every assertion below must hold
 * identically on both sides of the commit, so a red here means the deletion changed behaviour
 * rather than removing dead weight.
 *
 * The block banner has TWO producers, and driving only one would prove nothing about the half
 * the same commit also deletes: `start()`'s 409 (unreachable — a send is a TURN, nothing calls
 * `session.start()`) and `relaunch()`'s 409 (its one caller is wired to the unread prop, BUT
 * `relaunchPreview` itself is still called directly in production by `StartAppControl` and
 * `RailComposer` — a LIVE path whose 409 must answer in the workspace, not the banner).
 *
 * Every absence is paired with a liveness assertion, since "no banner" is also true of a
 * surface that threw on render. And the frame scenarios are the dangerous half: `relaunching`
 * fed the booleans deciding whether the iframe stays MOUNTED, and unmounting it kills a
 * container the server is still serving (`AppPaneHost.tsx` has the failure mode at length).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, cleanup, fireEvent, act } from '@testing-library/react'
import LivePreview from '../../components/LivePreview'

const h = vi.hoisted(() => ({
  loadBuilds: vi.fn(), getBuild: vi.fn(),
  listProjectConversations: vi.fn(), buildUserParts: vi.fn(),
  startTurn: vi.fn(), readTurnStream: vi.fn(), buildFromPlan: vi.fn(), stopTurn: vi.fn(),
  resolvePlanOptions: vi.fn(),
  // `start` is a PROBE, not a fixture, and it is deliberately still here after the client member
  // it shadowed was deleted. It is handed to the injected client below and armed with the 409 that
  // used to raise the block banner; the assertion is that nothing on this surface reaches it —
  // which was true while the hook still consumed a `start`, and is true structurally now.
  start: vi.fn(), stop: vi.fn(), getStatus: vi.fn(), relaunchPreview: vi.fn(),
  fetchSaveState: vi.fn(), fetchPreviewState: vi.fn(),
}))

vi.mock('../../utils/builderHistory', () => ({
  loadBuilds: h.loadBuilds, getBuild: h.getBuild, deriveTitle: (t) => (t || '').slice(0, 40),
}))
vi.mock('../../utils/conversationApi', async (orig) => ({
  ...(await orig()),
  // The send path creates the chat before its first upload; stubbed so no network is reached.
  createConversation: async () => ({ id: 'conv-created' }),
  listProjectConversations: h.listProjectConversations,
}))
vi.mock('../../components/layout/Navbar', () => ({ default: () => null }))
vi.mock('../../utils/attachmentStore', async (orig) => ({ ...(await orig()), buildUserParts: h.buildUserParts }))
vi.mock('../../utils/turnStreamApi', async (orig) => ({
  ...(await orig()),
  startTurn: (...a) => h.startTurn(...a),
  readTurnStream: (...a) => h.readTurnStream(...a),
  buildFromPlan: (...a) => h.buildFromPlan(...a),
  stopTurn: (...a) => h.stopTurn(...a),
  resolvePlanOptions: (...a) => h.resolvePlanOptions(...a),
}))
// `StartAppControl` imports `relaunchPreview` DIRECTLY from this module rather than through the
// injected client, so the live 409 arm can only be primed here — the client bag cannot reach it.
vi.mock('../../utils/buildSessionApi', async (orig) => ({
  ...(await orig()),
  fetchSaveState: (...a) => h.fetchSaveState(...a),
  fetchPreviewState: (...a) => h.fetchPreviewState(...a),
  relaunchPreview: (...a) => h.relaunchPreview(...a),
}))

import {
  FakeEventSource, primeTurn, renderBuilder, send, statusResp,
  primeStandbyReattach, findStartAppControl, planReply, turnStreaming, ENDED_RESP,
} from './_builderSession.jsx'
import { BuildSessionAlreadyActiveError } from '../../utils/buildSessionApi'

const SANDBOX_URL = 'https://app-xyz.example.azurecontainerapps.io/'
const SANDBOX_URL_2 = 'https://app-abc.example.azurecontainerapps.io/'
// The origin the beacon must claim to be believed — derived, never hand-typed, so a change to
// SANDBOX_URL can't quietly drift out of step with what `vouch()` below sends as `e.origin`.
const SANDBOX_ORIGIN = new URL(SANDBOX_URL).origin
const CHAT_ID = 'build-X'

/**
 * The injected client, assembled HERE rather than through `makeClient`, so this file decides
 * which members exist — `makeClient` no longer carries `start` at all, and this scenario needs to
 * hand one in to prove nothing reaches it. An extra member on the bag is inert: the hook only ever
 * calls what it names.
 */
const client = () => ({
  start: h.start, relaunchPreview: h.relaunchPreview, stop: h.stop, getStatus: h.getStatus,
})
const deps = () => ({ client: client(), eventSourceFactory: () => new FakeEventSource('x') })

/** The device card that carries the reveal's opacity — the handle every frame assertion uses. */
const card = (container) => container.querySelector('[data-testid="device-card"]')

/** The framed document vouching for itself — `bial:app-mounted`, sent from the CURRENT iframe's
 *  own window at the sandbox origin. This is the only thing that reveals the card now: `load`
 *  fires for a bodyless 502 exactly as it does for a real page, so a frame scenario that still
 *  revealed on `load` alone would pass over the one failure this mechanism exists to catch.
 *  `path` MUST be present — `isMountedBeaconFor` rejects a beacon without one — so this always
 *  sends `'/'`; the sandbox root's framed path strips to `''`, which every reported path matches. */
function vouch(container) {
  const iframe = container.querySelector('iframe')
  act(() => {
    window.dispatchEvent(new MessageEvent('message', {
      data: { type: 'bial:app-mounted', path: '/' },
      origin: SANDBOX_ORIGIN,
      source: iframe.contentWindow,
    }))
  })
}

/** The banner this unit deletes, found by its rendered copy, not by a testid.
 *
 *  ITS FORCE-END BUTTON USED TO BE ASSERTED HERE TOO, and that assertion went with force-end
 *  itself rather than outliving its subject: the button was the banner's, so "no banner" already
 *  covers it here, and what a citizen can still see is pinned directly — and exhaustively over
 *  every prop the component accepts — by the RETIREMENT GUARD in
 *  `components/chat/__tests__/SessionBanners.test`.
 *  There is no force-end left to render from any state: the client, the hook wrapper and the
 *  backend route all went in one change. */
const blockBanner = () => screen.queryByText(/you already have a build running/i)

beforeEach(() => {
  vi.clearAllMocks()
  sessionStorage.clear()
  primeTurn(h)
  h.stop.mockResolvedValue(ENDED_RESP)
  h.getStatus.mockResolvedValue(statusResp())
  h.getBuild.mockResolvedValue({ id: CHAT_ID, kind: 'build', messages: [] })
  h.loadBuilds.mockResolvedValue([])
  h.listProjectConversations.mockResolvedValue([])
  h.buildUserParts.mockImplementation(async (t) => [{ type: 'text', text: t }])
  h.fetchSaveState.mockResolvedValue({ dirty: false })
  h.fetchPreviewState.mockResolvedValue({
    state: 'unknown', alive: false, previewUrl: null,
    occupyingProjectName: null, occupyingProjectId: null, restorable: null,
  })
  h.relaunchPreview.mockResolvedValue({
    appId: 'a1', previewUrl: SANDBOX_URL, status: 'ready', ready: true, restoredFromFailedBuild: false,
  })
})
afterEach(cleanup)

describe('the block banner cannot reach the tree — from EITHER producer', () => {
  it('arm 1, start’s 409: a send is a TURN, so the start that raised `blocked` never fires', async () => {
    // The 409 is armed on the start the injected client exposes. If any path on this surface still
    // provisioned a session, this would raise the banner — which is exactly the point: none does.
    h.start.mockRejectedValue(new BuildSessionAlreadyActiveError('You already have a build running.', 'sess-9'))
    h.readTurnStream.mockImplementation(turnStreaming(planReply()))

    renderBuilder({ deps: deps() })
    await send('build me a visitor pass tracker')
    await screen.findByRole('button', { name: /^Build this plan$/ })

    // LIVENESS FIRST — a surface that threw on render would also have no banner.
    expect(screen.getByTestId('composer-input')).toBeTruthy()
    expect(h.start).not.toHaveBeenCalled()
    expect(blockBanner()).toBeNull()
  })

  it('arm 2, relaunch’s 409: the LIVE relaunch path answers in the pane, never in the banner', async () => {
    // `relaunchPreview` genuinely runs here — `StartAppControl` calls the module function — so the
    // 409 arrives on a reachable path. What it must NOT do is raise the banner, because the hook's
    // `relaunch()` (the half that mapped it onto `blocked`) has no caller.
    h.relaunchPreview.mockRejectedValue(
      new BuildSessionAlreadyActiveError('You already have a build running.', 'sess-9'),
    )
    const standby = primeStandbyReattach(h, { chatId: CHAT_ID, projectId: 'p1' })
    renderBuilder({ deps: deps(), hasSavedBuild: true })
    await waitFor(() => expect(h.getStatus).toHaveBeenCalled())

    fireEvent.click(await findStartAppControl())
    // The arm really fired…
    await waitFor(() => expect(h.relaunchPreview).toHaveBeenCalledWith({ projectId: 'p1' }))
    // …and the pane survives it and offers its one control again — the LIVENESS half of this arm,
    // without which "no banner" would also be true of a surface that had thrown.
    expect(await findStartAppControl()).toBeTruthy()
    expect(screen.getByTestId('composer-input')).toBeTruthy()
    // …never the banner.
    expect(blockBanner()).toBeNull()

    standby.settle()
  })
})

describe('frame survival — the case where an unmount kills a live container', () => {
  // A framed, pardoned preview: `status: 'ended'` + `serving` is the state in which the
  // server is STILL SERVING the container under an idle lease. `showTerminal` must stay false,
  // `frameContext` true and `framePending` true, or the iframe comes down over a live app.
  // (`serving` is what `completedLive` was renamed to when liveness moved onto the address — same
  // state, a name that no longer also claims a build succeeded.)
  const framedAndPardoned = (props = {}) =>
    render(<LivePreview previewUrl={SANDBOX_URL} status="ended" serving {...props} />)

  it('an ended-but-live preview keeps its frame mounted, shows no terminal card, and keeps the labelled wait', () => {
    const { container } = framedAndPardoned()

    // frameContext → the iframe exists at all.
    const iframe = container.querySelector('iframe')
    expect(iframe).toBeTruthy()
    expect(iframe.getAttribute('src')).toBe(SANDBOX_URL)
    // ★ NO ENDED CARD over a container that is still serving — and there is no ended card LEFT to
    // draw. That placeholder was a verdict about the citizen's WORKSPACE, and the workspace map
    // owns every one of those now; what survives here is the frame and the covers over it.
    expect(screen.queryByTestId('preview-ended-card')).toBeNull()
    expect(container.textContent).not.toMatch(/no longer running/i)
    // framePending → true: mounted but not yet loaded, so the wait is up and LABELLED. The label
    // says "Opening" rather than "Starting" now: this pane is mounted only once the platform has
    // watched the app ANSWER a request, so the starting is over and this frame's own document is
    // the only thing still pending.
    expect(card(container).className).toMatch(/opacity-0/)
    expect(container.textContent).toMatch(/opening your app/i)
  })

  it('…and the framed document’s own beacon resolves that wait — its `load` alone does not', () => {
    const { container } = framedAndPardoned()
    const iframe = container.querySelector('iframe')

    // `load` fires for a bodyless 502 exactly as it does for a real page — the whole reason this
    // pane stopped trusting it. Mutant that puts `frameLoaded` back into `revealed` goes red here:
    // the card would flip to opacity-100 on this `load` alone, with no beacon in sight.
    fireEvent.load(iframe)
    expect(card(container).className).toMatch(/opacity-0/)
    expect(container.textContent).toMatch(/opening your app/i)

    vouch(container)
    expect(card(container).className).toMatch(/opacity-100/)
    expect(container.textContent).not.toMatch(/opening your app/i)
  })
})

describe('the reload nonce is a turn-end edge, and nothing else', () => {
  it('an iterating true→false edge over a live preview re-requests the document exactly once', () => {
    const { container, rerender } = render(
      <LivePreview previewUrl={SANDBOX_URL} status="ready" iterating />,
    )
    const before = container.querySelector('iframe')
    vouch(container)
    expect(card(container).className).toMatch(/opacity-100/)

    // The edge: a turn that was running OVER a live preview just ended.
    rerender(<LivePreview previewUrl={SANDBOX_URL} status="ready" iterating={false} />)
    const after = container.querySelector('iframe')
    expect(after).toBeTruthy()
    expect(after).not.toBe(before) // remounted — the frame key changed
    expect(card(container).className).toMatch(/opacity-0/) // and re-gated for the new frame

    // EXACTLY ONCE: another render at the same (false) value must not remount again.
    vouch(container)
    expect(card(container).className).toMatch(/opacity-100/)
    rerender(<LivePreview previewUrl={SANDBOX_URL} status="ready" iterating={false} />)
    expect(container.querySelector('iframe')).toBe(after)
    expect(card(container).className).toMatch(/opacity-100/)
  })

  it('it is the EDGE, not the level — a turn STARTING over a live preview remounts nothing', () => {
    // The distinction this pins is the one `AppPaneHost` is written around: the nonce means "a turn
    // just ENDED over a live preview, so the served bundle may be stale". A rising `iterating` is a
    // turn beginning, which is exactly when a remount would throw away the frame the citizen is
    // watching work happen in.
    const { container, rerender } = render(
      <LivePreview previewUrl={SANDBOX_URL} status="ready" iterating={false} />,
    )
    const onMount = container.querySelector('iframe')
    vouch(container)
    expect(card(container).className).toMatch(/opacity-100/)

    rerender(<LivePreview previewUrl={SANDBOX_URL} status="ready" iterating />)
    expect(container.querySelector('iframe')).toBe(onMount) // the turn starting changes nothing
    expect(card(container).className).toMatch(/opacity-100/)

    rerender(<LivePreview previewUrl={SANDBOX_URL} status="ready" iterating={false} />)
    expect(container.querySelector('iframe')).not.toBe(onMount) // …and its ENDING re-requests once
  })
})

describe('the ordinary states the pane still has to render', () => {
  it('running: a live URL frames the app behind its labelled wait', () => {
    const { container } = render(<LivePreview previewUrl={SANDBOX_URL} status="ready" />)
    expect(container.querySelector('iframe')).toBeTruthy()
    expect(container.textContent).toMatch(/opening your app/i)
    // The ended card is not merely withheld here — it no longer exists anywhere in the pane.
    expect(screen.queryByTestId('preview-ended-card')).toBeNull()
  })

  it('nothing to frame yet: provisioning with no URL is the labelled build wait, not a blank pane', () => {
    const { container } = render(<LivePreview previewUrl={null} status="provisioning" />)
    expect(container.querySelector('iframe')).toBeNull()
    expect(container.textContent).toMatch(/setting up your sandbox/i)
  })

  it('a new URL mid-session re-gates the reveal on the new frame’s own beacon', () => {
    const { container, rerender } = render(<LivePreview previewUrl={SANDBOX_URL} status="ready" />)
    vouch(container)
    expect(card(container).className).toMatch(/opacity-100/)
    // The old key's vouch must not carry over to the new one — mutant that keys `vouchedKey` on
    // the bare URL rather than the full frame key goes red here, staying revealed across the swap.
    rerender(<LivePreview previewUrl={SANDBOX_URL_2} status="ready" />)
    expect(card(container).className).toMatch(/opacity-0/)
  })
})
