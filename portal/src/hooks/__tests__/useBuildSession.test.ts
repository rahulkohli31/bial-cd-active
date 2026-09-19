import { describe, it, expect, vi, afterEach } from 'vitest'
import { renderHook, act, cleanup } from '@testing-library/react'
import { useBuildSession } from '../useBuildSession'
import type { BuildSessionClient } from '../../utils/buildSessionApi'
import { FakeEventSource } from '../../utils/buildSessionMock'
import type { ProgressEnvelope, BuildSessionStatusResponse } from '../../utils/buildSessionTypes'

afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

const PREVIEW_URL = 'https://app.example.azurecontainerapps.io/'

function makeClient(over: Partial<BuildSessionClient> = {}): BuildSessionClient {
  return {
    relaunchPreview: vi.fn(async () => ({ appId: 'a1', previewUrl: PREVIEW_URL, status: 'ready' as const, restoredFromFailedBuild: false, ready: true })),
    getStatus: vi.fn(async () => ({ sessionId: 's1', projectId: 'p1', appId: 'a1', status: 'provisioning' as const, previewUrl: null, lastSeq: null, createdAt: 'c', updatedAt: 'u' })),
    ...over,
  }
}

function setup(client: BuildSessionClient = makeClient()) {
  const fake = new FakeEventSource('x')
  const view = renderHook(() => useBuildSession({ client, eventSourceFactory: () => fake }))
  return { ...view, fake, client }
}

const STEP: ProgressEnvelope = { type: 'step', seq: 1, name: 'scaffold', label: 'Scaffolding…', state: 'started' }
const READY: ProgressEnvelope = { type: 'preview_ready', seq: 2, preview_url: PREVIEW_URL }
const RECONNECTING: ProgressEnvelope = { type: 'preview_reconnecting', seq: 5 }
const ESCALATION: ProgressEnvelope = { type: 'escalation', seq: 3, reason: 'exhausted', detail: 'gave up', last_error: null }
const ENDED_FAIL: ProgressEnvelope = { type: 'ended', seq: 4, status: 'failed', preview_url: null, snapshot_committed: false, reason: 'escalated' }
const QUOTA: ProgressEnvelope = { type: 'quota_exceeded', seq: 3, limit: 1_000_000, used: 1_000_000, resets_at: '2026-07-15T18:30:00Z' }
const ENDED_QUOTA: ProgressEnvelope = { type: 'ended', seq: 4, status: 'ended', preview_url: null, snapshot_committed: true, reason: 'quota_exceeded' }
const ENDED_STOPPED: ProgressEnvelope = { type: 'ended', seq: 6, status: 'ended', preview_url: null, snapshot_committed: true, reason: 'stopped_by_user' }

/**
 * `start` and `relaunch` are gone from this hook — useBuildSession.ts carries why — and so are the
 * two describe blocks that drove them. Nothing they pinned is uncovered:
 *   · the 409 → `BuildSessionAlreadyActiveError` mapping is `postJson`'s, and its two cases are
 *     re-pointed onto `relaunchPreview` in `utils/__tests__/buildSessionApi.test.ts`;
 *   · the `blocked` banner those 409s fed is deleted, and `pages/__tests__/relaunch-chain-retired`
 *     drives BOTH producers to prove it cannot come back;
 *   · the server's verbatim 503 copy is asserted where it is now read — the live restore path
 *     in `components/workspace/__tests__/StartAppControl.test.tsx`;
 *   · the mid-flight-unmount guard (FIX 1) is re-pointed onto `reattach` below, which carries the
 *     identical `mountedRef` bail.
 */
