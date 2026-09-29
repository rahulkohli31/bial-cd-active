/**
 * The counter refreshes as a build's steps land, so a failed refresh must not blank the ring and a
 * slow answer must not land over a newer one.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'

const h = vi.hoisted(() => ({ fetchUsageToday: vi.fn() }))
vi.mock('../../utils/auth', () => ({ isAuthenticated: () => true }))
vi.mock('../../utils/usage', async (orig) => ({
  ...(await orig<typeof import('../../utils/usage')>()),
  fetchUsageToday: () => h.fetchUsageToday(),
}))

import { useUsageToday } from '../useUsageToday'
import { notifyUsageChanged, type UsageToday } from '../../utils/usage'

const reading = (used: number): UsageToday => ({
  used,
  limit: 500_000,
  remaining: 500_000 - used,
  resetsAt: '2026-09-29T18:30:00Z',
})

function Probe() {
  const usage = useUsageToday()
  return <span data-testid="used">{usage === null ? 'nothing' : String(usage.used)}</span>
}
const used = () => screen.getByTestId('used').textContent

beforeEach(() => h.fetchUsageToday.mockReset())
afterEach(cleanup)

describe('useUsageToday', () => {
  it('★ a refresh that fails keeps the last good reading', async () => {
    // Mutation check: set the failed answer as the reading and the ring blinks out mid-build.
    h.fetchUsageToday.mockResolvedValueOnce(reading(196_827)).mockResolvedValueOnce(null)
    render(<Probe />)
    await waitFor(() => expect(used()).toBe('196827'))

    await act(async () => notifyUsageChanged())

    expect(h.fetchUsageToday).toHaveBeenCalledTimes(2)
    expect(used()).toBe('196827')
  })

  it('a first read that fails still shows nothing', async () => {
    h.fetchUsageToday.mockResolvedValueOnce(null)
    render(<Probe />)
    await waitFor(() => expect(h.fetchUsageToday).toHaveBeenCalledTimes(1))
    expect(used()).toBe('nothing')
  })

  it('★ a slow older answer does not land over a newer one', async () => {
    // Mutation check: drop the newest-request check and the figure falls back to the older one.
    let answerSlowly: (value: UsageToday) => void = () => {}
    h.fetchUsageToday
      .mockResolvedValueOnce(reading(301_022))
      .mockImplementationOnce(() => new Promise<UsageToday>((resolve) => (answerSlowly = resolve)))
      .mockResolvedValueOnce(reading(331_744))
    render(<Probe />)
    await waitFor(() => expect(used()).toBe('301022'))

    await act(async () => {
      notifyUsageChanged()
      notifyUsageChanged()
    })
    await waitFor(() => expect(used()).toBe('331744'))

    await act(async () => answerSlowly(reading(313_283)))
    expect(used()).toBe('331744')
  })

  it('follows a newer figure, including one that went down after a reset', async () => {
    h.fetchUsageToday.mockResolvedValueOnce(reading(331_744)).mockResolvedValueOnce(reading(12))
    render(<Probe />)
    await waitFor(() => expect(used()).toBe('331744'))

    await act(async () => notifyUsageChanged())
    expect(used()).toBe('12')
  })
})
