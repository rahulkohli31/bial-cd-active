/**
 * THE ADMIN CONSOLE'S TOAST CHANNEL. `showToast`'s `severity` ('ok' | 'problem') drives the icon,
 * the colour, and whether the dismiss timer runs — without that, an administrator can't tell a
 * confirmation from a raw failure without reading the words, on the surface where being wrong
 * costs the most.
 *
 * `AppRegistryPanel` (the 'apps' tab, default) renders for REAL here, only its API module mocked —
 * this tests what AdminPage does with a callback a real panel actually invokes, not a synthetic
 * one. The other tabs are stubbed (see below for why).
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
  deleteApp: vi.fn(),
  fetchHistory: vi.fn(),
}))

vi.mock('../../utils/auth', () => ({ getStoredUser: h.getStoredUser }))
vi.mock('../../utils/appRegistryApi', () => h)
// Stubbed rather than exercised: they report their failures through their own state, not this
// channel, and their real modules would pull unrelated data-fetching into this file.
vi.mock('../../components/admin/UsersLimitsPanel', () => ({ default: () => null }))
vi.mock('../../components/admin/GlobalLimitsPanel', () => ({ default: () => null }))
vi.mock('../../components/admin/FeedbackPanel', () => ({ default: () => null }))
// Its toasts are covered by its own suite; here it only has to prove the tab opens it.
vi.mock('../../components/admin/ClassificationPanel', () => ({
  default: () => <p>Classification settings</p>,
}))

import AdminPage from '../AdminPage'

const ADMIN = { isAdmin: true }

const PENDING = {
  appId: 'app-1',
  name: 'Gate Tool',
  ownerUsername: 'alice',
  status: 'pending',
  registryStatus: 'waiting_for_review',
  liveVersion: null,
  loginRequired: false,
  hasApprovedSnapshot: false,
  submissionId: 'sub-1',
  commitSha: 'f0e1d2c3b4a5f0e1d2c3b4a5f0e1d2c3b4a5f0e1',
  submittedAt: '2026-07-16T09:00:00Z',
  declaration: null,
}

const DRAFT = { ...PENDING, appId: 'app-4', name: 'Draft Tool', status: 'draft', registryStatus: 'draft', submissionId: null }

beforeEach(() => {
  vi.clearAllMocks()
  h.getStoredUser.mockReturnValue(ADMIN)
  h.listApps.mockResolvedValue({ apps: [PENDING, DRAFT], truncated: false })
  h.fetchHistory.mockResolvedValue({ entries: [], live: null, liveUrl: null, truncated: false })
})
afterEach(() => cleanup())

const renderAdmin = () =>
  render(
    <MemoryRouter>
      <AdminPage />
    </MemoryRouter>,
  )

/** Open the one pending row's panel and press Approve: a success reports through `onToast`. */
const openReviewAndApprove = () => {
  fireEvent.click(screen.getByRole('button', { name: 'Gate Tool' }))
  fireEvent.click(screen.getByTestId('approve-btn'))
}

/** Disable the draft row from its ⋯ menu: a row action, which reports both outcomes through
 *  `onToast`. A failed Approve is said inside the panel instead. */
const disableDraft = () => {
  fireEvent.pointerDown(screen.getByTestId('actions-app-4'))
  fireEvent.click(screen.getByRole('menuitem', { name: 'Disable' }))
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
    h.disableApp.mockRejectedValue(new Error('Could not reach the registry.'))
    renderAdmin()
    await screen.findByText('Draft Tool')

    disableDraft()

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
    h.disableApp.mockRejectedValue(new Error('Could not reach the registry.'))
    vi.useFakeTimers()
    try {
      renderAdmin()
      await vi.advanceTimersByTimeAsync(0)
      expect(screen.getByText('Draft Tool')).toBeTruthy()

      disableDraft()
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
    h.disableApp.mockRejectedValueOnce(new Error('Could not reach the registry.'))
    renderAdmin()
    await screen.findByText('Draft Tool')
    disableDraft()
    const problemToast = await screen.findByTestId('admin-toast')
    expect(problemToast.dataset.severity).toBe('problem')
    const problemClass = problemToast.className

    h.disableApp.mockResolvedValueOnce({})
    disableDraft()

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
    // Fail, then immediately retry and succeed — a failed and a successful `act()` each call
    // `showToast` exactly once, and `showToast` replaces the whole `{ text, severity }` pair in a
    // single `setState`, never the two halves separately — so there is no render where the
    // SECOND message's text sits under the FIRST message's styling (or vice versa).
    h.disableApp.mockRejectedValueOnce(new Error('Could not reach the registry.'))
    renderAdmin()
    await screen.findByText('Draft Tool')
    disableDraft()
    const failedToast = await screen.findByTestId('admin-toast')
    expect(failedToast.dataset.severity).toBe('problem')

    h.disableApp.mockResolvedValueOnce({})
    disableDraft()

    await waitFor(() => {
      const toast = screen.getByTestId('admin-toast')
      expect(toast.dataset.severity).toBe('ok')
      expect(toast.textContent).toContain('disabled')
      expect(toast.textContent).not.toContain('Could not reach the registry.')
    })
  })
})

describe('the console tabs', () => {
  it('has no Integrations tab, and no people-waiting badge', async () => {
    renderAdmin()
    await screen.findByText('Gate Tool')
    expect(screen.getByRole('button', { name: 'App Registry' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Users & Limits' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /Integrations/ })).toBeNull()
    expect(screen.queryByTestId('waiting-count-integrations-tab')).toBeNull()
  })

  it('ends the tab row with Deployment Classification, which opens its panel', async () => {
    renderAdmin()
    await screen.findByText('Gate Tool')

    const tabs = ['App Registry', 'Users & Limits', 'Global Limits', 'Feedback', 'Deployment Classification']
    const row = screen.getByRole('button', { name: 'App Registry' }).parentElement
    expect([...(row?.children ?? [])].map((tab) => tab.textContent)).toEqual(tabs)

    fireEvent.click(screen.getByRole('button', { name: 'Deployment Classification' }))
    expect(screen.getByText('Classification settings')).toBeTruthy()
  })
})
