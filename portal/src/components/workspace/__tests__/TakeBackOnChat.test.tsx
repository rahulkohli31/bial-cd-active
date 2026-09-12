/**
 * THE TAKE-BACK ON `/chat/{id}` — and the one guarantee the owner named out loud.
 *
 * WHY THIS SUITE IS ON THE CHAT SURFACE AND NOT ON THE PANE.
 *
 * Everything else about the take-back is provable against `AppPane` with a channel primed by hand.
 * Two things are not, and both are about a slot this surface owns:
 *
 *  1. A TAKE-BACK STARTS NO TURN. `ConversationSurface`'s `captureReclaim` is a SINGLE SLOT whose
 *     `resolveReclaim` awaits `retry()` — `fireRelayTurn(rawText, …)` for a refused send, or
 *     `handleBuildIt`. Route the take-back through it and confirming the hand-over SENDS the
 *     message the citizen is still holding in the composer, as a build instruction, which is the
 *     exact outcome this guards against. `startTurn` is the observable, and its call count is zero.
 *  2. FIRST REFUSAL WINS is that slot's rule, so a refused send already holding it would have
 *     swallowed the take-back's own refusal and resolved with the SEND's retry. The take-back owns
 *     its own dialog and its own retry closure, so neither one can reach the other.
 *
 * A pane-level suite cannot see either: it has no composer, no send and no reclaim slot.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor, cleanup, fireEvent, act } from '@testing-library/react'
import type { PreviewLifeState, PreviewState } from '../../../utils/buildSessionApi'
import { ApiError } from '../../../utils/apiError'

const h = vi.hoisted(() => ({
  loadBuilds: vi.fn(), newBuild: vi.fn(), createBuild: vi.fn(), getBuild: vi.fn(),
  deleteBuild: vi.fn(), listProjectConversations: vi.fn(), buildUserParts: vi.fn(),
  startTurn: vi.fn(), readTurnStream: vi.fn(), buildFromPlan: vi.fn(), resolvePlanOptions: vi.fn(),
  stop: vi.fn(), getStatus: vi.fn(),
  relaunchPreview: vi.fn(), fetchPreviewState: vi.fn(), fetchSaveState: vi.fn(),
  handOverWorkspace: vi.fn(),
}))

vi.mock('../../../utils/builderHistory', () => ({
  loadBuilds: h.loadBuilds, newBuild: h.newBuild, createBuild: h.createBuild,
  getBuild: h.getBuild, deleteBuild: h.deleteBuild, deriveTitle: (t: string) => (t || '').slice(0, 40),
}))
vi.mock('../../../utils/conversationApi', () => ({
  // The send path creates the chat before its first upload; stubbed so no network is reached.
  createConversation: async () => ({ id: 'conv-created' }),
  listProjectConversations: h.listProjectConversations,
}))
vi.mock('../../layout/Navbar', () => ({ default: () => null }))
vi.mock('../../../utils/attachmentStore', async (orig) => ({
  ...(await orig<typeof import('../../../utils/attachmentStore')>()),
  buildUserParts: h.buildUserParts,
}))
vi.mock('../../../utils/turnStreamApi', async (orig) => ({
  ...(await orig<typeof import('../../../utils/turnStreamApi')>()),
  startTurn: (...a: unknown[]) => h.startTurn(...a),
  readTurnStream: (...a: unknown[]) => h.readTurnStream(...a),
  buildFromPlan: (...a: unknown[]) => h.buildFromPlan(...a),
  resolvePlanOptions: (...a: unknown[]) => h.resolvePlanOptions(...a),
}))
vi.mock('../../../utils/buildSessionApi', async (orig) => ({
  ...(await orig<typeof import('../../../utils/buildSessionApi')>()),
  fetchPreviewState: (...a: unknown[]) => h.fetchPreviewState(...a),
  fetchSaveState: (...a: unknown[]) => h.fetchSaveState(...a),
  relaunchPreview: (...a: unknown[]) => h.relaunchPreview(...a),
  handOverWorkspace: (...a: unknown[]) => h.handOverWorkspace(...a),
}))

const { renderBuilder, makeClient, primeClient, composer, waitForGateOpen, FakeEventSource } =
  await import('../../../pages/__tests__/_builderSession.jsx')

function deps() {
  const fake = new FakeEventSource('x')
  return { client: makeClient(h), eventSourceFactory: () => fake }
}

const HELD: PreviewState = {
  state: 'slot_taken' as PreviewLifeState,
  alive: false,
  previewUrl: null,
  occupyingProjectName: 'Car pool',
  occupyingProjectId: 'pA',
  restorable: true,
}

/** The refusal `POST /relaunch` raises when another project holds the one workspace. */
const heldBy = (over: Record<string, unknown> = {}) =>
  new ApiError('“Car pool” is still open.', 409, 'sandbox_reclaim_blocked', {
    projectId: 'pA', projectName: 'Car pool', dirty: true, building: false, ...over,
  })

const takeBackButton = () => screen.getByRole('button', { name: /^Stop “Car pool” and open this app instead$/ })

