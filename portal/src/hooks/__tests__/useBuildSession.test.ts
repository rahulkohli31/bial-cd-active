import { describe, it, expect, vi, afterEach } from 'vitest'
import { renderHook, act, cleanup } from '@testing-library/react'
import { useBuildSession } from '../useBuildSession'
import { BuildSessionAlreadyActiveError } from '../../utils/buildSessionApi'
import type { BuildSessionClient } from '../../utils/buildSessionApi'
import { ApiError } from '../../utils/apiError'
import { FakeEventSource } from '../../utils/buildSessionMock'
import type { ProgressEnvelope, BuildSessionStatusResponse, StartBuildResponse } from '../../utils/buildSessionTypes'

afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

const LOCK = { sessionId: 's1', held: true, ownerUserId: 'u', ttlSeconds: 900, expiresAt: 'e' }
const HB = { sessionId: 's1', alive: true, cadenceSeconds: 30, heartbeatExpiresAt: 'e' }
const PREVIEW_URL = 'https://app.example.azurecontainerapps.io/'

function makeClient(over: Partial<BuildSessionClient> = {}): BuildSessionClient {
  return {
    start: vi.fn(async () => ({ sessionId: 's1', projectId: 'p1', appId: 'a1', status: 'provisioning' as const, previewUrl: null, createdAt: 'c' })),
    stop: vi.fn(async () => ({ sessionId: 's1', status: 'ended' as const })),
    getStatus: vi.fn(async () => ({ sessionId: 's1', projectId: 'p1', appId: 'a1', status: 'provisioning' as const, previewUrl: null, lastSeq: null, createdAt: 'c', updatedAt: 'u' })),
    acquireLock: vi.fn(async () => LOCK),
    renewLock: vi.fn(async () => LOCK),
    releaseLock: vi.fn(async () => ({ sessionId: 's1', released: true })),
    forceEnd: vi.fn(async () => ({ sessionId: 's1', status: 'ended' as const })),
    heartbeat: vi.fn(async () => HB),
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
const ESCALATION: ProgressEnvelope = { type: 'escalation', seq: 3, reason: 'exhausted', detail: 'gave up', last_error: null }
const ENDED_FAIL: ProgressEnvelope = { type: 'ended', seq: 4, status: 'failed', preview_url: null, snapshot_committed: false, reason: 'escalated' }
const QUOTA: ProgressEnvelope = { type: 'quota_exceeded', seq: 3, limit: 1_000_000, used: 1_000_000, resets_at: '2026-07-15T18:30:00Z' }
const ENDED_QUOTA: ProgressEnvelope = { type: 'ended', seq: 4, status: 'ended', preview_url: null, snapshot_committed: true, reason: 'quota_exceeded' }

describe('useBuildSession — start + status derivation (C3 §1/§2)', () => {
  it('start begins a session at provisioning', async () => {
    const { result } = setup()
    await act(async () => {
      const out = await result.current.start('p1', 'build it')
      expect(out).toEqual({ kind: 'started', sessionId: 's1' })
    })
    expect(result.current.sessionId).toBe('s1')
    expect(result.current.status).toBe('provisioning')
  })

  it('a 409 build_session_already_active surfaces the block (existing sessionId), no stream started', async () => {
    const client = makeClient({ start: vi.fn(async () => { throw new BuildSessionAlreadyActiveError('busy', 'existing-2') }) })
    const { result } = setup(client)
    await act(async () => {
      const out = await result.current.start('p1', 'x')
      expect(out).toEqual({ kind: 'blocked', existingSessionId: 'existing-2' })
    })
    expect(result.current.blocked).toEqual({ existingSessionId: 'existing-2' })
    expect(result.current.status).toBeNull()
  })

  it('derives status at EACH hop: provisioning →(first step)→ building → preview_ready → ready → stop → ended', async () => {
    const { result, fake } = setup()
    await act(async () => { await result.current.start('p1', 'x') })
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

  it('FAILED fork: escalation → ended{status:failed} derives FAILED (distinct from the graceful ENDED branch)', async () => {
    const { result, fake } = setup()
    await act(async () => { await result.current.start('p1', 'x') })
    act(() => { fake.open() })
    act(() => { fake.emitEnvelope(ESCALATION) })
    act(() => { fake.emitEnvelope(ENDED_FAIL) })
    expect(result.current.status).toBe('failed')
  })

  it('quota graceful end resolves ENDED (not FAILED); quota banner set; timers torn down (C7 §8)', async () => {
    const { result, fake } = setup()
    await act(async () => { await result.current.start('p1', 'x') })
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

describe('useBuildSession — stop / force-end (C3 §2.2/§3.4)', () => {
  it('forceEnd resolves terminal from the control-plane response, overriding the envelope stream (mid-building, no ended envelope)', async () => {
    const forceEnd = vi.fn(async () => ({ sessionId: 's1', status: 'ended' as const }))
    const { result, fake } = setup(makeClient({ forceEnd }))
    await act(async () => { await result.current.start('p1', 'x') })
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
    await act(async () => { await result.current.start('p1', 'x') })
    act(() => { fake.open() })
    act(() => { fake.emitEnvelope(STEP) })

    await act(async () => { await result.current.forceEnd() })
    expect(result.current.error).toMatch(/not the owner/i)
    expect(result.current.status).toBe('building') // still active — the failed force-end did not fake a terminal
  })
})

describe('useBuildSession — keep-alive fails closed (C3 §3.2/§3.5, frozen-tab case)', () => {
  it.each([
    ['heartbeat 404', { heartbeat: vi.fn(async () => { throw new ApiError('gone', 404) }) }],
    ['renew 409 lock_lost', { renewLock: vi.fn(async () => { throw new ApiError('lost', 409, 'build_session_lock_lost') }) }],
    ['generic 5xx heartbeat', { heartbeat: vi.fn(async () => { throw new ApiError('boom', 503) }) }],
  ])('%s → the session is reclaimed and both timers clear, with NO SSE ended', async (_label, over) => {
    vi.useFakeTimers()
    const client = makeClient(over)
    const { result } = setup(client)
    await act(async () => { await result.current.start('p1', 'x') })

    // Advance past a renew cadence (300 s) so BOTH the 30 s heartbeat and the 300 s renew have fired.
    await act(async () => { await vi.advanceTimersByTimeAsync(300_000) })

    expect(result.current.status).toBe('ended') // graceful reaper teardown, surfaced as ended
    expect(result.current.reclaimed).toBe(true) // NOT a 6th status member — a distinct flag

    // Timers cleared: advancing further fires no more keep-alive calls.
    const hbCalls = (client.heartbeat as ReturnType<typeof vi.fn>).mock.calls.length
    await act(async () => { await vi.advanceTimersByTimeAsync(120_000) })
    expect((client.heartbeat as ReturnType<typeof vi.fn>).mock.calls.length).toBe(hbCalls)
  })

  it('heartbeat fires every 30 s and lock-renew every 300 s while the session is live', async () => {
    vi.useFakeTimers()
    const { result, client } = setup()
    await act(async () => { await result.current.start('p1', 'x') })

    await act(async () => { await vi.advanceTimersByTimeAsync(60_000) })
    expect((client.heartbeat as ReturnType<typeof vi.fn>).mock.calls.length).toBe(2) // 2 beats in 60 s
    expect((client.renewLock as ReturnType<typeof vi.fn>).mock.calls.length).toBe(0) // renew not yet due

    await act(async () => { await vi.advanceTimersByTimeAsync(240_000) })
    expect((client.renewLock as ReturnType<typeof vi.fn>).mock.calls.length).toBe(1) // 1 renew at 300 s
  })
})

describe('useBuildSession — feed disconnection + teardown (KTD-1)', () => {
  it('a bounded-reconnect exhaustion raises feedDisconnected (not a stalled-build masquerade); reconnect resubscribes', async () => {
    const { result, fake } = setup()
    await act(async () => { await result.current.start('p1', 'x') })
    act(() => { fake.open() })
    // The consumer's default cap is 5; drive the fake past it to exhaust the bounded reconnect.
    act(() => { for (let i = 0; i < 7; i += 1) fake.dropAfterOpen() })
    expect(result.current.feedDisconnected).toBe(true)
    expect(result.current.status).toBe('provisioning') // the session is NOT falsely terminal

    act(() => { result.current.reconnect() })
    expect(result.current.feedDisconnected).toBe(false)
  })

  it('a stale keep-alive rejection from a PRIOR session does not reclaim the new one (fenced by sid, review F1)', async () => {
    vi.useFakeTimers()
    let rejectHb: ((e: unknown) => void) | null = null
    const client = makeClient({
      start: vi
        .fn()
        .mockResolvedValueOnce({ sessionId: 's1', projectId: 'p1', appId: 'a', status: 'provisioning' as const, previewUrl: null, createdAt: 'c' })
        .mockResolvedValueOnce({ sessionId: 's2', projectId: 'p1', appId: 'a', status: 'provisioning' as const, previewUrl: null, createdAt: 'c' }),
      heartbeat: vi.fn(() => new Promise<never>((_, rej) => { rejectHb = rej })),
    })
    const { result } = setup(client)
    await act(async () => { await result.current.start('p1', 'a') }) // session s1
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000) }) // heartbeat('s1') fired, still pending
    await act(async () => { await result.current.start('p1', 'b') }) // session s2 — reset replaces s1
    expect(result.current.sessionId).toBe('s2')

    // The stale s1 heartbeat rejects AFTER s2 is live — the fence must stop it reclaiming s2.
    await act(async () => { rejectHb?.(new ApiError('gone', 404)); await Promise.resolve() })
    expect(result.current.reclaimed).toBe(false)
    expect(result.current.status).toBe('provisioning') // the fresh session s2 is untouched
  })

  it('unmount clears the keep-alive intervals (no leaked timers)', async () => {
    vi.useFakeTimers()
    const { result, client, unmount } = setup()
    await act(async () => { await result.current.start('p1', 'x') })
    unmount()
    await act(async () => { await vi.advanceTimersByTimeAsync(120_000) })
    expect((client.heartbeat as ReturnType<typeof vi.fn>).mock.calls.length).toBe(0) // nothing fired after unmount
  })

  it('unmount WHILE start() is in flight wires NO feed and NO timers (FIX 1 — no zombie heartbeat)', async () => {
    vi.useFakeTimers()
    // A start whose network call we resolve by hand, so we can unmount mid-flight.
    let resolveStart!: (v: StartBuildResponse) => void
    const startGate = new Promise<StartBuildResponse>((res) => { resolveStart = res })
    const esFactory = vi.fn((url: string) => new FakeEventSource(url)) // counts EventSource creations
    const client = makeClient({ start: vi.fn(() => startGate) })
    const { result, unmount } = renderHook(() => useBuildSession({ client, eventSourceFactory: esFactory }))

    let startPromise!: Promise<unknown>
    act(() => { startPromise = result.current.start('p1', 'x') })
    unmount() // the component tears down BEFORE the network start resolves

    // start() now resolves against an unmounted hook — it must bail before subscribing / arming timers.
    await act(async () => {
      resolveStart({ sessionId: 's1', projectId: 'p1', appId: 'a1', status: 'provisioning', previewUrl: null, createdAt: 'c' })
      await startPromise
    })

    expect(esFactory).not.toHaveBeenCalled() // no zombie EventSource
    await act(async () => { await vi.advanceTimersByTimeAsync(120_000) })
    expect((client.heartbeat as ReturnType<typeof vi.fn>).mock.calls.length).toBe(0) // no keep-alive interval left running
  })

  it('a terminal end clears a lingering feed-disconnected banner (FIX 3 — no dead Reconnect button)', async () => {
    const { result, fake } = setup()
    await act(async () => { await result.current.start('p1', 'x') })
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
