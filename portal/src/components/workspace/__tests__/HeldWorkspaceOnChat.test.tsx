/**
 * A HELD WORKSPACE ON `/chat/{id}` — and the two things only this surface can prove.
 *
 * WHY THIS SUITE IS ON THE CHAT SURFACE AND NOT ON THE PANE.
 *
 * What the pane says about a taken slot is provable against `AppPane` with a channel primed by
 * hand. Two things are not, and both are about the composer this surface owns:
 *
 *  1. TAKING THE WORKSPACE BACK STARTS NO TURN. The way back is the start control, and a citizen
 *     pressing it is asking for their app, never for the message still sitting in their composer
 *     to be sent as a build instruction. `startTurn` is the observable, and its call count is zero.
 *  2. A REFUSED SEND PUTS NO QUESTION ON SCREEN. The server hands the one workspace to whichever
 *     project was asked for, so a send has nothing to arbitrate — a refusal is read, not answered,
 *     and the citizen's text stays where they typed it.
 *
 * A pane-level suite cannot see either: it has no composer and no send.
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
}))

const { renderBuilder, makeClient, primeClient, composer, waitForGateOpen, FakeEventSource } =
  await import('../../../pages/__tests__/_builderSession.jsx')

function deps() {
  const fake = new FakeEventSource('x')
  return { client: makeClient(h), eventSourceFactory: () => fake }
}

/** The wire still names the holder; what is pinned here is that no screen repeats it. */
const HELD: PreviewState = {
  state: 'slot_taken' as PreviewLifeState,
  alive: false,
  previewUrl: null,
  occupyingProjectName: 'Car pool',
  occupyingProjectId: 'pA',
  restorable: true,
}

/** The one refusal `POST /relaunch` can still raise: a colleague's shared view in the slot. */
const sharedViewHolds = (over: Record<string, unknown> = {}) =>
  new ApiError('“Car pool” is open for a colleague right now.', 409, 'sandbox_reclaim_blocked', {
    projectId: 'pA', projectName: 'Car pool', dirty: true, building: false, isSharedView: true, ...over,
  })

const STARTED = {
  appId: 'a1', previewUrl: 'https://app/', status: 'ready', restoredFromFailedBuild: false, ready: true,
}

const launch = () => screen.getByRole('button', { name: /^Launch Application$/ })

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
})
afterEach(() => cleanup())

/** Mount the chat with the workspace held elsewhere, and wait for the pane to offer the way back. */
async function blockedChat() {
  renderBuilder({ deps: deps() })
  await screen.findByRole('button', { name: /^Launch Application$/ })
}

describe('★ a taken slot asks the citizen nothing, on the chat surface too', () => {
  it('★ offers the ordinary start, names no other project, and opens no dialog', async () => {
    await blockedChat()

    expect(screen.getByTestId('app-pane-empty').textContent).not.toContain('Car pool')
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('★ pressing it brings this app up, and posts no turn', async () => {
    h.relaunchPreview.mockResolvedValue(STARTED)
    await blockedChat()
    await waitForGateOpen()
    fireEvent.change(composer(), { target: { value: 'add a filter row' } })

    fireEvent.click(launch())

    // LIVENESS, paired with the absence below: the app really did come up, so a zero turn count is
    // a start that worked without a turn rather than a press that did nothing at all.
    await waitFor(() => expect(h.relaunchPreview).toHaveBeenCalledWith({ projectId: 'p1' }))
    // ★ THE GUARANTEE. Route the start through the send's retry and this reads 1.
    expect(h.startTurn).not.toHaveBeenCalled()
    // And the message is still theirs to send.
    expect((composer() as HTMLTextAreaElement).value).toBe('add a filter row')
  })

  it('★ a start refused for a colleague`s shared view is said, not asked', async () => {
    // The one refusal the server can still raise, and pressing again cannot move it — so the
    // server's own sentence is the whole answer and nothing appears to be decided.
    h.relaunchPreview.mockRejectedValue(sharedViewHolds())
    await blockedChat()

    fireEvent.click(launch())

    const note = await screen.findByTestId('app-pane-note')
    expect(note.textContent).toBe('“Car pool” is open for a colleague right now.')
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('★ and a refused SEND opens no question anywhere — the text stays in the composer', async () => {
    // The regression this file is the last guard for: this refusal used to open the hand-over
    // dialog, whose confirm resolved the send — posting the held message as a build instruction.
    h.startTurn.mockRejectedValue(sharedViewHolds())
    await blockedChat()
    await waitForGateOpen()
    fireEvent.change(composer(), { target: { value: 'add a filter row' } })
    fireEvent.keyDown(composer(), { key: 'Enter' })

    await waitFor(() => expect(h.startTurn).toHaveBeenCalledTimes(1))
    await act(async () => { await Promise.resolve() })

    expect(screen.queryByRole('dialog')).toBeNull()
    expect((composer() as HTMLTextAreaElement).value).toBe('add a filter row')
    // ONE ATTEMPT, NOT TWO: nothing retried the send behind the citizen's back.
    expect(h.startTurn).toHaveBeenCalledTimes(1)
  })
})
