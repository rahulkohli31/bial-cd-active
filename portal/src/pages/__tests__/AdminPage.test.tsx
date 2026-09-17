/**
 * THE ADMIN CONSOLE'S TOAST CHANNEL. `showToast`'s `severity` ('ok' | 'problem') drives the icon,
 * the colour, and whether the dismiss timer runs — without that, an administrator can't tell a
 * confirmation from a raw failure without reading the words, on the surface where being wrong
 * costs the most.
 *
 * `AppRegistryPanel` (the 'apps' tab, default) renders for REAL here, only its API module mocked —
 * this tests what AdminPage does with a callback a real panel actually invokes, not a synthetic
 * one. The other three tabs are stubbed (see below for why).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

const h = vi.hoisted(() => ({
  getStoredUser: vi.fn(),
  listApps: vi.fn(),
  approveApp: vi.fn(),
  rejectApp: vi.fn(),
  patchApp: vi.fn(),
  disableApp: vi.fn(),
  enableApp: vi.fn(),
  markDeployed: vi.fn(),
  deleteApp: vi.fn(),
  fetchAudit: vi.fn(),
  fetchAppStatusCounts: vi.fn(),
}))

vi.mock('../../utils/auth', () => ({ getStoredUser: h.getStoredUser }))
vi.mock('../../utils/appRegistryApi', () => h)
// Stubbed rather than exercised: they report their failures through their own state, not this
// channel, and their real modules would pull unrelated data-fetching into this file.
vi.mock('../../components/admin/UsersLimitsPanel', () => ({ default: () => null }))
vi.mock('../../components/admin/GlobalLimitsPanel', () => ({ default: () => null }))
vi.mock('../../components/admin/FeedbackPanel', () => ({ default: () => null }))

import AdminPage from '../AdminPage'

const ADMIN = { isAdmin: true }

const PENDING = {
  appId: 'app-1',
  name: 'Gate Tool',
  ownerUsername: 'alice',
  status: 'pending',
  loginRequired: false,
  hasApprovedSnapshot: false,
  submissionId: 'sub-1',
  commitSha: 'f0e1d2c3b4a5f0e1d2c3b4a5f0e1d2c3b4a5f0e1',
  submittedAt: '2026-07-16T09:00:00Z',
  redeployNeeded: false,
  approvalRoute: 'self_publish',
  declaration: null,
}

beforeEach(() => {
  vi.clearAllMocks()
  h.getStoredUser.mockReturnValue(ADMIN)
  h.listApps.mockResolvedValue([PENDING])
  h.fetchAppStatusCounts.mockResolvedValue({ draft: 0, pending: 1, approved: 0, rejected: 0, disabled: 0 })
})
afterEach(() => cleanup())

const renderAdmin = () =>
  render(
    <MemoryRouter>
      <AdminPage />
    </MemoryRouter>,
  )

/** Open the one pending row's review modal and press Approve — the exact path
 *  `AppRegistryPanel.act()` reports back through `onToast`. */
const openReviewAndApprove = () => {
  fireEvent.click(screen.getByTestId('review-app-1'))
  fireEvent.click(screen.getByTestId('approve-btn'))
}

describe('the admin toast channel — confirmation vs failure through the SAME callback', () => {
  it('an action that succeeds renders the confirmation appearance', async () => {
    h.approveApp.mockResolvedValue({})
    renderAdmin()
    await screen.findByText('Gate Tool')

    openReviewAndApprove()

    const toast = await screen.findByTestId('admin-toast')
    expect(toast.dataset.severity).toBe('ok')
    expect(toast.textContent).toContain('approved')
  })

  it('an action that throws renders the failure appearance, through the exact same channel', async () => {
    h.approveApp.mockRejectedValue(new Error('Could not reach the registry.'))
    renderAdmin()
    await screen.findByText('Gate Tool')

    openReviewAndApprove()

    const toast = await screen.findByTestId('admin-toast')
    expect(toast.dataset.severity).toBe('problem')
    expect(toast.textContent).toContain('Could not reach the registry.')
  })
})

