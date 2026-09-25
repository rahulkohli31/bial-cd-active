/**
 * THE CHAT ROUTE HOLDS ITS CONTAINER OPEN TOO — and this suite exists because it is the surface
 * that gets forgotten.
 *
 * There are TWO preview polls in this product: the project workspace's, and this one. They read
 * their cadence from the same shared module precisely so they cannot drift, and the renewal is
 * shared the same way. A surface that frames an app WITHOUT renewing is a silent container-killer:
 * the citizen is looking straight at their app while the platform counts it as abandoned. Proving
 * it on `useWorkspaceState` alone would prove exactly the half that was never in doubt.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { waitFor, cleanup, act } from '@testing-library/react'
import type { PreviewLifeState, PreviewState } from '../../../utils/buildSessionApi'

const h = vi.hoisted(() => ({
  loadBuilds: vi.fn(), newBuild: vi.fn(), createBuild: vi.fn(), getBuild: vi.fn(),
  deleteBuild: vi.fn(), listProjectConversations: vi.fn(), buildUserParts: vi.fn(),
  startTurn: vi.fn(), readTurnStream: vi.fn(), buildFromPlan: vi.fn(), resolvePlanOptions: vi.fn(),
  relaunchPreview: vi.fn(), fetchPreviewState: vi.fn(), fetchSaveState: vi.fn(),
  fetchCompileState: vi.fn(), checkWorkspace: vi.fn(), renewPresence: vi.fn(),
}))

vi.mock('../../../utils/builderHistory', () => ({
  loadBuilds: h.loadBuilds, newBuild: h.newBuild, createBuild: h.createBuild,
  getBuild: h.getBuild, deleteBuild: h.deleteBuild, deriveTitle: (t: string) => (t || '').slice(0, 40),
}))
vi.mock('../../../utils/conversationApi', () => ({
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
  fetchCompileState: (...a: unknown[]) => h.fetchCompileState(...a),
  checkWorkspace: (...a: unknown[]) => h.checkWorkspace(...a),
  relaunchPreview: (...a: unknown[]) => h.relaunchPreview(...a),
  renewPresence: (...a: unknown[]) => h.renewPresence(...a),
}))

const { renderBuilder } =
  await import('../../../pages/__tests__/_builderSession.jsx')
const { HIDDEN_PROBE_MS, PREVIEW_PROBE_MS, STARTING_PROBE_MS } = await import('../../workspace/workspaceState')

const LIVE: PreviewState = {
  state: 'alive' as PreviewLifeState,
  alive: true,
  previewUrl: 'https://app.example/',
  restorable: true,
  startingSince: null,
  startFailure: null,
}

/**
 * Put the document out of sight, or bring it back, and fire the event the browser would.
 * `visibilityState` is a read-only getter, so it is redefined rather than assigned.
 *
 * JSDOM IS NOT A BROWSER, and this is the one place that matters most: it will keep firing a timer
 * Chrome throttles and Edge freezes. These tests prove the CODE asks correctly; only the real
 * browser check proves a backgrounded tab is allowed to.
 */
function hide(hidden: boolean): void {
  Object.defineProperty(document, 'visibilityState', {
    configurable: true,
    get: () => (hidden ? 'hidden' : 'visible'),
  })
  document.dispatchEvent(new Event('visibilitychange'))
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.useFakeTimers({ shouldAdvanceTime: true })
  Element.prototype.scrollIntoView = vi.fn()
  h.newBuild.mockReturnValue('build-Y')
  h.createBuild.mockResolvedValue({ ok: true })
  h.getBuild.mockResolvedValue(null)
  h.loadBuilds.mockResolvedValue([])
  h.listProjectConversations.mockResolvedValue([
    { id: 'build-X', kind: 'build', title: 'My build', updatedAt: new Date().toISOString() },
  ])
  h.buildUserParts.mockImplementation(async (text: string) => [{ type: 'text', text }])
  h.fetchSaveState.mockResolvedValue({ appId: 'a1', dirty: false, savedHead: null, containerHead: null })
  h.fetchCompileState.mockResolvedValue({ state: 'unknown' })
  h.checkWorkspace.mockResolvedValue(false)
  h.fetchPreviewState.mockResolvedValue(LIVE)
  h.renewPresence.mockResolvedValue('renewed')
  hide(false)
})
afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

const settle = async () => {
  await act(async () => {
    await Promise.resolve()
    await Promise.resolve()
  })
}

describe('the chat route holds its container open', () => {
  it('renews on the ordinary tick', async () => {
    // A citizen sitting on the chat route between turns has not left, and the ten minutes they
    // spend reading the last answer must not cost them their app.
    renderBuilder()
    await waitFor(() => expect(h.fetchPreviewState).toHaveBeenCalled())

    await waitFor(() => expect(h.renewPresence).toHaveBeenCalledWith(expect.any(String), 'visible'))
  })

  it('renews from a hidden tab, on the longer budget', async () => {
    hide(true)
    renderBuilder()

    await waitFor(() => expect(h.renewPresence).toHaveBeenCalledWith(expect.any(String), 'hidden'))
  })

  it('asks a hidden tab for nothing that could restart the dev server', async () => {
    // `checkWorkspace` is a POST whose server side can restart a stopped app's dev server.
    // Reaching it from a background tab would do that with nobody looking.
    hide(true)
    renderBuilder()
    await waitFor(() => expect(h.renewPresence).toHaveBeenCalled())
    await settle()

    expect(h.checkWorkspace).not.toHaveBeenCalled()
    expect(h.fetchCompileState).not.toHaveBeenCalled()
  })

  it('polls a hidden tab on the longer cadence', async () => {
    renderBuilder()
    await waitFor(() => expect(h.renewPresence).toHaveBeenCalled())
    hide(true)
    await settle()
    h.renewPresence.mockClear()

    await act(async () => {
      await vi.advanceTimersByTimeAsync(PREVIEW_PROBE_MS + 1)
    })
    expect(h.renewPresence).not.toHaveBeenCalled()

    await act(async () => {
      await vi.advanceTimersByTimeAsync(HIDDEN_PROBE_MS)
    })
    expect(h.renewPresence).toHaveBeenCalledWith(expect.any(String), 'hidden')
  })

  it('★ renews throughout a watched start, exactly as the project surface does', async () => {
    // THE SHARED DECISION, PINNED ON BOTH SIDES. `presenceToRenew` is one function precisely so a
    // container held open on one surface cannot be quietly abandoned on the other — and the window
    // it used to skip is the one where the marker and the lock lapse together.
    h.fetchPreviewState.mockResolvedValue({ ...LIVE, state: 'starting' as PreviewLifeState, alive: false, previewUrl: null })
    renderBuilder()
    await waitFor(() => expect(h.fetchPreviewState).toHaveBeenCalled())
    await settle()
    h.renewPresence.mockClear()

    for (let tick = 0; tick < 3; tick += 1) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(STARTING_PROBE_MS + 1)
      })
    }

    expect(h.renewPresence).toHaveBeenCalledTimes(3)
    expect(h.renewPresence).toHaveBeenCalledWith(expect.any(String), 'visible')
  })

  it('sends nothing on unmount — leaving is silence', async () => {
    const { unmount } = renderBuilder()
    await waitFor(() => expect(h.renewPresence).toHaveBeenCalled())
    h.renewPresence.mockClear()

    unmount()
    await settle()

    expect(h.renewPresence).not.toHaveBeenCalled()
  })
})
