/**
 * THE RELAUNCH CHAIN IS INERT — the characterization this unit was allowed to delete against.
 *
 * `RelaunchAffordance` and its four render sites went in Plan F, U4, and `LivePreview` was left
 * ACCEPTING `onRelaunch` and never reading it. Everything above that unread prop — the surface's
 * `handleRelaunch`, the session hook's `relaunch()`, the two `409` arms that set `blocked`, and the
 * block banner with its Force-end button — was therefore reachable-looking code hanging off a
 * callback nobody consumes.
 *
 * WRITTEN BEFORE THE DELETION, AND IT STAYS GREEN AFTER IT. That is the whole contract of this
 * file: every assertion below holds identically on both sides of the commit, so a red here means
 * the deletion changed behaviour rather than removing dead weight.
 *
 * THE BLOCK BANNER HAD **TWO** PRODUCERS, and a test driven from only one proves nothing about the
 * half the same commit also deletes:
 *
 *   1. `start`'s 409    — `useBuildSession.start()` mapped `build_session_already_active` onto
 *                         `blocked`. Nothing calls `session.start()`; a composer send is a TURN.
 *   2. `relaunch`'s 409 — `useBuildSession.relaunch()` surfaced the SAME banner, by its own
 *                         comment ("relaunch never pre-empts a running build"). Nothing calls
 *                         `session.relaunch()` either: its one caller is wired to the unread prop.
 *
 * Both are driven here. The second matters most, because `relaunchPreview` IS still called in
 * production — `StartAppControl` and `RailComposer` reach the module function directly, bypassing
 * the hook — so its 409 is a LIVE path, and what this pins is that the live path answers with the
 * workspace's own sentence rather than with the banner.
 *
 * EVERY ABSENCE IS PAIRED WITH A LIVENESS ASSERTION. "No banner" is also true of a surface that
 * threw on render, which is exactly how an assert-absence test false-greens.
 *
 * The frame scenarios are the dangerous half. `relaunching` was never a banner flag — it fed
 * `showRestoring`, `showTerminal`, `frameContext`, `framePending` and the frame's own reload
 * identity, i.e. four booleans that decide whether the iframe stays MOUNTED. Unmounting it kills a
 * container the server is still serving (`AppPaneHost.tsx` describes that failure at length), so
 * the framed cases below pin those derived values through their DOM consequences, on both sides of
 * the change.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, cleanup, fireEvent } from '@testing-library/react'
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
  start: vi.fn(), stop: vi.fn(), getStatus: vi.fn(), forceEnd: vi.fn(), relaunchPreview: vi.fn(),
  fetchSaveState: vi.fn(), fetchPreviewState: vi.fn(),
}))

vi.mock('../../utils/builderHistory', () => ({
  loadBuilds: h.loadBuilds, getBuild: h.getBuild, deriveTitle: (t) => (t || '').slice(0, 40),
}))
vi.mock('../../utils/conversationApi', async (orig) => ({
  ...(await orig()),
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
const CHAT_ID = 'build-X'

/**
 * The injected C3 client, assembled HERE rather than through `makeClient`, so this file decides
 * which members exist — `makeClient` no longer carries `start` at all, and this scenario needs to
 * hand one in to prove nothing reaches it. An extra member on the bag is inert: the hook only ever
 * calls what it names.
 */
const client = () => ({
  start: h.start, relaunchPreview: h.relaunchPreview, stop: h.stop, getStatus: h.getStatus, forceEnd: h.forceEnd,
})
const deps = () => ({ client: client(), eventSourceFactory: () => new FakeEventSource('x') })

/** The device card that carries the reveal's opacity — the handle every frame assertion uses. */
const card = (container) => container.querySelector('[data-testid="device-card"]')

/** The banner and the control this unit deletes, found by their rendered copy, not by a testid. */
const blockBanner = () => screen.queryByText(/you already have a build running/i)
const forceEndControl = () => screen.queryByRole('button', { name: /force-end/i })

