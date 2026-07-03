import { describe, it, expect, vi } from 'vitest'
import { fetchClaudeStream } from '../../hooks/useClaudeAPI.js'

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
