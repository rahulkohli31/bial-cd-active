/**
 * THE "THIS CHAT IS GETTING LONG" LINE, AND WHERE ITS NUMBER COMES FROM.
 *
 * WHY THIS FILE EXISTS
 *
 * The browser used to compute this number itself — four characters to the token, a flat nominal
 * per attachment — so the meter a citizen watched and the wall the server enforces were two
 * readings of one GUESS, wrong by 47x on a document. Both estimators are deleted. What is left
 * is plumbing, and plumbing is exactly what a unit test of `contextState` cannot see: that
 * function is already covered in `utils/__tests__/contextLimits.test.ts` and would stay green if
 * this surface handed it `null` for ever, which is precisely the state this unit found it in.
 *
 * So what is asserted here is the WIRING, in both directions:
 *   1. the cold read's figure reaches the line (a reopened chat warns on first paint);
 *   2. the send's own 202 replaces it (the meter tracks the chat as it grows);
 *   3. an unmeasured chat stays silent — `null` is not zero and is not "assume full";
 *   4. NOTHING is asked to size anything before a send.
 *
 * The fourth is the one this repo has settled and must keep settled: there is no pre-send token
 * counting, at any layer. It is asserted on the transport log rather than by inspection, because
 * "no request" is the kind of claim that quietly stops being true.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor, cleanup, fireEvent } from '@testing-library/react'

const h = vi.hoisted(() => ({
  loadBuilds: vi.fn(), getBuild: vi.fn(),
  listProjectConversations: vi.fn(), buildUserParts: vi.fn(),
  startTurn: vi.fn(), readTurnStream: vi.fn(), buildFromPlan: vi.fn(), stopTurn: vi.fn(),
  resolvePlanOptions: vi.fn(),
  stop: vi.fn(), getStatus: vi.fn(), relaunchPreview: vi.fn(),
  fetchSaveState: vi.fn(), fetchPreviewState: vi.fn(), saveProject: vi.fn(),
  getStoredUser: vi.fn(),
}))

vi.mock('../../utils/builderHistory', () => ({
  loadBuilds: h.loadBuilds, getBuild: h.getBuild, deriveTitle: (t: string) => (t || '').slice(0, 40),
}))
vi.mock('../../utils/conversationApi', async (orig) => ({
  ...(await orig<typeof import('../../utils/conversationApi')>()),
  // The send path creates the chat before its first upload; stubbed so no network is reached.
  createConversation: async () => ({ id: 'conv-created' }),
  listProjectConversations: h.listProjectConversations,
}))
vi.mock('../../components/layout/Navbar', () => ({ default: () => null }))
vi.mock('../../utils/attachmentStore', async (orig) => ({
  ...(await orig<typeof import('../../utils/attachmentStore')>()),
  buildUserParts: h.buildUserParts,
}))
vi.mock('../../utils/turnStreamApi', async (orig) => ({
  ...(await orig<typeof import('../../utils/turnStreamApi')>()),
  startTurn: (...a: unknown[]) => h.startTurn(...a),
  readTurnStream: (...a: unknown[]) => h.readTurnStream(...a),
  buildFromPlan: (...a: unknown[]) => h.buildFromPlan(...a),
  stopTurn: (...a: unknown[]) => h.stopTurn(...a),
  resolvePlanOptions: (...a: unknown[]) => h.resolvePlanOptions(...a),
}))
vi.mock('../../utils/buildSessionApi', async (orig) => ({
  ...(await orig<typeof import('../../utils/buildSessionApi')>()),
  fetchSaveState: (...a: unknown[]) => h.fetchSaveState(...a),
  fetchPreviewState: (...a: unknown[]) => h.fetchPreviewState(...a),
  relaunchPreview: (...a: unknown[]) => h.relaunchPreview(...a),
  saveProject: (...a: unknown[]) => h.saveProject(...a),
}))
// The administrator's thresholds reach the browser on the signed-in profile. Pinned here so the
// numbers below are read against a KNOWN ceiling rather than whatever default happens to ship.
vi.mock('../../utils/auth', async (orig) => ({
  ...(await orig<typeof import('../../utils/auth')>()),
  getStoredUser: (...a: unknown[]) => h.getStoredUser(...a),
}))

import {
  FakeEventSource, makeClient, primeClient, primeTurn, renderBuilder, send, waitForGateOpen,
  composer,
} from './_builderSession.jsx'

/** The administrator's numbers, as the profile carries them. */
const SOFT = 375_000
const HARD = 500_000

const WARNING =
  'This chat is getting long. Start a new chat soon to keep things quick — your app and everything you have built stays exactly as it is.'

const deps = () => {
  const fake = new FakeEventSource('x')
  return { fake, deps: { client: makeClient(h), eventSourceFactory: () => fake } }
}

/** A conversation as the cold read hands it back, at a stated occupancy. */
const savedChat = (contextTokens: number | null) => ({
  id: 'chat-A',
  kind: 'build',
  messages: [],
  activeTurn: null,
  contextTokens,
})

