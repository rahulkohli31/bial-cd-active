import { describe, it, expect, vi } from 'vitest'
import { submitFeedback } from '../feedback'

// Inject authFetch's deps so no real token/localStorage/network is touched.
const deps = (fetchImpl) => ({ fetchImpl, getToken: () => 'tok', refresh: vi.fn() })

describe('submitFeedback', () => {
  it('POSTs { message, page } to /api/feedback with an auth header and returns the JSON', async () => {
    const fetchImpl = vi.fn(async () => ({ ok: true, status: 201, json: async () => ({ ok: true }) }))
    const result = await submitFeedback('the export button is broken', '/chat', deps(fetchImpl))
    expect(result).toEqual({ ok: true })

    const [url, opts] = fetchImpl.mock.calls[0]
    expect(url).toBe('/api/feedback')
    expect(opts.method).toBe('POST')
    expect(JSON.parse(opts.body)).toEqual({ message: 'the export button is broken', page: '/chat' })
    expect(opts.headers.Authorization).toBe('Bearer tok')
  })

  it('throws the server message on a non-ok response', async () => {
    const fetchImpl = vi.fn(async () => ({
      ok: false,
      status: 400,
      json: async () => ({ error: { message: 'Feedback message cannot be empty.' } }),
    }))
    await expect(submitFeedback('', '/chat', deps(fetchImpl))).rejects.toThrow('Feedback message cannot be empty.')
  })

  it('throws a status-coded fallback when the error body has no message', async () => {
    const fetchImpl = vi.fn(async () => ({ ok: false, status: 429, json: async () => ({}) }))
    await expect(submitFeedback('hi', '/chat', deps(fetchImpl))).rejects.toThrow(/429/)
  })
})