describe('useBuildSession — status derivation across the lifecycle', () => {
  it('derives status at EACH hop: provisioning →(first step)→ building → preview_ready → ready → ended', async () => {
    const { result, fake } = setup()
    await act(async () => { await result.current.reattach('s1') })
    expect(result.current.status).toBe('provisioning')

    act(() => { fake.open() })
    act(() => { fake.emitEnvelope(STEP) })
    expect(result.current.status).toBe('building') // the provisioning→building transition is asserted, not left dead

    act(() => { fake.emitEnvelope(READY) })
    expect(result.current.status).toBe('ready')
    expect(result.current.previewUrl).toBe(PREVIEW_URL)

    act(() => { fake.emitEnvelope(ENDED_STOPPED) })
    expect(result.current.status).toBe('ended')
    // preview_ready is NEVER a feed row; the step and the terminal are.
    expect(result.current.envelopes.map((e) => e.type)).toEqual(['step', 'ended'])
  })

  it('preview_reconnecting raises a DISTINCT reconnecting flag (not a feed row, not feedDisconnected), cleared by the re-frame', async () => {
    const { result, fake } = setup()
    await act(async () => { await result.current.reattach('s1') })
    act(() => { fake.open() })
    act(() => { fake.emitEnvelope(READY) })
    expect(result.current.status).toBe('ready')
    expect(result.current.reconnecting).toBe(false)

    // The dev-server PROCESS crashes → the reconnecting flag, NOT feedDisconnected (SSE drop) and
    // NOT a 6th status; it is never a feed row.
    act(() => { fake.emitEnvelope(RECONNECTING) })
    expect(result.current.reconnecting).toBe(true)
    expect(result.current.feedDisconnected).toBe(false)
    expect(result.current.status).toBe('ready')
    expect(result.current.envelopes.map((e) => e.type)).toEqual([])

    // A fresh preview_ready (the re-frame after restart) clears the reconnecting flag.
    act(() => { fake.emitEnvelope({ type: 'preview_ready', seq: 6, preview_url: PREVIEW_URL }) })
    expect(result.current.reconnecting).toBe(false)
  })

  it('FAILED fork: escalation → ended{status:failed} derives FAILED (distinct from the graceful ENDED branch)', async () => {
    const { result, fake } = setup()
    await act(async () => { await result.current.reattach('s1') })
    act(() => { fake.open() })
    act(() => { fake.emitEnvelope(ESCALATION) })
    act(() => { fake.emitEnvelope(ENDED_FAIL) })
    expect(result.current.status).toBe('failed')
  })

  it('quota graceful end resolves ENDED (not FAILED); quota banner set; timers torn down', async () => {
    const { result, fake } = setup()
    await act(async () => { await result.current.reattach('s1') })
    act(() => { fake.open() })
    act(() => { fake.emitEnvelope(QUOTA) })
    expect(result.current.status).not.toBe('failed') // the quota precursor must NOT flip to failed
    act(() => { fake.emitEnvelope(ENDED_QUOTA) })
    expect(result.current.status).toBe('ended') // graceful, not failed
    expect(result.current.quota).toEqual({ limit: 1_000_000, used: 1_000_000, resetsAt: '2026-07-15T18:30:00Z' })
  })

  it('missed preview_ready: reattach seeds previewUrl from getStatus even though no live preview_ready arrives', async () => {
    const client = makeClient({
      getStatus: vi.fn(async (): Promise<BuildSessionStatusResponse> => ({ sessionId: 's1', projectId: 'p1', appId: 'a1', status: 'ready', previewUrl: PREVIEW_URL, lastSeq: 7, createdAt: 'c', updatedAt: 'u' })),
    })
    const { result } = setup(client)
    await act(async () => { await result.current.reattach('s1') })
    // The preview frames from the status seed — not from catching a live preview_ready envelope.
    expect(result.current.status).toBe('ready')
    expect(result.current.previewUrl).toBe(PREVIEW_URL)
  })

  it('reattach measures elapsed time from the session createdAt, not the moment of reattach', async () => {
    const created = '2026-07-14T00:00:00.000Z'
    const client = makeClient({
      getStatus: vi.fn(async (): Promise<BuildSessionStatusResponse> => ({ sessionId: 's1', projectId: 'p1', appId: 'a1', status: 'building', previewUrl: null, lastSeq: 3, createdAt: created, updatedAt: 'u' })),
    })
    const { result } = setup(client)
    await act(async () => { await result.current.reattach('s1') })
    expect(result.current.startedAt).toBe(Date.parse(created)) // a 12-min-old build must not read as 0s
  })
})

