/**
 * `relativeTime` — the boundaries, which nothing pinned.
 *
 * It had tests when it lived in `chatHistory`; they went with the re-export #175 retired, and
 * the function kept rendering the projects list's "Details updated" column untested
 * (round-4 review). Each case below sits ON a threshold rather than safely inside one, since
 * a `<` that should be `<=` only ever shows up at the edge.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { relativeTime } from '../relativeTime'

const NOW = new Date('2026-09-03T12:00:00.000Z').getTime()

function ago(ms: number): string {
  return new Date(NOW - ms).toISOString()
}

const SECOND = 1000
const MINUTE = 60 * SECOND
const HOUR = 60 * MINUTE
const DAY = 24 * HOUR

afterEach(() => vi.useRealTimers())

describe('relativeTime', () => {
  it.each([
    [0, 'just now'],
    [59 * SECOND, 'just now'], // still under a minute
    [MINUTE, '1m ago'], // the first minute boundary
    [59 * MINUTE, '59m ago'],
    [HOUR, '1h ago'], // rolls to hours exactly on the hour
    [23 * HOUR, '23h ago'],
    [DAY, '1d ago'], // ...and to days exactly on the day
    [12 * DAY, '12d ago'],
  ])('renders %i ms ago as %s', (elapsed, expected) => {
    vi.useFakeTimers()
    vi.setSystemTime(NOW)
    expect(relativeTime(ago(elapsed))).toBe(expected)
  })

  it('never shows a negative age for a clock that is slightly ahead', () => {
    // Server and browser clocks disagree by seconds routinely, and `updatedAt` is stamped by
    // the server. "-1m ago" is the shape that leaks, so the sub-minute branch has to swallow
    // it rather than arithmetic running below zero.
    vi.useFakeTimers()
    vi.setSystemTime(NOW)
    expect(relativeTime(new Date(NOW + 30 * SECOND).toISOString())).toBe('just now')
  })
})
