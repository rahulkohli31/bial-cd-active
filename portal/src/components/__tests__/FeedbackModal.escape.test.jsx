/**
 * ESCAPE DISMISSES THE FEEDBACK MODAL — and nothing else in the product asserted it.
 *
 * `FeedbackModal` is hand-rolled (`fixed inset-0`, `role="dialog"`, its own overlay) and its
 * ONLY key handler is a Tab focus trap. The Escape that dismisses it lives in `Navbar.tsx`, in
 * a document `keydown` effect that used to close two things on one line — the avatar menu and
 * this modal. Swapping the menu onto Radix hands menu-Escape to `DismissableLayer` and makes
 * that whole effect look redundant; it is not, and this file is the only thing standing between
 * "the menu tests are all green" and a shipped behaviour vanishing without trace.
 *
 * WHY IT MOUNTS THE NAVBAR RATHER THAN THE MODAL. The handler is not in the modal, so a test
 * that rendered `FeedbackModal` alone would pass no matter what `Navbar.tsx` did. And it cannot
 * live in `Navbar.test.jsx`, which stubs `FeedbackModal` to `null` throughout.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, cleanup, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

const h = vi.hoisted(() => ({
  fetchUsageToday: vi.fn(),
  onUsageChanged: vi.fn(),
  isAuthenticated: vi.fn(() => true),
  getStoredUser: vi.fn(),
  logout: vi.fn(),
  fetchAppStatusCounts: vi.fn(),
  submitFeedback: vi.fn(),
}))

vi.mock('../../utils/usage', () => ({
  fetchUsageToday: h.fetchUsageToday,
  onUsageChanged: h.onUsageChanged,
}))
vi.mock('../../utils/auth', () => ({
  isAuthenticated: h.isAuthenticated,
  getStoredUser: h.getStoredUser,
  logout: h.logout,
}))
vi.mock('../../utils/attachmentApi', () => ({ revokeAllAttachmentUrls: vi.fn() }))
vi.mock('../../utils/appRegistryApi', () => ({ fetchAppStatusCounts: h.fetchAppStatusCounts }))
// Deliberately NOT mocked: `../FeedbackModal` is the whole subject.
vi.mock('../../utils/feedback', () => ({ submitFeedback: h.submitFeedback }))

import Navbar from '../layout/Navbar'

beforeEach(() => {
  vi.clearAllMocks()
  h.isAuthenticated.mockReturnValue(true)
  h.getStoredUser.mockReturnValue({ email: 'asha@rvaiglobal.com', display_name: 'Asha' })
  h.fetchUsageToday.mockResolvedValue({ used: 12_345, limit: 50_000, remaining: 37_655 })
  h.fetchAppStatusCounts.mockResolvedValue({ draft: 0, pending: 0, approved: 0, rejected: 0, disabled: 0 })
  h.onUsageChanged.mockImplementation(() => () => {})
})
afterEach(() => cleanup())

const feedbackDialog = () => screen.queryByRole('dialog', { name: 'Send feedback' })

const openFeedback = async () => {
  render(
    <MemoryRouter>
      <Navbar />
    </MemoryRouter>,
  )
  await waitFor(() => expect(h.fetchUsageToday).toHaveBeenCalled())
  fireEvent.click(screen.getByRole('button', { name: 'Feedback' }))
  expect(feedbackDialog()).not.toBeNull()
}

describe('the feedback modal is dismissed by Escape', () => {
  it('★ Escape closes it — the half of the Navbar keydown handler that survives the Radix menu', async () => {
    await openFeedback()

    fireEvent.keyDown(document, { key: 'Escape' })

    await waitFor(() => expect(feedbackDialog()).toBeNull())
    // Liveness: the navbar is still mounted, so the absence above is the modal closing and
    // not a component that crashed on the keypress — an assert-absence test with no liveness
    // partner goes green under an error boundary.
    expect(screen.getByRole('button', { name: 'Feedback' })).toBeTruthy()
  })

  it('leaves it open on any other key, so the dismissal is Escape and not every keypress', async () => {
    await openFeedback()

    fireEvent.keyDown(document, { key: 'a' })
    fireEvent.keyDown(document, { key: 'Enter' })

    expect(feedbackDialog()).not.toBeNull()
  })
})