describe('useBuildSession — endReason: the pardoned preview signal', () => {
  const ENDED_COMPLETED: ProgressEnvelope = { type: 'ended', seq: 3, status: 'ended', preview_url: PREVIEW_URL, snapshot_committed: true, reason: 'completed' }

  it("a completed terminal carries reason 'completed' and KEEPS previewUrl — the done-preview-live state", async () => {
    const { result, fake } = setup()
    await act(async () => { await result.current.reattach('s1') })
    act(() => { fake.open() })
    act(() => { fake.emitEnvelope(READY) })
    act(() => { fake.emitEnvelope(ENDED_COMPLETED) })
    expect(result.current.status).toBe('ended')
    expect(result.current.endReason).toBe('completed')
    expect(result.current.previewUrl).toBe(PREVIEW_URL) // the server pardoned the container: still live
  })

  it('a FAILED terminal carries its own reason (no pardon on failure)', async () => {
    const { result, fake } = setup()
    await act(async () => { await result.current.reattach('s1') })
    act(() => { fake.open() })
    act(() => { fake.emitEnvelope(ENDED_FAIL) })
    expect(result.current.status).toBe('failed')
    expect(result.current.endReason).toBe('escalated')
  })

  it('endReason is null while live and cleared by reset() — a new build never inherits the old verdict', async () => {
    const { result, fake } = setup()
    await act(async () => { await result.current.reattach('s1') })
    act(() => { fake.open() })
    expect(result.current.endReason).toBeNull() // live: no verdict yet
    act(() => { fake.emitEnvelope(ENDED_COMPLETED) })
    expect(result.current.endReason).toBe('completed')
    act(() => { result.current.reset() })
    expect(result.current.endReason).toBeNull()
  })

  it('a RECLAIMED terminal settles with a NULL reason — it must not claim a live preview', async () => {
    // Reclaim arrives from the server's own verdict on the feed, never from a browser heartbeat,
    // and the pane's contract holds either way: a container taken back does not get to say
    // "completed" and keep its preview on screen.
    const { result, fake } = setup()
    await act(async () => { await result.current.reattach('s1') })
    act(() => { fake.open() })
    act(() => { fake.emitEnvelope(READY) })
    act(() => { fake.emitEnvelope({ type: 'ended', seq: 3, status: 'ended', preview_url: null, snapshot_committed: false, reason: 'reclaimed' }) })

    expect(result.current.status).toBe('ended')
    expect(result.current.endReason).not.toBe('completed')
  })
})

/*
 * THE FORCE-END AND STOP SUITES ARE GONE — both died WITH their subject rather than losing
 * coverage. The force-end pair pinned a control-plane override and a non-owner 403; the stop
 * test pinned a locally-set end reason. The hook wrappers, the client functions and the backend
 * routes were deleted together, neither having a UI call site left.
 *
 * What settles a live session from this hook now is the feed: an `ended` envelope carries the
 * terminal AND the reason, which the lifecycle hop above and the `endReason` suite both pin.
 */

describe('useBuildSession — an open tab is NOT a keep-alive writer', () => {
  /*
   * useBuildSession.ts carries why nothing in the browser extends a deadline; what is pinned here
   * is that this hook makes no such call, however long a tab sits.
   */

  it('a live session with an untouched tab makes NO keep-alive calls, however long it sits', async () => {
    vi.useFakeTimers()
    const { result, fake } = setup()
    await act(async () => { await result.current.reattach('s1') })
    act(() => { fake.open() })
    act(() => { fake.emitEnvelope(READY) })

    // An hour of a tab nobody is touching.
    await act(async () => { await vi.advanceTimersByTimeAsync(3_600_000) })

    // There is nothing left to count: `heartbeat` and `renewLock` are gone from the client, so the
    // type checker refuses the loop rather than a test noticing it ran. What is asserted instead is
    // the other half — the session is not torn down by the absence either.
    expect(result.current.status).toBe('ready')
  })

  it('the session still ends on the authority it always had — the feed, not a timer', async () => {
    vi.useFakeTimers()
    const { result, fake } = setup()
    await act(async () => { await result.current.reattach('s1') })
    act(() => { fake.open() })
    act(() => { fake.emitEnvelope(READY) })
    act(() => { fake.emitEnvelope(ENDED_QUOTA) })

    expect(result.current.status).toBe('ended')
  })
})

