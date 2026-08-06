/**
 * SubmitControl (APPROVAL R12): the citizen's submit-for-review card. Status +
 * metadata render from the typed status read; clicking Submit opens the V4
 * data-classification modal rather than posting directly — the actual submit
 * (double-click-safe, every 409/422 rendering the server's own self-describing
 * copy) happens from the modal's Confirm.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, fireEvent, cleanup } from '@testing-library/react'
import SubmitControl from '../SubmitControl'
import { ApiError } from '../../utils/apiError'
import type { AppApprovalStatus, DataClassificationAnswers, SubmitResult } from '../../utils/approvalApi'

const h = vi.hoisted(() => ({
  getApprovalStatus: vi.fn(),
  submitForReview: vi.fn(),
}))

vi.mock('../../utils/approvalApi', async () => {
  const actual = await vi.importActual<typeof import('../../utils/approvalApi')>(
    '../../utils/approvalApi',
  )
  return {
    ...actual,
    getApprovalStatus: h.getApprovalStatus,
    submitForReview: h.submitForReview,
  }
})

const SHA = 'a1b2c3d4e5f6a7b8c9d0a1b2c3d4e5f6a7b8c9d0'

const LIVE_URL = 'https://apps.bial.example.com/gate-ops'

const CATEGORY_KEYS = [
  'credentialsSecrets',
  'healthData',
  'personalInformation',
  'financialData',
  'confidentialBusinessData',
  'publicData',
] as const

const allNoAnswers: DataClassificationAnswers = {
  credentialsSecrets: false,
  healthData: false,
  personalInformation: false,
  financialData: false,
  confidentialBusinessData: false,
  publicData: false,
  notes: null,
}

/** Opens the modal (if not already open) and answers all six categories No. */
async function openModalAndAnswerAllNo(): Promise<void> {
  fireEvent.click(screen.getByTestId('submit-for-review'))
  await screen.findByTestId('data-classification-modal')
  for (const key of CATEGORY_KEYS) {
    fireEvent.click(screen.getByTestId(`dc-question-${key}-no`))
  }
}

