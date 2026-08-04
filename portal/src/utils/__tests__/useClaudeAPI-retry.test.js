import { describe, it, expect, vi } from 'vitest'
import { fetchClaudeStream, STREAM_STALL_TIMEOUT_MS, FIRST_BYTE_TIMEOUT_MS } from '../../hooks/useClaudeAPI'

const enc = (s) => new TextEncoder().encode(s)

function sseResponse(lines, { ok = true, status = 200 } = {}) {
  const encoder = new TextEncoder()
  let i = 0
  return {
    ok,
    status,
    json: async () => ({}),
    body: {
      getReader() {
        return {
          read: async () =>
            i < lines.length
              ? { done: false, value: encoder.encode(lines[i++]) }
              : { done: true, value: undefined },
          cancel: async () => {},
        }
      },
    },
  }
}

const STREAM = ['data: {"delta":{"text":"Hello"}}\n\n', 'data: {"delta":{"text":" world"}}\n\n', 'data: [DONE]\n\n']
const unauthorized = () => ({ ok: false, status: 401, json: async () => ({ error: { message: 'unauth' } }) })

describe('fetchClaudeStream', () => {
  it('AE4: a pre-stream 401 triggers one refresh and a successful retry that streams', async () => {
    const fetchImpl = vi
      .fn()
      .mockResolvedValueOnce(unauthorized())
      .mockResolvedValueOnce(sseResponse(STREAM))
    const refresh = vi.fn(async () => true) // cookie-session refresh returns a success boolean
    const chunks = []

    const text = await fetchClaudeStream({
      body: { messages: [] },
      onChunk: (delta) => chunks.push(delta),
      fetchImpl,
      getToken: () => 'stale-token',
      refresh,
    })

    expect(refresh).toHaveBeenCalledTimes(1)
    expect(fetchImpl).toHaveBeenCalledTimes(2)
    // The retry carries NO bearer header (never `Bearer true`); the refreshed
    // session cookie rides along automatically.
    expect(fetchImpl.mock.calls[1][1].headers.Authorization).toBeUndefined()
    expect(text).toBe('Hello world')
    expect(chunks).toEqual(['Hello', ' world'])
  })

  it('happy path: a valid token streams in a single request, no refresh, no retry', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(sseResponse(STREAM))
    const refresh = vi.fn()

    const text = await fetchClaudeStream({
      body: { messages: [] },
      fetchImpl,
      getToken: () => 'good-token',
      refresh,
    })

    expect(fetchImpl).toHaveBeenCalledTimes(1)
    expect(fetchImpl.mock.calls[0][1].headers.Authorization).toBe('Bearer good-token')
    expect(refresh).not.toHaveBeenCalled()
    expect(text).toBe('Hello world')
  })

  it('a 401 where refresh also fails throws an AUTH_REFRESH_FAILED error', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(unauthorized())
    const refresh = vi.fn(async () => null)

    await expect(
      fetchClaudeStream({ body: { messages: [] }, fetchImpl, getToken: () => 't', refresh }),
    ).rejects.toMatchObject({ code: 'AUTH_REFRESH_FAILED' })
    expect(refresh).toHaveBeenCalledTimes(1)
  })

  it('a 429 daily-limit response throws a user-ready message naming the limit, never reading the stream', async () => {
    const fetchImpl = vi.fn().mockResolvedValue({
      ok: false,
      status: 429,
      json: async () => ({
        error: { code: 'daily_token_limit_exceeded', limit: 1000000, used: 1000000, remaining: 0 },
      }),
    })
    await expect(
      fetchClaudeStream({ body: { messages: [] }, fetchImpl, getToken: () => 't', refresh: vi.fn() }),
    ).rejects.toThrow(/1,000,000 tokens/)
  })

  it('the daily-limit message points the user at the administrator for a higher plan', async () => {
    const withLimit = vi.fn().mockResolvedValue({
      ok: false,
      status: 429,
      json: async () => ({ error: { code: 'daily_token_limit_exceeded', limit: 100 } }),
    })
    await expect(
      fetchClaudeStream({ body: { messages: [] }, fetchImpl: withLimit, getToken: () => 't', refresh: vi.fn() }),
    ).rejects.toThrow(/contact your administrator to enable a higher plan/i)

    // …and on the no-limit fallback branch too.
    const noLimit = vi.fn().mockResolvedValue({
      ok: false,
      status: 429,
      json: async () => ({ error: { code: 'daily_token_limit_exceeded' } }),
    })
    await expect(
      fetchClaudeStream({ body: { messages: [] }, fetchImpl: noLimit, getToken: () => 't', refresh: vi.fn() }),
    ).rejects.toThrow(/contact your administrator to enable a higher plan/i)
  })

  it('a 429 WITHOUT the known code falls through to the generic error (back-compat)', async () => {
    const fetchImpl = vi.fn().mockResolvedValue({
      ok: false,
      status: 429,
      json: async () => ({ error: { message: 'slow down' } }),
    })
    await expect(
      fetchClaudeStream({ body: { messages: [] }, fetchImpl, getToken: () => 't', refresh: vi.fn() }),
    ).rejects.toThrow('slow down')
  })

  it('a non-401 error surfaces the server message and never refreshes', async () => {
    const fetchImpl = vi.fn().mockResolvedValue({
      ok: false,
      status: 500,
      json: async () => ({ error: { message: 'boom' } }),
    })
    const refresh = vi.fn()

    await expect(
      fetchClaudeStream({ body: { messages: [] }, fetchImpl, getToken: () => 't', refresh }),
    ).rejects.toThrow('boom')
    expect(refresh).not.toHaveBeenCalled()
  })

  it('aborting mid-stream resolves with partial text instead of throwing', async () => {
    const fetchImpl = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({}),
      body: {
        getReader() {
          return {
            read: async () => {
              const err = new Error('aborted')
              err.name = 'AbortError'
              throw err
            },
            cancel: async () => {},
          }
        },
      },
    })

    const text = await fetchClaudeStream({
      body: { messages: [] },
      fetchImpl,
      getToken: () => 't',
      refresh: vi.fn(),
      signal: { aborted: true },
    })
    expect(text).toBe('')
  })

  it('forwards the provided AbortSignal to fetch (real abort wiring)', async () => {
    const controller = new AbortController()
    const fetchImpl = vi.fn().mockResolvedValue(sseResponse(STREAM))

    await fetchClaudeStream({
      body: { messages: [] },
      fetchImpl,
      getToken: () => 'good-token',
      refresh: vi.fn(),
      signal: controller.signal,
    })
    expect(fetchImpl.mock.calls[0][1].signal).toBe(controller.signal)
  })

  it('F1: a stalled socket (no bytes) trips the watchdog → StreamStalledError, NOT a silent partial', async () => {
    // The core anti-hang fix: a dead-but-unclosed socket makes read() never resolve, which would
    // hang the caller forever. The idle watchdog must surface a DISTINCT stall error (re-thrown, not
    // swallowed like an abort) so the caller shows the error banner instead of a truncated reply.
    vi.useFakeTimers()
    try {
      const fetchImpl = vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: async () => ({}),
        body: { getReader: () => ({ read: () => new Promise(() => {}), cancel: async () => {} }) },
      })
      const promise = fetchClaudeStream({
        body: { messages: [] },
        fetchImpl,
        getToken: () => 't',
        refresh: vi.fn(),
        signal: {},
      })
      const assertion = expect(promise).rejects.toMatchObject({ name: 'StreamStalledError' })
      await vi.advanceTimersByTimeAsync(STREAM_STALL_TIMEOUT_MS + 100)
      await assertion
    } finally {
      vi.useRealTimers()
    }
  })

  it('F1: bytes arriving within the window (incl. a `: ping` keepalive) keep the stream alive', async () => {
    // The watchdog resets on ANY received byte — a keepalive `: ping` comment (skipped by the
    // delta filter) counts. A slow-but-alive stream fed inside the window must NEVER false-trip.
    vi.useFakeTimers()
    try {
      const frames = [
        'data: {"delta":{"text":"Hi"}}\n\n',
        ': ping\n\n', // keepalive during a server→model retry backoff — resets the watchdog, adds no text
        ': ping\n\n',
        'data: [DONE]\n\n',
      ]
      let i = 0
      const fetchImpl = vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: async () => ({}),
        body: {
          getReader: () => ({
            // Each frame arrives at HALF the stall window — always resets the timer before it fires.
            read: () =>
              i < frames.length
                ? new Promise((res) =>
                    setTimeout(() => res({ done: false, value: enc(frames[i++]) }), STREAM_STALL_TIMEOUT_MS / 2),
                  )
                : Promise.resolve({ done: true, value: undefined }),
            cancel: async () => {},
          }),
        },
      })
      const chunks = []
      const promise = fetchClaudeStream({
        body: { messages: [] },
        onChunk: (d) => chunks.push(d),
        fetchImpl,
        getToken: () => 't',
        refresh: vi.fn(),
        signal: {},
      })
      await vi.advanceTimersByTimeAsync(STREAM_STALL_TIMEOUT_MS * (frames.length + 1))
      const text = await promise
      expect(text).toBe('Hi') // completed cleanly; the pings carried no text but kept it alive
      expect(chunks).toEqual(['Hi'])
    } finally {
      vi.useRealTimers()
    }
  })

  it('F1: a clean EOF WITHOUT the terminal [DONE] is rejected as truncated, never returned as success', async () => {
    // A mid-stream relay failure (_END_FAIL) closes the SSE without [DONE]. The partial must be
    // REJECTED so the caller drops the bubble + offers Regenerate — not persisted as a full reply.
    const fetchImpl = vi.fn().mockResolvedValue(sseResponse(['data: {"delta":{"text":"partial"}}\n\n']))
    await expect(
      fetchClaudeStream({ body: { messages: [] }, fetchImpl, getToken: () => 't', refresh: vi.fn(), signal: {} }),
    ).rejects.toMatchObject({ name: 'StreamIncompleteError' })
  })

  it('a properly [DONE]-terminated stream resolves with the full text (no false truncation)', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(sseResponse(STREAM)) // STREAM ends with data: [DONE]
    const text = await fetchClaudeStream({ body: { messages: [] }, fetchImpl, getToken: () => 't', refresh: vi.fn(), signal: {} })
    expect(text).toBe('Hello world')
  })

  it('F1: a POST that never returns headers is bounded by the first-byte timeout → StreamStalledError', async () => {
    // The reader watchdog only covers post-header reads; a socket that half-closes BEFORE headers
    // would hang the POST forever. The first-byte timeout converts that into a clean error.
    vi.useFakeTimers()
    try {
      const fetchImpl = vi.fn(() => new Promise(() => {})) // never resolves (half-open before headers)
      const promise = fetchClaudeStream({
        body: { messages: [] },
        fetchImpl,
        getToken: () => 't',
        refresh: vi.fn(),
        signal: {},
      })
      const assertion = expect(promise).rejects.toMatchObject({ name: 'StreamStalledError' })
      await vi.advanceTimersByTimeAsync(FIRST_BYTE_TIMEOUT_MS + 100)
      await assertion
    } finally {
      vi.useRealTimers()
    }
  })

  it('F9: a stall ABORTS the underlying request AND still surfaces StreamStalledError', async () => {
    // Without the abort the browser keeps the dead socket open and the server keeps generating
    // (and billing) into it. The abort must not reclassify the stall as a benign navigation
    // abort — the error banner still owns this outcome.
    vi.useFakeTimers()
    try {
      const signal = { aborted: false }
      const abort = vi.fn(() => { signal.aborted = true }) // mirrors AbortController semantics
      const fetchImpl = vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: async () => ({}),
        body: { getReader: () => ({ read: () => new Promise(() => {}), cancel: async () => {} }) },
      })
      const promise = fetchClaudeStream({
        body: { messages: [] }, fetchImpl, getToken: () => 't', refresh: vi.fn(), signal, abort,
      })
      const assertion = expect(promise).rejects.toMatchObject({ name: 'StreamStalledError' })
      await vi.advanceTimersByTimeAsync(STREAM_STALL_TIMEOUT_MS + 100)
      await assertion
      expect(abort).toHaveBeenCalledTimes(1)
    } finally {
      vi.useRealTimers()
    }
  })

  it('F9: a mid-stream stall with partial text already received still errors (not a silent partial)', async () => {
    // The stall's own abort flips `signal.aborted` true — the abort-swallow branch must re-check
    // the stall flag instead of returning the partial as success.
    vi.useFakeTimers()
    try {
      const signal = { aborted: false }
      const abort = vi.fn(() => { signal.aborted = true })
      let reads = 0
      const fetchImpl = vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: async () => ({}),
        body: {
          getReader: () => ({
            read: () =>
              reads++ === 0
                ? Promise.resolve({ done: false, value: enc('data: {"delta":{"text":"partial"}}\n\n') })
                : new Promise(() => {}), // then the socket goes dead
            cancel: async () => {},
          }),
        },
      })
      const promise = fetchClaudeStream({
        body: { messages: [] }, fetchImpl, getToken: () => 't', refresh: vi.fn(), signal, abort,
      })
      const assertion = expect(promise).rejects.toMatchObject({ name: 'StreamStalledError' })
      await vi.advanceTimersByTimeAsync(STREAM_STALL_TIMEOUT_MS + 100)
      await assertion
      expect(abort).toHaveBeenCalled()
    } finally {
      vi.useRealTimers()
    }
  })

  it('F14: a `data: [DONE]` torn across two read() chunks reassembles — no false truncation', async () => {
    // SSE frames are not aligned to read() chunks; parsing per-chunk read the torn halves as
    // garbage and threw StreamIncompleteError on a stream the server finished cleanly (whose
    // "try again" then double-bills).
    const fetchImpl = vi.fn().mockResolvedValue(
      sseResponse(['data: {"delta":{"text":"Hi"}}\n\ndata: [DO', 'NE]\n\n']),
    )
    const text = await fetchClaudeStream({
      body: { messages: [] }, fetchImpl, getToken: () => 't', refresh: vi.fn(), signal: {},
    })
    expect(text).toBe('Hi')
  })

  it('F14: a delta frame split mid-JSON reassembles into exactly one delta', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(
      sseResponse(['data: {"delta":{"te', 'xt":"Hello world"}}\n\ndata: [DONE]\n\n']),
    )
    const chunks = []
    const text = await fetchClaudeStream({
      body: { messages: [] }, onChunk: (d) => chunks.push(d), fetchImpl, getToken: () => 't', refresh: vi.fn(), signal: {},
    })
    expect(text).toBe('Hello world')
    expect(chunks).toEqual(['Hello world'])
  })

  it('F14: a terminal [DONE] with no trailing newline still counts (the tail is flushed at EOF)', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(
      sseResponse(['data: {"delta":{"text":"Hi"}}\n\n', 'data: [DONE]']),
    )
    const text = await fetchClaudeStream({
      body: { messages: [] }, fetchImpl, getToken: () => 't', refresh: vi.fn(), signal: {},
    })
    expect(text).toBe('Hi')
  })

  it('F6: the first-byte budget outlasts the server model-retry worst case', () => {
    // MIRROR of backend/src/config.py FoundryConfig defaults. The relay awaits the FIRST model
    // delta before even sending response headers (claude/router.py `_stream`), and the `: ping`
    // keepalive only starts after that — so NOTHING resets this client timer while the server's
    // SDK retries (connect + per-chunk read, × attempts). If the backend retunes those knobs,
    // this pin fails and the budget must be re-derived instead of silently re-opening the gap
    // where the client gives up on a turn the server goes on to finish AND bill.
    const SERVER_CONNECT_TIMEOUT_S = 10 // FoundryConfig.connect_timeout_s
    const SERVER_READ_TIMEOUT_S = 120 // FoundryConfig.read_timeout_s
    const SERVER_MAX_RETRIES = 2 // FoundryConfig.max_retries
    const serverWorstCaseMs =
      (SERVER_CONNECT_TIMEOUT_S + SERVER_READ_TIMEOUT_S) * (SERVER_MAX_RETRIES + 1) * 1000
    expect(FIRST_BYTE_TIMEOUT_MS).toBeGreaterThan(serverWorstCaseMs)
  })

  it('a 401 that persists after a SUCCESSFUL refresh still throws AUTH_REFRESH_FAILED', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(unauthorized()) // both the initial post and the retry 401
    const refresh = vi.fn(async () => 'new-token') // refresh itself succeeds

    await expect(
      fetchClaudeStream({ body: { messages: [] }, fetchImpl, getToken: () => 'stale', refresh }),
    ).rejects.toMatchObject({ code: 'AUTH_REFRESH_FAILED' })
    expect(refresh).toHaveBeenCalledTimes(1)
    expect(fetchImpl).toHaveBeenCalledTimes(2)
  })
})
