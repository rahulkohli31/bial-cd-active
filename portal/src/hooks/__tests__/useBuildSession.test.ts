import { describe, it, expect, vi, afterEach } from 'vitest'
import { renderHook, act, cleanup } from '@testing-library/react'
import { useBuildSession } from '../useBuildSession'
import type { BuildSessionClient } from '../../utils/buildSessionApi'
import { ApiError } from '../../utils/apiError'
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
    stop: vi.fn(async () => ({ sessionId: 's1', status: 'ended' as const })),
    getStatus: vi.fn(async () => ({ sessionId: 's1', projectId: 'p1', appId: 'a1', status: 'provisioning' as const, previewUrl: null, lastSeq: null, createdAt: 'c', updatedAt: 'u' })),
    forceEnd: vi.fn(async () => ({ sessionId: 's1', status: 'ended' as const })),
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

/**
 * `start` AND `relaunch` ARE GONE FROM THIS HOOK, and the two describe blocks that drove them went
 * with them — thirteen tests over two functions that production could not call.
 *
 *   · `start` lost its caller when row creation and the build itself moved inside the turn's own
 *     transaction: a composer send is a TURN. Its client wrapper went in the same change.
 *   · `relaunch` was called only by `ConversationSurface.handleRelaunch`, wired to `LivePreview`'s
 *     `onRelaunch` — a prop the pane accepts and never reads.
 *
 * NOTHING THEY PINNED IS UNCOVERED, and this note is where a reader checks that rather than
 * assuming it:
 *   · the 409 → `BuildSessionAlreadyActiveError` mapping is `postJson`'s, and its two cases are
 *     re-pointed onto `relaunchPreview` in `utils/__tests__/buildSessionApi.test.ts`;
 *   · the `blocked` banner those 409s fed is deleted, and `pages/__tests__/relaunch-chain-retired`
 *     drives BOTH producers to prove it cannot come back;
 *   · the server's verbatim 503 copy (R6) is asserted where it is now read — the live restore path
 *     in `components/workspace/__tests__/StartAppControl.test.tsx`;
 *   · the mid-flight-unmount guard (FIX 1) is re-pointed onto `reattach` below, which carries the
 *     identical `mountedRef` bail.
 *
 * Every scenario below that needed a live session now reaches one through `reattach`, the
 * surviving entry point — a reload onto a build that is still running.
 */