describe('a failure waits to be dismissed; a confirmation may fade', () => {
  it('a confirmation auto-dismisses after 3 seconds', async () => {
    h.approveApp.mockResolvedValue({})
    vi.useFakeTimers()
    try {
      renderAdmin()
      await vi.advanceTimersByTimeAsync(0)
      expect(screen.getByText('Gate Tool')).toBeTruthy()

      openReviewAndApprove()
      await vi.advanceTimersByTimeAsync(0)
      expect(screen.getByTestId('admin-toast').dataset.severity).toBe('ok')

      await vi.advanceTimersByTimeAsync(3000)
      expect(screen.queryByTestId('admin-toast')).toBeNull()
    } finally {
      vi.useRealTimers()
    }
  })

  it('a failure never auto-dismisses, well past the window a confirmation fades on', async () => {
    h.approveApp.mockRejectedValue(new Error('Could not reach the registry.'))
    vi.useFakeTimers()
    try {
      renderAdmin()
      await vi.advanceTimersByTimeAsync(0)
      expect(screen.getByText('Gate Tool')).toBeTruthy()

      openReviewAndApprove()
      await vi.advanceTimersByTimeAsync(0)
      expect(screen.getByTestId('admin-toast').dataset.severity).toBe('problem')

      // The mistake THIS test exists to catch: reverting `showToast` to always start a
      // 3s timer makes this go red.
      await vi.advanceTimersByTimeAsync(10_000)
      expect(screen.getByTestId('admin-toast')).toBeTruthy()
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('a confirmation and a failure are visually distinguishable without reading the text', () => {
  it('carry different severity markers, not merely different words', async () => {
    // Fail first: `act()` leaves the review modal OPEN on a non-withdrawal failure (the
    // admin still needs the submission metadata), so `approve-btn` is still on screen —
    // no second `review-app-1` click needed to retry the SAME action.
    h.approveApp.mockRejectedValueOnce(new Error('Could not reach the registry.'))
    renderAdmin()
    await screen.findByText('Gate Tool')
    openReviewAndApprove()
    const problemToast = await screen.findByTestId('admin-toast')
    expect(problemToast.dataset.severity).toBe('problem')
    const problemClass = problemToast.className

    h.approveApp.mockResolvedValueOnce({})
    fireEvent.click(screen.getByTestId('approve-btn'))

    const okToast = await waitFor(() => {
      const toast = screen.getByTestId('admin-toast')
      expect(toast.dataset.severity).toBe('ok')
      return toast
    })
    expect(okToast.className).not.toBe(problemClass)
  })
})

describe('two messages in quick succession', () => {
  it('the second message never leaves the first one’s text under the second’s styling', async () => {
    // Fail, then immediately retry and succeed — `act()`'s catch and success branches
    // each call `showToast` exactly once, and `showToast` replaces the whole
    // `{ text, severity }` pair in a single `setState`, never the two halves separately —
    // so there is no render where the SECOND message's text sits under the FIRST
    // message's styling (or vice versa).
    h.approveApp.mockRejectedValueOnce(new Error('Could not reach the registry.'))
    renderAdmin()
    await screen.findByText('Gate Tool')
    openReviewAndApprove()
    const failedToast = await screen.findByTestId('admin-toast')
    expect(failedToast.dataset.severity).toBe('problem')

    h.approveApp.mockResolvedValueOnce({})
    fireEvent.click(screen.getByTestId('approve-btn'))

    await waitFor(() => {
      const toast = screen.getByTestId('admin-toast')
      expect(toast.dataset.severity).toBe('ok')
      expect(toast.textContent).toContain('approved')
      expect(toast.textContent).not.toContain('Could not reach the registry.')
    })
  })
})