beforeEach(() => {
  vi.clearAllMocks()
  sessionStorage.clear()
  primeTurn(h)
  h.stop.mockResolvedValue(ENDED_RESP)
  h.getStatus.mockResolvedValue(statusResp())
  h.forceEnd.mockResolvedValue(ENDED_RESP)
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
  it('arm 1, start’s 409: a send is a TURN, so the C3 start that raised `blocked` never fires', async () => {
    // The 409 is armed on the start the injected client exposes. If any path on this surface still
    // provisioned a C3 session, this would raise the banner — which is exactly the point: none does.
    h.start.mockRejectedValue(new BuildSessionAlreadyActiveError('You already have a build running.', 'sess-9'))
    h.readTurnStream.mockImplementation(turnStreaming(planReply()))

    renderBuilder({ deps: deps() })
    await send('build me a visitor pass tracker')
    await screen.findByRole('button', { name: /^Build this plan$/ })

    // LIVENESS FIRST — a surface that threw on render would also have no banner.
    expect(screen.getByTestId('composer-input')).toBeTruthy()
    expect(h.start).not.toHaveBeenCalled() // the reason the arm is unreachable, stated
    expect(blockBanner()).toBeNull()
    expect(forceEndControl()).toBeNull()
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
    // …never the banner, and never its kill switch.
    expect(blockBanner()).toBeNull()
    expect(forceEndControl()).toBeNull()

    standby.settle()
  })
})

describe('frame survival — the case where an unmount kills a live container', () => {
  // A framed, pardoned preview: `status: 'ended'` + `completedLive` is the #13/R2 state in which
  // the server is STILL SERVING the container under an idle lease. `showTerminal` must stay false,
  // `frameContext` true and `framePending` true, or the iframe comes down over a live app.
  const framedAndPardoned = (props = {}) =>
    render(<LivePreview previewUrl={SANDBOX_URL} status="ended" completedLive {...props} />)

  it('an ended-but-live preview keeps its frame mounted, shows no terminal card, and keeps the labelled wait', () => {
    const { container } = framedAndPardoned()

    // frameContext → the iframe exists at all.
    const iframe = container.querySelector('iframe')
    expect(iframe).toBeTruthy()
    expect(iframe.getAttribute('src')).toBe(SANDBOX_URL)
    // showTerminal → false: no ended card over a container that is still serving.
    expect(screen.queryByTestId('preview-ended-card')).toBeNull()
    expect(container.textContent).not.toMatch(/no longer running/i)
    // framePending → true: mounted but not yet loaded, so the wait is up and LABELLED.
    expect(card(container).className).toMatch(/opacity-0/)
    expect(container.textContent).toMatch(/starting your app/i)
  })

  it('…and the framed document’s own load still resolves that wait', () => {
    const { container } = framedAndPardoned()
    fireEvent.load(container.querySelector('iframe'))
    expect(card(container).className).toMatch(/opacity-100/)
    expect(container.textContent).not.toMatch(/starting your app/i)
  })
})

describe('the reload nonce is a turn-end edge, and nothing else', () => {
  it('an iterating true→false edge over a live preview re-requests the document exactly once', () => {
    const { container, rerender } = render(
      <LivePreview previewUrl={SANDBOX_URL} status="ready" iterating />,
    )
    const before = container.querySelector('iframe')
    fireEvent.load(before)
    expect(card(container).className).toMatch(/opacity-100/)

    // The edge: a turn that was running OVER a live preview just ended.
    rerender(<LivePreview previewUrl={SANDBOX_URL} status="ready" iterating={false} />)
    const after = container.querySelector('iframe')
    expect(after).toBeTruthy()
    expect(after).not.toBe(before) // remounted — the frame key changed
    expect(card(container).className).toMatch(/opacity-0/) // and re-gated on the new load

    // EXACTLY ONCE: another render at the same (false) value must not remount again.
    fireEvent.load(after)
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
    fireEvent.load(onMount)
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
    expect(container.textContent).toMatch(/starting your app/i)
    expect(screen.queryByTestId('preview-ended-card')).toBeNull()
  })

  it('nothing to frame yet: provisioning with no URL is the labelled build wait, not a blank pane', () => {
    const { container } = render(<LivePreview previewUrl={null} status="provisioning" />)
    expect(container.querySelector('iframe')).toBeNull()
    expect(container.textContent).toMatch(/setting up your sandbox/i)
  })

  it('a new URL mid-session re-gates the reveal on the new frame’s own load', () => {
    const { container, rerender } = render(<LivePreview previewUrl={SANDBOX_URL} status="ready" />)
    fireEvent.load(container.querySelector('iframe'))
    expect(card(container).className).toMatch(/opacity-100/)
    rerender(<LivePreview previewUrl={SANDBOX_URL_2} status="ready" />)
    expect(card(container).className).toMatch(/opacity-0/)
  })
})
