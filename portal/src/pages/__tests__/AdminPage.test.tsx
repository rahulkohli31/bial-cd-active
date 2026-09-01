/**
 * U15 — the admin console's message channel, which had NO test at all before this file:
 * the panel tests (AppRegistryPanel.test.jsx etc.) only ever assert that a panel calls
 * `onToast`, never what AdminPage itself does with the call. This is that missing half.
 *
 * Before this unit, `AdminPage.showToast` took one string and rendered it inside one
 * white/blue-info card that always auto-dismissed after three seconds — the SAME
 * appearance whether `AppRegistryPanel.act()` had just sent a confirmation
 * (`"Gate Tool" approved`) or a raw failure (an approve that threw). An administrator
 * could not tell, without reading the words, whether the action they just took had
 * worked — "the surface where being wrong costs the most." The fix gives the channel a
 * `severity: 'ok' | 'problem'` that drives the icon, the colour, and whether the
 * dismiss timer runs at all; `AppRegistryPanel.act()`'s catch branch is the one call
 * site in the whole admin console that has ever needed 'problem'.
 *
 * `AppRegistryPanel` (the 'apps' tab, active by default) is rendered for REAL here, with
 * only its API module mocked — the fix under test is what AdminPage does with a callback
 * a real panel really invokes, not a synthetic one. The other three tabs' panels never
 * report a failure through this channel (their own catch blocks call `setActionError` /
 * `setApplyError` instead — see UsersLimitsPanel/GlobalLimitsPanel), so they are stubbed
 * out to keep this file's mock surface to what the fix actually touches.
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
vi.mock('../../components/layout/Navbar', () => ({ default: () => null }))
vi.mock('../../utils/appRegistryApi', () => h)
// Not this unit's concern (see file docblock) — kept as no-op stand-ins so AdminPage's
// static imports of them don't drag in their own, unrelated data-fetching.
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

describe('the admin toast channel — confirmation vs failure through the SAME callback (U15 integration)', () => {
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

describe('a failure waits to be dismissed; a confirmation may fade (U15)', () => {
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
      // 3s timer (the code as it existed before this unit) makes this go red.
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

describe('two messages in quick succession (U15 edge case)', () => {
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
