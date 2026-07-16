/**
 * SubmitControl (APPROVAL R12): the citizen's submit-for-review card. Status +
 * metadata render from the typed status read; a submit posts once, is
 * double-click-safe, and every 409 renders the server's own self-describing copy
 * (build session running / nothing to submit / illegal state are DISTINCT).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, fireEvent, cleanup } from '@testing-library/react'
import SubmitControl from '../SubmitControl'
import { ApiError } from '../../utils/apiError'
import type { AppApprovalStatus, SubmitResult } from '../../utils/approvalApi'

const h = vi.hoisted(() => ({
  getApprovalStatus: vi.fn(),
  submitForReview: vi.fn(),
}))

vi.mock('../../utils/approvalApi', () => ({
  getApprovalStatus: h.getApprovalStatus,
  submitForReview: h.submitForReview,
}))

const SHA = 'a1b2c3d4e5f6a7b8c9d0a1b2c3d4e5f6a7b8c9d0'

const makeStatus = (over: Partial<AppApprovalStatus> = {}): AppApprovalStatus => ({
  appId: 'app-1',
  status: 'draft',
  rejectionNote: null,
  submissionId: null,
  commitSha: null,
  submittedAt: null,
  ...over,
})

const submitted: SubmitResult = {
  appId: 'app-1',
  status: 'pending',
  submissionId: 'sub-1',
  commitSha: SHA,
  submittedAt: '2026-07-16T10:00:00Z',
}

afterEach(cleanup)
beforeEach(() => {
  vi.clearAllMocks()
})

describe('SubmitControl', () => {
  it('renders the status and, after submit, the returned metadata', async () => {
    h.getApprovalStatus
      .mockResolvedValueOnce(makeStatus())
      .mockResolvedValueOnce(
        makeStatus({ status: 'pending', submissionId: 'sub-1', commitSha: SHA, submittedAt: submitted.submittedAt }),
      )
    h.submitForReview.mockResolvedValue(submitted)
    render(<SubmitControl appId="app-1" />)

    expect((await screen.findByTestId('submit-status')).textContent).toContain('Not submitted')

    fireEvent.click(screen.getByTestId('submit-for-review'))

    await waitFor(() => expect(h.submitForReview).toHaveBeenCalledTimes(1))
    expect(h.submitForReview).toHaveBeenCalledWith('app-1')
    expect((await screen.findByTestId('submit-status')).textContent).toContain('Pending admin review')
    // The short SHA renders — the provenance the admin sees is visible to the citizen too.
    expect(screen.getByTestId('commit-sha').textContent).toContain(SHA.slice(0, 12))
    expect(screen.getByTestId('submitted-at')).toBeTruthy()
  })

  it('shows the rejection note for a rejected app', async () => {
    h.getApprovalStatus.mockResolvedValue(
      makeStatus({ status: 'rejected', rejectionNote: 'Remove the sample data.' }),
    )
    render(<SubmitControl appId="app-1" />)
    expect((await screen.findByTestId('rejection-note')).textContent).toContain('Remove the sample data.')
    expect(screen.getByTestId('submit-status').textContent).toContain('Changes requested')
  })

  it.each([
    'A build session is still running — end it before submitting.',
    'Nothing to submit — generate an app first.',
    'This app cannot be submitted in its current state.',
  ])('renders the distinct 409 copy verbatim: %s', async (message) => {
    h.getApprovalStatus.mockResolvedValue(makeStatus())
    h.submitForReview.mockRejectedValue(new ApiError(message, 409))
    render(<SubmitControl appId="app-1" />)

    await screen.findByTestId('submit-status')
    fireEvent.click(screen.getByTestId('submit-for-review'))

    expect((await screen.findByRole('alert')).textContent).toContain(message)
  })

  it('renders a load failure as an alert, not a broken card', async () => {
    h.getApprovalStatus.mockRejectedValue(new ApiError('Failed to read the app status', 503))
    render(<SubmitControl appId="app-1" />)
    expect((await screen.findByRole('alert')).textContent).toContain('Failed to read the app status')
  })

  it('disables the button while a submit is in flight (no double-submit)', async () => {
    h.getApprovalStatus.mockResolvedValue(makeStatus())
    let release: (value: SubmitResult) => void = () => {}
    h.submitForReview.mockImplementation(
      () => new Promise<SubmitResult>((resolve) => { release = resolve }),
    )
    render(<SubmitControl appId="app-1" />)
    await screen.findByTestId('submit-status')

    const button = screen.getByTestId('submit-for-review') as HTMLButtonElement
    fireEvent.click(button)
    await waitFor(() => expect(button.disabled).toBe(true))
    fireEvent.click(button) // a second click while busy must not double-post
    expect(h.submitForReview).toHaveBeenCalledTimes(1)

    release(submitted)
    await waitFor(() => expect(button.disabled).toBe(false))
  })
})
