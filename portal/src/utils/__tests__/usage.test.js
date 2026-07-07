import { describe, it, expect, vi } from 'vitest'
import { fetchUsageToday, notifyUsageChanged, onUsageChanged } from '../usage.js'

describe('fetchUsageToday', () => {
  it('returns the usage body on a 200 and sends the session cookie', async () => {
    const body = { used: 1234, limit: 1000000, remaining: 998766, resetsAt: '2026-06-18T18:30:00.000Z' }
    const fetchImpl = vi.fn(async () => ({ ok: true, json: async () => body }))
    expect(await fetchUsageToday(fetchImpl)).toEqual(body)
    // Cookie auth: the HttpOnly session cookie rides via credentials:'include' (no Bearer).
    expect(fetchImpl).toHaveBeenCalledWith('/api/usage/today', { credentials: 'include' })
  })

  it('returns null on a non-ok response (e.g. 401 mid-logout) so the badge hides', async () => {
    const fetchImpl = vi.fn(async () => ({ ok: false, status: 401, json: async () => ({}) }))
    expect(await fetchUsageToday(fetchImpl)).toBeNull()
  })

  it('returns null when the fetch throws (network error)', async () => {
    const fetchImpl = vi.fn(async () => {
      throw new Error('network')
    })
    expect(await fetchUsageToday(fetchImpl)).toBeNull()
  })
})

describe('usage change signal', () => {
  it('notifyUsageChanged invokes subscribed handlers; unsubscribe stops them', () => {
    const handler = vi.fn()
    const off = onUsageChanged(handler)
    notifyUsageChanged()
    expect(handler).toHaveBeenCalledTimes(1)
    off()
    notifyUsageChanged()
    expect(handler).toHaveBeenCalledTimes(1) // no longer subscribed
  })
})
