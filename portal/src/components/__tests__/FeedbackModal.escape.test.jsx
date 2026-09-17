/**
 * ESCAPE DISMISSES THE FEEDBACK MODAL — and nothing else in the product asserted it.
 *
 * `FeedbackModal` is hand-rolled (`fixed inset-0`, `role="dialog"`, its own overlay) and its
 * ONLY key handler is a Tab focus trap. The Escape that dismisses it lives in `ProfileCluster`,
 * in a document `keydown` effect that closes the modal without going through the Radix dropdown
 * that opens it — Radix owns Escape for its own menu; it owns nothing for the modal.
 *
 * WHY IT MOUNTS `ProfileCluster` RATHER THAN THE MODAL. The handler is not in the modal, so a
 * test that rendered `FeedbackModal` alone would pass no matter what `ProfileCluster` did.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, cleanup, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

const h = vi.hoisted(() => ({
  getStoredUser: vi.fn(),
  logout: vi.fn(),
}))

vi.mock('../../utils/auth', () => ({
  getStoredUser: h.getStoredUser,
  logout: h.logout,
}))
// Deliberately NOT mocked: `../FeedbackModal` is the whole subject.

import ProfileCluster from '../layout/ProfileCluster'

beforeEach(() => {
  vi.clearAllMocks()
  h.getStoredUser.mockReturnValue({ email: 'asha@rvaiglobal.com', display_name: 'Asha' })
})
afterEach(() => cleanup())

const feedbackDialog = () => screen.queryByRole('dialog', { name: 'Send feedback' })

const openFeedback = async () => {
  render(
    <MemoryRouter>
      <ProfileCluster />
    </MemoryRouter>,
  )
  // The Radix trigger opens on POINTERDOWN — a plain click does nothing.
  fireEvent.pointerDown(screen.getByTestId('profile-cluster'))
  fireEvent.click(await screen.findByRole('menuitem', { name: 'Feedback' }))
  expect(feedbackDialog()).not.toBeNull()
}

describe('the feedback modal is dismissed by Escape', () => {
  it('★ Escape closes it — the half of the keydown handler that survives the Radix menu', async () => {
    await openFeedback()

    fireEvent.keyDown(document, { key: 'Escape' })

    await waitFor(() => expect(feedbackDialog()).toBeNull())
    // Liveness: the profile cluster is still mounted, so the absence above is the modal closing
    // and not a component that crashed on the keypress — an assert-absence test with no liveness
    // partner goes green under an error boundary.
    expect(screen.getByTestId('profile-cluster')).toBeTruthy()
  })

  it('leaves it open on any other key, so the dismissal is Escape and not every keypress', async () => {
    await openFeedback()

    fireEvent.keyDown(document, { key: 'a' })
    fireEvent.keyDown(document, { key: 'Enter' })

    expect(feedbackDialog()).not.toBeNull()
  })
})
