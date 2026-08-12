/**
 * N4 — the daily-token meter has to be BOTH live and visible.
 *
 * Two halves of one regression, both introduced on this branch. F7 removed the in-rail meter on
 * the grounds that "the header already shows real usage" — but the header's badge was
 * `hidden md:flex`, so below 768px there was no usage feedback anywhere at all; and the header
 * only ever refetched on mount, because `notifyUsageChanged` had exactly one caller in the
 * retiring relay hook and the turn transport never signalled. Between them a citizen could spend
 * their entire daily budget watching a number that never moved — or that was not on screen.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, cleanup } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

const h = vi.hoisted(() => ({
  fetchUsageToday: vi.fn(),
  onUsageChanged: vi.fn(),
  isAuthenticated: vi.fn(() => true),
  getStoredUser: vi.fn(() => ({ email: 'asha@rvaiglobal.com', display_name: 'Asha' })),
  logout: vi.fn(),
}))

vi.mock('../../../utils/usage', () => ({
  fetchUsageToday: h.fetchUsageToday,
  onUsageChanged: h.onUsageChanged,
}))
vi.mock('../../../utils/auth', () => ({
  isAuthenticated: h.isAuthenticated,
  getStoredUser: h.getStoredUser,
  logout: h.logout,
}))
vi.mock('../../../utils/attachmentApi', () => ({ revokeAllAttachmentUrls: vi.fn() }))
vi.mock('../../FeedbackModal', () => ({ default: () => null }))

import Navbar from '../Navbar'

/** The subscriber the Navbar hands `onUsageChanged`, so a test can fire the signal itself. */
let subscriber = null

beforeEach(() => {
  vi.clearAllMocks()
  subscriber = null
  h.isAuthenticated.mockReturnValue(true)
  h.getStoredUser.mockReturnValue({ email: 'asha@rvaiglobal.com', display_name: 'Asha' })
  h.fetchUsageToday.mockResolvedValue({ used: 12_345, limit: 50_000, remaining: 37_655 })
  h.onUsageChanged.mockImplementation((fn) => {
    subscriber = fn
    return () => {
      subscriber = null
    }
  })
})
afterEach(() => cleanup())

const renderNavbar = () =>
  render(
    <MemoryRouter>
      <Navbar />
    </MemoryRouter>,
  )

describe('the usage meter is visible on a narrow screen (N4)', () => {
  it('THE BUG: the meter is never hidden behind a breakpoint', async () => {
    // jsdom has no viewport-driven CSS, so the honest assertion is on the MECHANISM: a
    // `hidden md:flex` container is unreachable below 768px no matter what the media query
    // would do. Mutation-check: restore `hidden md:flex` and this goes red.
    renderNavbar()
    const meter = await screen.findByTestId('usage-meter')
    expect(meter.className).not.toMatch(/(^|\s)hidden(\s|$)/)
    expect(meter.className).toMatch(/(^|\s)flex(\s|$)/)
  })

  it('states the same fact at both widths — compact on small, full on md and up', async () => {
    renderNavbar()
    const meter = await screen.findByTestId('usage-meter')
    // Both readings are rendered; CSS picks one. The compact one must still be a real reading
    // of the same numbers, not a bare bar with no figures.
    expect(meter.textContent).toMatch(/12\.3K\s*\/\s*50K/)
    expect(meter.textContent).toMatch(/12,345 \/ 50,000 tokens/)
  })

  it('turns danger when the budget is spent, at either width', async () => {
    h.fetchUsageToday.mockResolvedValue({ used: 50_000, limit: 50_000, remaining: 0 })
    renderNavbar()
    const meter = await screen.findByTestId('usage-meter')
    expect(meter.querySelector('.text-danger')).not.toBeNull()
  })
})

describe('the meter settles without a reload (N4)', () => {
  it('subscribes to the usage-changed signal and refetches when it fires', async () => {
    renderNavbar()
    await waitFor(() => expect(h.fetchUsageToday).toHaveBeenCalledTimes(1))
    expect(subscriber).toBeTypeOf('function')

    h.fetchUsageToday.mockResolvedValue({ used: 20_000, limit: 50_000, remaining: 30_000 })
    subscriber()

    await waitFor(() => expect(screen.getByTestId('usage-meter').textContent).toMatch(/20,000 \/ 50,000/))
    expect(h.fetchUsageToday).toHaveBeenCalledTimes(2)
  })

  it('unsubscribes on unmount — a fired signal must not touch a dead component', async () => {
    const { unmount } = renderNavbar()
    await waitFor(() => expect(h.onUsageChanged).toHaveBeenCalled())
    unmount()
    expect(subscriber).toBeNull()
  })

  it('hides the meter entirely when the session is gone, rather than showing a stale budget', async () => {
    h.isAuthenticated.mockReturnValue(false)
    renderNavbar()
    await waitFor(() => expect(h.onUsageChanged).toHaveBeenCalled())
    expect(screen.queryByTestId('usage-meter')).toBeNull()
    expect(h.fetchUsageToday).not.toHaveBeenCalled()
  })
})