const makeStatus = (over: Partial<AppApprovalStatus> = {}): AppApprovalStatus => ({
  appId: 'app-1',
  status: 'draft',
  rejectionNote: null,
  submissionId: null,
  commitSha: null,
  submittedAt: null,
  deployedAt: null,
  deployedUrl: null,
  dataClassification: null,
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
  it('renders the status and, after Confirm, the metadata from the submit result (no re-fetch)', async () => {
    h.getApprovalStatus.mockResolvedValue(makeStatus())
    h.submitForReview.mockResolvedValue(submitted)
    render(<SubmitControl appId="app-1" />)

    expect((await screen.findByTestId('submit-status')).textContent).toContain('Not submitted')

    await openModalAndAnswerAllNo()
    fireEvent.click(screen.getByTestId('dc-confirm'))

    await waitFor(() => expect(h.submitForReview).toHaveBeenCalledTimes(1))
    expect(h.submitForReview).toHaveBeenCalledWith('app-1', allNoAnswers)
    expect((await screen.findByTestId('submit-status')).textContent).toContain('Pending admin review')
    // The modal closes on a successful submit.
    expect(screen.queryByTestId('data-classification-modal')).toBeNull()
    // The short SHA renders — the provenance the admin sees is visible to the citizen too.
    expect(screen.getByTestId('commit-sha').textContent).toContain(SHA.slice(0, 12))
    expect(screen.getByTestId('submitted-at')).toBeTruthy()
    // The metadata comes from the POST's OWN result — status is NOT re-fetched (so a
    // transient follow-up GET failure can't hide the submit's success).
    expect(h.getApprovalStatus).toHaveBeenCalledTimes(1)
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
  ])('renders the distinct 409 copy verbatim inside the modal: %s', async (message) => {
    h.getApprovalStatus.mockResolvedValue(makeStatus())
    h.submitForReview.mockRejectedValue(new ApiError(message, 409))
    render(<SubmitControl appId="app-1" />)

    await screen.findByTestId('submit-status')
    await openModalAndAnswerAllNo()
    fireEvent.click(screen.getByTestId('dc-confirm'))

    expect((await screen.findByRole('alert')).textContent).toContain(message)
    // A failed submit leaves the modal open with the answers intact.
    expect(screen.getByTestId('data-classification-modal')).toBeTruthy()
  })

  // --- Cancel is structural — see DataClassificationModal.test.tsx for the full
  // focus-trap/Escape/backdrop coverage; this pins the SubmitControl-level contract. --

  it('Cancel closes the modal and never calls submitForReview', async () => {
    h.getApprovalStatus.mockResolvedValue(makeStatus())
    render(<SubmitControl appId="app-1" />)
    await screen.findByTestId('submit-status')

    fireEvent.click(screen.getByTestId('submit-for-review'))
    await screen.findByTestId('data-classification-modal')
    fireEvent.click(screen.getByTestId('dc-cancel'))

    expect(screen.queryByTestId('data-classification-modal')).toBeNull()
    expect(h.submitForReview).not.toHaveBeenCalled()
  })

  it('Confirm stays disabled until all six questions are answered', async () => {
    h.getApprovalStatus.mockResolvedValue(makeStatus())
    render(<SubmitControl appId="app-1" />)
    await screen.findByTestId('submit-status')

    fireEvent.click(screen.getByTestId('submit-for-review'))
    await screen.findByTestId('data-classification-modal')
    expect((screen.getByTestId('dc-confirm') as HTMLButtonElement).disabled).toBe(true)

    // Answer five of six — still disabled.
    for (const key of CATEGORY_KEYS.slice(0, 5)) {
      fireEvent.click(screen.getByTestId(`dc-question-${key}-no`))
    }
    expect((screen.getByTestId('dc-confirm') as HTMLButtonElement).disabled).toBe(true)

    fireEvent.click(screen.getByTestId(`dc-question-${CATEGORY_KEYS[5]}-no`))
    expect((screen.getByTestId('dc-confirm') as HTMLButtonElement).disabled).toBe(false)
    expect(h.submitForReview).not.toHaveBeenCalled()
  })

  it('requires an explanation once the weighted total reaches the soft-gate threshold', async () => {
    h.getApprovalStatus.mockResolvedValue(makeStatus())
    render(<SubmitControl appId="app-1" />)
    await screen.findByTestId('submit-status')

    await openModalAndAnswerAllNo()
    expect((screen.getByTestId('dc-confirm') as HTMLButtonElement).disabled).toBe(false)

    // Flip Credentials/Secrets to Yes (weight 40) — crosses the threshold alone.
    fireEvent.click(screen.getByTestId('dc-question-credentialsSecrets-yes'))
    expect((screen.getByTestId('dc-confirm') as HTMLButtonElement).disabled).toBe(true)

    fireEvent.change(screen.getByTestId('dc-notes'), {
      target: { value: 'Stored in a managed vault, never logged.' },
    })
    expect((screen.getByTestId('dc-confirm') as HTMLButtonElement).disabled).toBe(false)
  })

  // --- "Your app is live" (R5) ------------------------------------------------

  it('renders the Live link when a deployed URL is recorded, replacing the footer copy', async () => {
    h.getApprovalStatus.mockResolvedValue(
      makeStatus({ status: 'approved', deployedUrl: LIVE_URL, deployedAt: '2026-07-16T12:00:00Z' }),
    )
    render(<SubmitControl appId="app-1" />)

    const link = (await screen.findByTestId('live-link')).querySelector('a')
    expect(link?.getAttribute('href')).toBe(LIVE_URL)
    expect(link?.getAttribute('target')).toBe('_blank')
    // The deployed app is a foreign origin: no window.opener, no referrer leak.
    expect(link?.getAttribute('rel')).toBe('noopener noreferrer')
    expect(screen.getByTestId('submit-control').textContent).toContain('Your app is live')
    expect(screen.getByTestId('submit-control').textContent).not.toContain(
      'An approved app is deployed by the platform team',
    )
  })

  it('shows no Live link when the app is approved but has no recorded URL', async () => {
    // The link is gated on the URL, NOT on `status === 'approved'` or `deployedAt`:
    // an admin can record a deploy without an address, and a dead link is worse than none.
    h.getApprovalStatus.mockResolvedValue(
      makeStatus({ status: 'approved', deployedAt: '2026-07-16T12:00:00Z', deployedUrl: null }),
    )
    render(<SubmitControl appId="app-1" />)

    await screen.findByTestId('submit-status')
    expect(screen.queryByTestId('live-link')).toBeNull()
    expect(screen.getByTestId('submit-control').textContent).toContain(
      'An approved app is deployed by the platform team',
    )
  })

  it('keeps the Live link after submitting an update (a submit does not undeploy)', async () => {
    // The submit result carries no deploy marker; dropping it would blink the owner's
    // live link out on every update, implying the running app had gone away.
    h.getApprovalStatus.mockResolvedValue(
      makeStatus({ status: 'approved', deployedUrl: LIVE_URL, deployedAt: '2026-07-16T12:00:00Z' }),
    )
    h.submitForReview.mockResolvedValue(submitted)
    render(<SubmitControl appId="app-1" />)
    await screen.findByTestId('live-link')

    await openModalAndAnswerAllNo()
    fireEvent.click(screen.getByTestId('dc-confirm'))

    await waitFor(() =>
      expect(screen.getByTestId('submit-status').textContent).toContain('Pending admin review'),
    )
    expect(screen.getByTestId('live-link').querySelector('a')?.getAttribute('href')).toBe(LIVE_URL)
  })

  it('renders a load failure as an alert, not a broken card', async () => {
    h.getApprovalStatus.mockRejectedValue(new ApiError('Failed to read the app status', 503))
    render(<SubmitControl appId="app-1" />)
    expect((await screen.findByRole('alert')).textContent).toContain('Failed to read the app status')
  })

  it('disables Confirm while a submit is in flight (no double-submit)', async () => {
    h.getApprovalStatus.mockResolvedValue(makeStatus())
    let release: (value: SubmitResult) => void = () => {}
    h.submitForReview.mockImplementation(
      () => new Promise<SubmitResult>((resolve) => { release = resolve }),
    )
    render(<SubmitControl appId="app-1" />)
    await screen.findByTestId('submit-status')

    await openModalAndAnswerAllNo()
    const confirmButton = screen.getByTestId('dc-confirm') as HTMLButtonElement
    fireEvent.click(confirmButton)
    await waitFor(() => expect(confirmButton.disabled).toBe(true))
    fireEvent.click(confirmButton) // a second click while busy must not double-post
    expect(h.submitForReview).toHaveBeenCalledTimes(1)

    release(submitted)
    await waitFor(() => expect(screen.queryByTestId('data-classification-modal')).toBeNull())
  })
})