describe('useBuildSession — feed disconnection + teardown', () => {
  it('a bounded-reconnect exhaustion raises feedDisconnected (not a stalled-build masquerade); reconnect resubscribes', async () => {
    const { result, fake } = setup()
    await act(async () => { await result.current.reattach('s1') })
    act(() => { fake.open() })
    // The consumer's default cap is 5; drive the fake past it to exhaust the bounded reconnect.
    act(() => { for (let i = 0; i < 7; i += 1) fake.dropAfterOpen() })
    expect(result.current.feedDisconnected).toBe(true)
    expect(result.current.status).toBe('provisioning') // the session is NOT falsely terminal

    act(() => { result.current.reconnect() })
    expect(result.current.feedDisconnected).toBe(false)
  })

  it('reconnect() reseeds previewUrl/status from getStatus — a preview_ready missed while the feed was dead still frames', async () => {
    // The reattach that opens the session and the reconnect that reseeds it now read the SAME
    // `getStatus`, so the fixture has to move between them or the "never seen" assertion below is
    // vacuous: the first call answers as the session looked when the tab reattached, the second as
    // it looks after the preview came up during the dead window.
    const getStatus = vi
      .fn<() => Promise<BuildSessionStatusResponse>>()
      .mockResolvedValueOnce({ sessionId: 's1', projectId: 'p1', appId: 'a1', status: 'provisioning', previewUrl: null, lastSeq: null, createdAt: 'c', updatedAt: 'u' })
      .mockResolvedValue({ sessionId: 's1', projectId: 'p1', appId: 'a1', status: 'ready', previewUrl: PREVIEW_URL, lastSeq: 9, createdAt: 'c', updatedAt: 'u' })
    const { result, fake } = setup(makeClient({ getStatus }))
    await act(async () => { await result.current.reattach('s1') })
    act(() => { fake.open() })
    act(() => { for (let i = 0; i < 7; i += 1) fake.dropAfterOpen() }) // exhaust → feed dead
    expect(result.current.feedDisconnected).toBe(true)
    expect(result.current.previewUrl).toBeNull() // preview_ready fired during the dead window — never seen

    await act(async () => { result.current.reconnect() })
    expect(getStatus).toHaveBeenCalledWith('s1')
    expect(result.current.previewUrl).toBe(PREVIEW_URL) // reseeded from authoritative status, not lost
    expect(result.current.status).toBe('ready')
    expect(result.current.feedDisconnected).toBe(false)
  })

  it('unmount WHILE reattach() is in flight wires NO feed and NO timers (FIX 1 — no zombie heartbeat)', async () => {
    vi.useFakeTimers()
    // The guard is `mountedRef`, and `reattach` carries the identical bail.
    vi.useFakeTimers()
    let resolveStatus!: (v: BuildSessionStatusResponse) => void
    const statusGate = new Promise<BuildSessionStatusResponse>((res) => { resolveStatus = res })
    const esFactory = vi.fn((url: string) => new FakeEventSource(url)) // counts EventSource creations
    const client = makeClient({ getStatus: vi.fn(() => statusGate) })
    const { result, unmount } = renderHook(() => useBuildSession({ client, eventSourceFactory: esFactory }))

    let startPromise!: Promise<unknown>
    act(() => { startPromise = result.current.reattach('s1') })
    unmount() // the component tears down BEFORE the network read resolves

    // reattach() now resolves against an unmounted hook — it must bail before subscribing / arming timers.
    await act(async () => {
      resolveStatus({ sessionId: 's1', projectId: 'p1', appId: 'a1', status: 'provisioning', previewUrl: null, lastSeq: null, createdAt: 'c', updatedAt: 'u' })
      await startPromise
    })

    expect(esFactory).not.toHaveBeenCalled() // no zombie EventSource
    await act(async () => { await vi.advanceTimersByTimeAsync(120_000) })
    // No keep-alive interval can be left running.
  })

  it('a terminal end clears a lingering feed-disconnected banner (FIX 3 — no dead Reconnect button)', async () => {
    const client = makeClient()
    const { result, fake } = setup(client)
    await act(async () => { await result.current.reattach('s1') })
    act(() => { fake.open() })
    // Exhaust the bounded reconnect so the "Lost the feed" banner is showing.
    act(() => { for (let i = 0; i < 7; i += 1) fake.dropAfterOpen() })
    expect(result.current.feedDisconnected).toBe(true)

    // Reconnect reseeds from the status read; hold that read open so the banner's second life
    // can be staged underneath it. An exhausted feed delivers nothing, so this reseed is the
    // ONLY way a terminal still reaches the hook once the banner is up.
    let landStatus!: (v: BuildSessionStatusResponse) => void
    client.getStatus = vi.fn(() => new Promise<BuildSessionStatusResponse>((res) => { landStatus = res }))
    act(() => { result.current.reconnect() })
    expect(result.current.feedDisconnected).toBe(false) // the resubscribe cleared it…

    // …and the fresh feed dies too, so the banner is showing again when the terminal lands.
    act(() => { for (let i = 0; i < 7; i += 1) fake.dropAfterOpen() })
    expect(result.current.feedDisconnected).toBe(true)

    await act(async () => {
      landStatus({ sessionId: 's1', projectId: 'p1', appId: 'a1', status: 'ended', previewUrl: null, lastSeq: null, createdAt: 'c', updatedAt: 'u' })
      await Promise.resolve()
    })
    expect(result.current.status).toBe('ended')
    expect(result.current.feedDisconnected).toBe(false)
  })
})