beforeEach(() => {
  vi.clearAllMocks()
  Element.prototype.scrollIntoView = vi.fn()
  primeClient(h)
  h.newBuild.mockReturnValue('build-Y')
  h.createBuild.mockResolvedValue({ ok: true })
  h.getBuild.mockResolvedValue(null)
  h.loadBuilds.mockResolvedValue([])
  h.listProjectConversations.mockResolvedValue([
    { id: 'build-X', kind: 'build', title: 'My build', updatedAt: new Date().toISOString() },
  ])
  h.buildUserParts.mockImplementation(async (text: string) => [{ type: 'text', text }])
  h.startTurn.mockResolvedValue({ turnId: 't1' })
  h.fetchSaveState.mockResolvedValue({ appId: 'a1', dirty: false, savedHead: null, containerHead: null, recoveryAt: null })
  h.fetchPreviewState.mockResolvedValue(HELD)
  h.handOverWorkspace.mockResolvedValue(undefined)
})
afterEach(() => cleanup())

/** Mount the chat with the workspace held elsewhere, and wait for the pane to say so. */
async function blockedChat() {
  renderBuilder({ deps: deps() })
  await screen.findByRole('button', { name: /^Open “Car pool”$/ })
}

describe('★ the take-back never starts a turn', () => {
  for (const [name, button] of [
    ['saving first', /^Save “Car pool” and stop it$/],
    ['without saving', /^Stop “Car pool” without saving$/],
  ] as const) {
    it(`brings this app up, ${name}, and posts no turn`, async () => {
      // The probe refusal names the holder; the relaunch after the hand-over succeeds.
      h.relaunchPreview.mockRejectedValueOnce(heldBy()).mockResolvedValue({
        appId: 'a1', previewUrl: 'https://app/', status: 'ready', restoredFromFailedBuild: false, ready: true,
      })
      await blockedChat()

      fireEvent.click(takeBackButton())
      fireEvent.click(await screen.findByRole('button', { name: button }))

      await waitFor(() => expect(h.handOverWorkspace).toHaveBeenCalledWith('pA', name === 'saving first', {}, expect.any(Function)))
      // LIVENESS, paired with the absence below: the app really did come up, so a zero turn count
      // is a take-back that worked without a turn rather than a press that did nothing at all.
      await waitFor(() => expect(h.relaunchPreview).toHaveBeenCalledTimes(2))
      // ★ THE GUARANTEE. Mutate the handler to reuse the surface's send retry and this goes red.
      expect(h.startTurn).not.toHaveBeenCalled()
    })
  }

  it('★ starts no turn even with a refused send already holding the reclaim slot', async () => {
    // FIRST REFUSAL WINS is `captureReclaim`'s rule. If the take-back went through that slot its
    // own refusal would be discarded, and confirming would resolve the SEND — posting the message
    // still sitting in the composer as a build instruction.
    h.startTurn.mockRejectedValue(heldBy())
    await blockedChat()
    await waitForGateOpen()
    fireEvent.change(composer(), { target: { value: 'add a filter row' } })
    fireEvent.keyDown(composer(), { key: 'Enter' })
    await waitFor(() => expect(h.startTurn).toHaveBeenCalledTimes(1))

    h.relaunchPreview.mockRejectedValueOnce(heldBy()).mockResolvedValue({
      appId: 'a1', previewUrl: 'https://app/', status: 'ready', restoredFromFailedBuild: false, ready: true,
    })
    fireEvent.click(takeBackButton())
    // Wait for the take-back's ask to have landed and been dealt with — WHICHEVER WAY the code
    // chose to deal with it. Deliberately not "wait for two dialogs": that waypoint would fail
    // first under the mutant this scenario exists to catch, and the assertion that matters would
    // never run. Press the LAST question on screen, which is the take-back's when it has one (the
    // shell's `ReclaimSlot` renders above the pane) and the send's when it does not.
    await waitFor(() => expect(h.relaunchPreview).toHaveBeenCalledTimes(1))
    await act(async () => { await Promise.resolve() })
    const stop = screen.getAllByRole('button', { name: /^Stop “Car pool” without saving$/ })
    fireEvent.click(stop[stop.length - 1])
    await waitFor(() => expect(h.handOverWorkspace).toHaveBeenCalled())

    // ★ THE GUARANTEE. Route this through the surface's slot and the confirm resolves the SEND —
    // `fireRelayTurn` — so this reads 2.
    expect(h.startTurn).toHaveBeenCalledTimes(1)
    // LIVENESS, so a zero-turn count is a take-back that worked rather than a press that did not.
    await waitFor(() => expect(h.relaunchPreview).toHaveBeenCalledTimes(2))
    // And the message is still theirs to send.
    expect((composer() as HTMLTextAreaElement).value).toBe('add a filter row')
  })
})

describe('and the send`s own dialog is untouched by it', () => {
  it('★ the take-back does not steal, or feed, the surface`s reclaim slot', async () => {
    h.startTurn.mockRejectedValue(heldBy())
    h.relaunchPreview.mockRejectedValueOnce(heldBy({ projectName: 'Car pool' })).mockResolvedValue({
      appId: 'a1', previewUrl: 'https://app/', status: 'ready', restoredFromFailedBuild: false, ready: true,
    })
    await blockedChat()

    // The take-back first: it opens ITS dialog and leaves the surface's slot empty.
    fireEvent.click(takeBackButton())
    await screen.findByRole('dialog')
    await waitForGateOpen()
    fireEvent.change(composer(), { target: { value: 'add a filter row' } })
    fireEvent.keyDown(composer(), { key: 'Enter' })
    await waitFor(() => expect(h.startTurn).toHaveBeenCalledTimes(1))

    // Two dialogs are now up — the take-back's and the send's — and cancelling the take-back's
    // must not cancel the other. Liveness: the send's question survives.
    await act(async () => { await Promise.resolve() })
    expect(screen.getAllByRole('dialog').length).toBe(2)
  })
})