beforeEach(() => {
  vi.clearAllMocks()
  sessionStorage.clear()
  primeClient(h)
  primeTurn(h)
  h.getStoredUser.mockReturnValue({
    limits: { contextSoftLimit: SOFT, contextHardLimit: HARD },
  })
  h.getBuild.mockResolvedValue(savedChat(null))
  h.loadBuilds.mockResolvedValue([])
  h.listProjectConversations.mockResolvedValue([])
  h.buildUserParts.mockImplementation(async (t: string) => [{ type: 'text', text: t }])
  h.fetchSaveState.mockResolvedValue({
    appId: null, dirty: null, containerHead: null, savedHead: null, recoveryAt: null,
  })
  h.fetchPreviewState.mockResolvedValue({
    state: 'unknown', alive: false, previewUrl: null, occupyingProjectName: null, restorable: null,
  })
})

afterEach(() => {
  cleanup()
  sessionStorage.clear()
})

const warning = () => screen.queryByTestId('composer-context-warning')

describe('the meter reads the server’s figure', () => {
  it('★ warns on first paint for a chat the server would already be about to refuse', async () => {
    // The figure is the administrator's own soft threshold — the exact token at which the server's
    // `effective_context` says this chat is getting long, handed over by the read rather than
    // recomputed here. One number, two readers.
    h.getBuild.mockResolvedValue(savedChat(SOFT))
    const { deps: d } = deps()
    renderBuilder({ deps: d })

    await waitFor(() => expect(warning()).toBeTruthy())
    expect(warning()?.textContent).toBe(WARNING)
  })

  it('is silent one token below that threshold — the boundary is the server’s, not a mood', async () => {
    h.getBuild.mockResolvedValue(savedChat(SOFT - 1))
    const { deps: d } = deps()
    renderBuilder({ deps: d })

    await waitForGateOpen()
    expect(warning()).toBeNull()
    // LIVENESS: the composer really rendered, so the absence above is an absence rather than a
    // surface that threw before it drew anything.
    expect(composer()).toBeTruthy()
  })

  it('★ is silent for a chat nobody has measured — null is not zero and is not "assume full"', async () => {
    // EDGE CASE. A brand-new chat, or one where only the platform has spoken, carries no
    // measurement. The honest answer is to say nothing: a browser that guessed here is exactly
    // the kind of estimate this surface no longer makes.
    h.getBuild.mockResolvedValue(savedChat(null))
    const { deps: d } = deps()
    renderBuilder({ deps: d })

    await waitForGateOpen()
    expect(warning()).toBeNull()
    expect(composer()).toBeTruthy()
  })

  it('★ takes the send’s own 202 figure — the meter tracks the chat as it grows', async () => {
    // The cold read said this chat was well inside the ceiling. The turn the citizen just sent
    // was admitted at a figure past the threshold, and that is the number the NEXT send would be
    // judged on — so the line appears now rather than after a reload.
    h.getBuild.mockResolvedValue(savedChat(1_000))
    h.startTurn.mockResolvedValue({ turnId: 't1', contextTokens: SOFT + 25_000 })
    const { deps: d } = deps()
    renderBuilder({ deps: d })

    await waitForGateOpen()
    expect(warning()).toBeNull() // liveness: it really was silent before the send

    await send('one more change')

    await waitFor(() => expect(warning()).toBeTruthy())
  })

  it('does not turn an older server’s missing figure into a zero', async () => {
    // A 202 without the field means "no measurement", the same as `null`. Read as `0` it would
    // be the browser asserting the chat is empty — and it would wipe out a real figure the cold
    // read had already delivered.
    h.getBuild.mockResolvedValue(savedChat(SOFT + 10_000))
    h.startTurn.mockResolvedValue({ turnId: 't1' })
    const { deps: d } = deps()
    renderBuilder({ deps: d })

    await waitFor(() => expect(warning()).toBeTruthy())
    await send('one more change')

    // Silent now, because the server said nothing — not because it said zero. Either way the
    // browser must not have invented a number.
    await waitFor(() => expect(h.startTurn).toHaveBeenCalledTimes(1))
    expect(warning()).toBeNull()
  })
})

describe('nothing is sized before a send', () => {
  it('★ typing asks the server nothing, at any length', async () => {
    // THE EDGE CASE — asserted on the transport log. This platform has SETTLED that
    // there is no pre-send token counting: the only number that exists is the one the provider
    // reported for a turn it already served. A "how big is this?" round trip on the keystroke
    // path is the thing that must never be added back, and this is what would notice.
    h.getBuild.mockResolvedValue(savedChat(1_000))
    const { deps: d } = deps()
    renderBuilder({ deps: d })
    await waitForGateOpen()

    const callsAfterLoad = [h.getBuild, h.startTurn, h.buildFromPlan, h.readTurnStream].map(
      (fn) => fn.mock.calls.length,
    )

    fireEvent.change(composer(), { target: { value: 'x'.repeat(9_000) } })
    fireEvent.change(composer(), { target: { value: 'x'.repeat(9_500) } })

    expect(
      [h.getBuild, h.startTurn, h.buildFromPlan, h.readTurnStream].map(
        (fn) => fn.mock.calls.length,
      ),
    ).toEqual(callsAfterLoad)
    expect(h.startTurn).not.toHaveBeenCalled()

    // …and the send itself is ONE request, which is where the figure comes from. Without this
    // the assertion above would also pass on a surface that had stopped sending altogether.
    await send('go')
    await waitFor(() => expect(h.startTurn).toHaveBeenCalledTimes(1))
  })
})
