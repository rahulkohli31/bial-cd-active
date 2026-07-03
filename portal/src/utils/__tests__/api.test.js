import { describe, it, expect, vi } from 'vitest'
import { authFetch } from '../api.js'

describe('authFetch', () => {
  it('attaches the Bearer access token', async () => {
    const fetchImpl = vi.fn(async () => ({ status: 200 }))
    await authFetch('/api/x', {}, { getToken: () => 'tok', refresh: vi.fn(), fetchImpl })
    expect(fetchImpl).toHaveBeenCalledOnce()
    expect(fetchImpl.mock.calls[0][1].headers.Authorization).toBe('Bearer tok')
  })

  it('refreshes once on a 401 and retries with NO bearer header (cookie-session model)', async () => {
    const fetchImpl = vi.fn().mockResolvedValueOnce({ status: 401 }).mockResolvedValueOnce({ status: 200 })
    // refreshAccessToken() resolves a SUCCESS BOOLEAN in the cookie model, not a token.
    const refresh = vi.fn(async () => true)
    const res = await authFetch('/api/x', {}, { getToken: () => 'stale', refresh, fetchImpl })
    expect(refresh).toHaveBeenCalledTimes(1)
    expect(fetchImpl).toHaveBeenCalledTimes(2)
    // The retry must NOT template the boolean into `Authorization: Bearer true` —
    // the refreshed session cookie is sent automatically.
    expect(fetchImpl.mock.calls[1][1].headers.Authorization).toBeUndefined()
    expect(res.status).toBe(200)
  })

  it('does not retry when the refresh fails (returns the 401)', async () => {
    const fetchImpl = vi.fn(async () => ({ status: 401 }))
    const refresh = vi.fn(async () => null)
    const res = await authFetch('/api/x', {}, { getToken: () => 'stale', refresh, fetchImpl })
    expect(fetchImpl).toHaveBeenCalledTimes(1)
    expect(res.status).toBe(401)
  })

  it('preserves the caller method + headers', async () => {
    const fetchImpl = vi.fn(async () => ({ status: 200 }))
    await authFetch(
      '/api/x',
      { method: 'PATCH', headers: { 'Content-Type': 'application/json' } },
      { getToken: () => 't', refresh: vi.fn(), fetchImpl },
    )
    const opts = fetchImpl.mock.calls[0][1]
    expect(opts.method).toBe('PATCH')
    expect(opts.headers['Content-Type']).toBe('application/json')
    expect(opts.headers.Authorization).toBe('Bearer t')
  })
})
