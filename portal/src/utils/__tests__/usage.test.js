import { describe, it, expect, vi, afterEach } from 'vitest'
import {
  createStepUsageRefresh,
  fetchUsageToday,
  notifyUsageChanged,
  onUsageChanged,
  STEP_REFRESH_WINDOW_MS,
} from '../usage'

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

describe('createStepUsageRefresh — the counter follows a build\'s steps, with a brake', () => {
  const listening = []
  afterEach(() => {
    for (const off of listening.splice(0)) off()
    vi.useRealTimers()
  })
  /** Every refresh the counter was asked for, and a fresh brake. */
  const setUp = () => {
    vi.useFakeTimers()
    const refreshed = vi.fn()
    listening.push(onUsageChanged(refreshed))
    return { refreshed, brake: createStepUsageRefresh() }
  }

  it('★ one step asks for a reading at once', () => {
    const { refreshed, brake } = setUp()
    brake.step()
    expect(refreshed).toHaveBeenCalledTimes(1)
  })

  it('★ five steps in two seconds read once at once and once when the window ends', () => {
    // Mutation check: drop the trailing refresh and the last steps are never read.
    const { refreshed, brake } = setUp()
    for (let i = 0; i < 5; i += 1) {
      brake.step()
      vi.advanceTimersByTime(400)
    }
    expect(refreshed).toHaveBeenCalledTimes(1)

    vi.advanceTimersByTime(STEP_REFRESH_WINDOW_MS - 2_000)
    expect(refreshed).toHaveBeenCalledTimes(2)

    vi.advanceTimersByTime(STEP_REFRESH_WINDOW_MS * 3)
    expect(refreshed).toHaveBeenCalledTimes(2)
  })

  it('never reads more than once per window, however steady the steps', () => {
    const { refreshed, brake } = setUp()
    for (let second = 0; second < 12; second += 1) {
      brake.step()
      vi.advanceTimersByTime(1_000)
    }
    // 0 s, 5 s and 10 s, and nothing in between.
    expect(refreshed).toHaveBeenCalledTimes(3)
  })

  it('★ the build ending cancels the pending read, so its own final read is the only one', () => {
    // Mutation check: make `cancel` a no-op and a stale read lands after the final one.
    const { refreshed, brake } = setUp()
    brake.step()
    brake.step()
    expect(refreshed).toHaveBeenCalledTimes(1)

    brake.cancel()
    notifyUsageChanged()
    expect(refreshed).toHaveBeenCalledTimes(2)

    vi.advanceTimersByTime(STEP_REFRESH_WINDOW_MS * 2)
    expect(refreshed).toHaveBeenCalledTimes(2)
  })
})