describe('useBuildSession — status derivation across the lifecycle (C3 §1/§2)', () => {
  it('derives status at EACH hop: provisioning →(first step)→ building → preview_ready → ready → stop → ended', async () => {
    const { result, fake } = setup()
    await act(async () => { await result.current.reattach('s1') })
    expect(result.current.status).toBe('provisioning')

    act(() => { fake.open() })
    act(() => { fake.emitEnvelope(STEP) })
    expect(result.current.status).toBe('building') // the provisioning→building transition is asserted, not left dead

    act(() => { fake.emitEnvelope(READY) })
    expect(result.current.status).toBe('ready')
    expect(result.current.previewUrl).toBe(PREVIEW_URL)

    await act(async () => { await result.current.stop() })
    expect(result.current.status).toBe('ended')
    // preview_ready is NEVER a feed row; only the step is in the store.
    expect(result.current.envelopes.map((e) => e.type)).toEqual(['step'])
  })

  it('preview_reconnecting raises a DISTINCT reconnecting flag (not a feed row, not feedDisconnected), cleared by the re-frame (F8/U5)', async () => {
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

  it('quota graceful end resolves ENDED (not FAILED); quota banner set; timers torn down (C7 §8)', async () => {
    const { result, fake } = setup()
    await act(async () => { await result.current.reattach('s1') })
    act(() => { fake.open() })
    act(() => { fake.emitEnvelope(QUOTA) })
    expect(result.current.status).not.toBe('failed') // the quota precursor must NOT flip to failed
    act(() => { fake.emitEnvelope(ENDED_QUOTA) })
    expect(result.current.status).toBe('ended') // graceful, not failed
    expect(result.current.quota).toEqual({ limit: 1_000_000, used: 1_000_000, resetsAt: '2026-07-15T18:30:00Z' })
  })

  it('missed preview_ready (KTD-1): reattach seeds previewUrl from getStatus even though no live preview_ready arrives', async () => {
    const client = makeClient({
      getStatus: vi.fn(async (): Promise<BuildSessionStatusResponse> => ({ sessionId: 's1', projectId: 'p1', appId: 'a1', status: 'ready', previewUrl: PREVIEW_URL, lastSeq: 7, createdAt: 'c', updatedAt: 'u' })),
    })
    const { result } = setup(client)
    await act(async () => { await result.current.reattach('s1') })
    // The preview frames from the status seed — not from catching a live preview_ready envelope.
    expect(result.current.status).toBe('ready')
    expect(result.current.previewUrl).toBe(PREVIEW_URL)
  })

  it('reattach measures elapsed time from the session createdAt, not the moment of reattach (review F3)', async () => {
    const created = '2026-07-14T00:00:00.000Z'
    const client = makeClient({
      getStatus: vi.fn(async (): Promise<BuildSessionStatusResponse> => ({ sessionId: 's1', projectId: 'p1', appId: 'a1', status: 'building', previewUrl: null, lastSeq: 3, createdAt: created, updatedAt: 'u' })),
    })
    const { result } = setup(client)
    await act(async () => { await result.current.reattach('s1') })
    expect(result.current.startedAt).toBe(Date.parse(created)) // a 12-min-old build must not read as 0s
  })
})

describe('useBuildSession — endReason: the pardoned preview signal (#13/R2)', () => {
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

  it("a user stop settles with reason 'stopped_by_user' — NEVER the pardoned 'completed' (the server tore down)", async () => {
    const { result, fake } = setup()
    await act(async () => { await result.current.reattach('s1') })
    act(() => { fake.open() })
    act(() => { fake.emitEnvelope(READY) })
    await act(async () => { await result.current.stop() })
    expect(result.current.status).toBe('ended')
    expect(result.current.endReason).toBe('stopped_by_user')
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
    // Reclaim now arrives from the server's own verdict on the feed rather than from a failed
    // browser heartbeat (U13 deleted that loop), but the pane's contract is unchanged: a
    // container taken back does not get to say "completed" and keep its preview on screen.
    const { result, fake } = setup()
    await act(async () => { await result.current.reattach('s1') })
    act(() => { fake.open() })
    act(() => { fake.emitEnvelope(READY) })
    act(() => { fake.emitEnvelope({ type: 'ended', seq: 3, status: 'ended', preview_url: null, snapshot_committed: false, reason: 'reclaimed' }) })

    expect(result.current.status).toBe('ended')
    expect(result.current.endReason).not.toBe('completed')
  })
})

describe('useBuildSession — stop / force-end (C3 §2.2/§3.4)', () => {
  it('forceEnd resolves terminal from the control-plane response, overriding the envelope stream (mid-building, no ended envelope)', async () => {
    const forceEnd = vi.fn(async () => ({ sessionId: 's1', status: 'ended' as const }))
    const { result, fake } = setup(makeClient({ forceEnd }))
    await act(async () => { await result.current.reattach('s1') })
    act(() => { fake.open() })
    act(() => { fake.emitEnvelope(STEP) }) // building, mid-stream, NO terminal ended envelope
    expect(result.current.status).toBe('building')

    await act(async () => { await result.current.forceEnd() })
    expect(forceEnd).toHaveBeenCalledWith('s1')
    expect(result.current.status).toBe('ended') // driven by ForceEndResponse.status, not the stream
  })

  it('forceEnd 403 (non-owner) is surfaced fail-closed, not swallowed; the session is not silently ended', async () => {
    const forceEnd = vi.fn(async () => { throw new ApiError('Not the owner.', 403, 'build_session_forbidden') })
    const { result, fake } = setup(makeClient({ forceEnd }))
    await act(async () => { await result.current.reattach('s1') })
    act(() => { fake.open() })
    act(() => { fake.emitEnvelope(STEP) })

    await act(async () => { await result.current.forceEnd() })
    expect(result.current.error).toMatch(/not the owner/i)
    expect(result.current.status).toBe('building') // still active — the failed force-end did not fake a terminal
  })
})

describe('useBuildSession — an open tab is NOT a keep-alive writer (U13, R13)', () => {
  /*
   * REPLACES the old "keep-alive fails closed" suite, which characterised a blind `setInterval`
   * that heartbeated and renewed the lock for as long as the tab existed. That loop made AN OPEN
   * TAB a deadline writer — a browser left on a project overnight kept its container alive until
   * morning, and nothing could reclaim it. R13 names the writers permitted to extend a sandbox's
   * deadline and an open connection is deliberately not one of them.
   *
   * What replaced it is not another timer. A turn in flight is held server-side by the R10
   * wall-clock lease (U12), which outranks every writer and — unlike this loop ever did — is
   * legible to a sweep in another process.
   */

  it('a live session with an untouched tab makes NO keep-alive calls, however long it sits', async () => {
    vi.useFakeTimers()
    const { result, fake } = setup()
    await act(async () => { await result.current.reattach('s1') })
    act(() => { fake.open() })
    act(() => { fake.emitEnvelope(READY) })

    // An hour of a tab nobody is touching. Before U13 this was ~120 heartbeats and 12 lock
    // renewals — an hour of a container being told to stay up by a window.
    await act(async () => { await vi.advanceTimersByTimeAsync(3_600_000) })

    // The old assertions here counted calls to `client.heartbeat` and `client.renewLock`. Both
    // functions are now DELETED from the client, which is a strictly stronger guarantee than
    // counting their calls: the type checker refuses the loop rather than a test noticing it ran.
    // ...and the session is not torn down by the absence either: reclamation is the server's
    // decision now, not something the browser talks itself into. This used to also assert
    // `reclaimed === false`; that flag is gone, because deleting the loop deleted its only
    // producer and left the state, its banner and its attention dot standing unreachable.
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

describe('useBuildSession — feed disconnection + teardown (KTD-1)', () => {
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

  it('reconnect() reseeds previewUrl/status from getStatus — a preview_ready missed while the feed was dead still frames (finding #18)', async () => {
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
    // RE-POINTED off `start`, which is gone. The guard is `mountedRef`, and `reattach` carries the
    // identical bail — it is the surviving entry point, so it is now the only place the guard can
    // be driven from at all.
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
    // No keep-alive interval can be left running: the client has no keep-alive surface left.
  })

  it('a terminal end clears a lingering feed-disconnected banner (FIX 3 — no dead Reconnect button)', async () => {
    const { result, fake } = setup()
    await act(async () => { await result.current.reattach('s1') })
    act(() => { fake.open() })
    // Exhaust the bounded reconnect so the "Lost the feed" banner is showing.
    act(() => { for (let i = 0; i < 7; i += 1) fake.dropAfterOpen() })
    expect(result.current.feedDisconnected).toBe(true)

    // The session then reaches a terminal `ended` (a graceful stop) — the stale banner must clear.
    await act(async () => { await result.current.stop() })
    expect(result.current.status).toBe('ended')
    expect(result.current.feedDisconnected).toBe(false)
  })
})
