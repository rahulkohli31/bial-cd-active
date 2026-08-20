import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, waitFor, fireEvent } from '@testing-library/react'
import AppRegistryPanel from '../AppRegistryPanel.jsx'

const h = vi.hoisted(() => ({
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
vi.mock('../../../utils/appRegistryApi', () => h)

import { ApiError } from '../../../utils/apiError'

const SHA = 'f0e1d2c3b4a5f0e1d2c3b4a5f0e1d2c3b4a5f0e1'
const OLDER_SHA = '9a8b7c6d5e4f9a8b7c6d5e4f9a8b7c6d5e4f9a8b'

const PENDING = {
  appId: 'app-1',
  name: 'Gate Tool',
  ownerUsername: 'alice',
  status: 'pending',
  loginRequired: false,
  hasApprovedSnapshot: false,
  submissionId: 'sub-1',
  commitSha: SHA,
  submittedAt: '2026-07-16T09:00:00Z',
  redeployNeeded: false,
  approvalRoute: 'self_publish',
  declaration: null,
}

const APPROVED = {
  ...PENDING,
  appId: 'app-2',
  name: 'Live Tool',
  status: 'approved',
  hasApprovedSnapshot: true,
  redeployNeeded: true,
  // The runbook lineage is what still HAS a go-live step; the self-publish assertions
  // below override this deliberately.
  approvalRoute: 'runbook',
}

/**
 * A declaration in the shape the publish gate writes
 * (`backend/src/api/v1/deploy/router.py::_declaration`) — snake_case keys inside the
 * document, camelCase inside its sub-objects, exactly as stored.
 */
const declaration = ({
  shipping = SHA,
  reviewed = SHA,
  citizen = {},
  reviewAnswers = {},
  reasons = {},
  merged = {},
  differences = {},
  explanation = 'The form only stores a staff name and a badge number, both kept in the app’s own database.',
} = {}) => ({
  commits: { shipping, reviewed },
  citizen: { answers: citizen, explanation },
  review: {
    available: reviewed !== null,
    complete: true,
    status: 'complete',
    failureCode: null,
    source: 'review',
    answers: reviewAnswers,
    reasons,
    scan: { tierAHit: false, tierBHit: false, incomplete: false, tierADispute: false },
  },
  merged: { answers: merged, anyWeightedYes: Object.values(merged).some(Boolean) },
  differences,
})

const ALL_NO = {
  credentials_secrets: false,
  health_data: false,
  personal_information: false,
  financial_data: false,
  confidential_business_data: false,
  public_data: false,
}

const CATEGORY_KEYS = Object.keys(ALL_NO)

afterEach(cleanup)
beforeEach(() => {
  for (const fn of Object.values(h)) fn.mockReset()
  h.listApps.mockResolvedValue([PENDING])
  h.fetchAppStatusCounts.mockResolvedValue({
    draft: 0, pending: 1, approved: 0, rejected: 0, disabled: 0,
  })
})

/** Open the review modal for the one pending row. */
const openReview = async () => {
  await screen.findByText('Gate Tool')
  fireEvent.click(screen.getByTestId('review-app-1'))
}

describe('AppRegistryPanel — registry vocabulary + actions', () => {
  it('loads the pending list and renders the registry status sub-tabs (not the mock vocabulary)', async () => {
    render(<AppRegistryPanel onToast={() => {}} />)
    await screen.findByText('Gate Tool')
    expect(h.listApps).toHaveBeenCalledWith('pending')
    // registry sub-tabs exist; the mock "Security Flags"/"under_review" vocabulary does not
    expect(screen.getByTestId('apps-tab-approved')).toBeTruthy()
    expect(screen.getByTestId('apps-tab-disabled')).toBeTruthy()
    expect(screen.queryByText('Security Flags')).toBeNull()
    expect(screen.getAllByText('Pending Review').length).toBeGreaterThan(0) // tab + badge
  })

  it('the review modal shows submission METADATA (SHA, submitted-at, submission id)', async () => {
    render(<AppRegistryPanel onToast={() => {}} />)
    await screen.findByText('Gate Tool')
    fireEvent.click(screen.getByTestId('review-app-1'))
    expect(screen.getByTestId('review-commit-sha').textContent).toContain(SHA.slice(0, 12))
    expect(screen.getByTestId('review-submission-id').textContent).toContain('sub-1')
    expect(screen.getByTestId('review-submitted-at').textContent).not.toContain('—')

    // A MISSING submitted-at reads as missing, never as the epoch. Folding null into
    // `new Date(0)` rendered "1/1/1970" directly above the Approve button, which an
    // administrator reads as a fact about the submission rather than as absent data.
    cleanup()
    h.listApps.mockResolvedValue([{ ...PENDING, submittedAt: null }])
    render(<AppRegistryPanel onToast={() => {}} />)
    await screen.findByText('Gate Tool')
    fireEvent.click(screen.getByTestId('review-app-1'))
    const when = screen.getByTestId('review-submitted-at').textContent ?? ''
    expect(when).toBe('—')
    expect(when).not.toMatch(/1970/)
    // The false JSX-era claims are gone: no "pre-compiles" copy, no /apps/{id} link.
    expect(document.body.textContent).not.toMatch(/pre-compiles/i)
    expect(document.querySelector('a[href^="/apps/"]')).toBeNull()
    // The dead bundle-download control (#118) is gone too — button and instruction both.
    expect(screen.queryByTestId('download-bundle')).toBeNull()
    expect(document.body.textContent).not.toMatch(/download the submitted bundle/i)
  })

  it('Review → Approve sends the DISPLAYED submission id (the reviewed-id guard input) and reloads', async () => {
    h.approveApp.mockResolvedValue({ status: 'approved' })
    render(<AppRegistryPanel onToast={() => {}} />)
    await screen.findByText('Gate Tool')
    fireEvent.click(screen.getByTestId('review-app-1'))
    fireEvent.click(screen.getByTestId('approve-btn'))
    await waitFor(() => expect(h.approveApp).toHaveBeenCalledWith('app-1', 'sub-1'))
    await waitFor(() => expect(h.listApps).toHaveBeenCalledTimes(2)) // initial + reload
  })

  it('an approve 409 surfaces the re-submitted-since-review copy, not a generic failure', async () => {
    const copy = 'This app was re-submitted since you reviewed it — please re-review.'
    h.approveApp.mockRejectedValue(new Error(copy))
    const onToast = vi.fn()
    render(<AppRegistryPanel onToast={onToast} />)
    await screen.findByText('Gate Tool')
    fireEvent.click(screen.getByTestId('review-app-1'))
    fireEvent.click(screen.getByTestId('approve-btn'))
    await waitFor(() => expect(onToast).toHaveBeenCalledWith(copy))
    // The modal stays OPEN on the 409 — the admin still needs the submission metadata
    // to re-review; act() reports failure so onApprove does not setReview(null).
    expect(screen.getByTestId('approve-btn')).toBeTruthy()
  })

  it('an approved app shows the deploy-needed indicator and Mark deployed records the runbook run + URL', async () => {
    const live = 'https://apps.bial.example.com/gate-ops'
    const prompted = vi.spyOn(window, 'prompt').mockReturnValue(`  ${live}  `)
    h.listApps.mockResolvedValue([APPROVED])
    h.markDeployed.mockResolvedValue({ appId: 'app-2', deployedUrl: live })
    render(<AppRegistryPanel onToast={() => {}} />)
    await screen.findByText('Live Tool')
    expect(screen.getByTestId('redeploy-needed-app-2')).toBeTruthy()
    fireEvent.click(screen.getByTestId('mark-deployed-app-2'))
    // Pasted URLs pick up stray whitespace — trimmed before it can 422 at the server.
    await waitFor(() => expect(h.markDeployed).toHaveBeenCalledWith('app-2', live))
    await waitFor(() => expect(h.listApps).toHaveBeenCalledTimes(2)) // reload reflects the marker
    prompted.mockRestore()
  })

  it('the URL prompt defaults to the recorded one and a blank answer keeps it (R5)', async () => {
    const live = 'https://apps.bial.example.com/gate-ops'
    const prompted = vi.spyOn(window, 'prompt').mockReturnValue('')
    h.listApps.mockResolvedValue([{ ...APPROVED, deployedUrl: live }])
    h.markDeployed.mockResolvedValue({ appId: 'app-2', deployedUrl: live })
    render(<AppRegistryPanel onToast={() => {}} />)
    await screen.findByText('Live Tool')
    fireEvent.click(screen.getByTestId('mark-deployed-app-2'))

    // The already-recorded address is pre-filled, so a routine re-deploy is one Enter…
    expect(prompted.mock.calls[0][1]).toBe(live)
    // …and a blank answer still marks the deploy, sending no URL (server keeps it).
    await waitFor(() => expect(h.markDeployed).toHaveBeenCalledWith('app-2', ''))
    prompted.mockRestore()
  })

  it('cancelling the URL prompt marks nothing at all', async () => {
    const prompted = vi.spyOn(window, 'prompt').mockReturnValue(null)
    h.listApps.mockResolvedValue([APPROVED])
    render(<AppRegistryPanel onToast={() => {}} />)
    await screen.findByText('Live Tool')
    fireEvent.click(screen.getByTestId('mark-deployed-app-2'))
    expect(h.markDeployed).not.toHaveBeenCalled()
    prompted.mockRestore()
  })

  it('surfaces the server 422 for a bad URL as a toast (no client-side URL check)', async () => {
    const prompted = vi.spyOn(window, 'prompt').mockReturnValue('http://insecure.example.com')
    const onToast = vi.fn()
    h.listApps.mockResolvedValue([APPROVED])
    h.markDeployed.mockRejectedValue(new Error('URL scheme should be https'))
    render(<AppRegistryPanel onToast={onToast} />)
    await screen.findByText('Live Tool')
    fireEvent.click(screen.getByTestId('mark-deployed-app-2'))
    await waitFor(() => expect(onToast).toHaveBeenCalledWith('URL scheme should be https'))
    prompted.mockRestore()
  })

  it('a deployed-and-current app shows NO deploy-needed indicator', async () => {
    h.listApps.mockResolvedValue([{ ...APPROVED, redeployNeeded: false }])
    render(<AppRegistryPanel onToast={() => {}} />)
    await screen.findByText('Live Tool')
    expect(screen.queryByTestId('redeploy-needed-app-2')).toBeNull()
  })

  it('renders the advisory database size column, human-formatted, and "—" when null', async () => {
    h.listApps.mockResolvedValue([
      { ...PENDING, appId: 'app-sized', databaseBytes: 2 * 1024 * 1024 },
      { ...PENDING, appId: 'app-null', name: 'No DB', databaseBytes: null },
    ])
    render(<AppRegistryPanel onToast={() => {}} />)
    await screen.findByText('Gate Tool')
    // The backend surfaces AdminAppOut.databaseBytes (R10) — the column must actually show it.
    expect(screen.getByTestId('db-bytes-app-sized').textContent).toBe('2.0 MB')
    // Null is "no number to show" (never provisioned / not ready / cluster unreachable), not 0 B.
    expect(screen.getByTestId('db-bytes-app-null').textContent).toBe('—')
  })

  it('toggling login PATCHes the inverse loginRequired', async () => {
    h.patchApp.mockResolvedValue({})
    render(<AppRegistryPanel onToast={() => {}} />)
    await screen.findByText('Gate Tool')
    fireEvent.click(screen.getByRole('button', { name: /Off/i }))
    await waitFor(() => expect(h.patchApp).toHaveBeenCalledWith('app-1', { loginRequired: true }))
  })
})

/**
 * U13 — the administrator's review screen.
 *
 * What is IN DISPUTE leads, then the automatic check's reason for each, then the
 * developer's explanation (R15). Evidence locations never appear (OD-B). An item with no
 * review says so rather than rendering blanks. And the whole thing stays operable: the
 * actions sit outside the scroll region, so a full six-category dispute cannot push
 * Approve off the bottom of a card that has no way to scroll to it.
 */
describe('the review screen leads with the dispute (R15)', () => {
  it('shows the disputed categories, their reasons, and the explanation IN THAT ORDER', async () => {
    h.listApps.mockResolvedValue([{
      ...PENDING,
      declaration: declaration({
        citizen: { ...ALL_NO, public_data: true },
        reviewAnswers: { personal_information: 'yes', financial_data: 'no' },
        reasons: {
          personal_information: 'The app stores staff names and badge numbers.',
          financial_data: 'Nothing money-related was found.',
        },
        merged: { ...ALL_NO, personal_information: true, public_data: true },
        differences: { personal_information: ['review_yes_over_citizen_no'] },
      }),
    }])
    render(<AppRegistryPanel onToast={() => {}} />)
    await openReview()

    const dispute = screen.getByTestId('dispute-personal_information')
    expect(dispute.textContent).toContain('Personal Information (PII)')
    expect(dispute.textContent).toContain('Developer said No')
    expect(dispute.textContent).toContain('Automatic check said Yes')
    expect(screen.getByTestId('dispute-reason-personal_information').textContent)
      .toBe('The app stores staff names and badge numbers.')

    // ORDER: disputes -> reasons -> explanation. Compare document positions rather than
    // eyeballing the JSX, so a reshuffle fails here.
    const body = document.body.textContent
    const disputeAt = body.indexOf('Personal Information (PII)')
    const reasonAt = body.indexOf('The app stores staff names and badge numbers.')
    const explanationAt = body.indexOf('The form only stores a staff name')
    expect(disputeAt).toBeGreaterThan(-1)
    expect(disputeAt).toBeLessThan(reasonAt)
    expect(reasonAt).toBeLessThan(explanationAt)

    // A category nobody disagreed on is NOT dressed up as a dispute.
    expect(screen.queryByTestId('dispute-financial_data')).toBeNull()
  })

  it('states the criterion — the data, not the code (P3)', async () => {
    render(<AppRegistryPanel onToast={() => {}} />)
    await openReview()
    const criterion = screen.getByTestId('review-criterion').textContent
    expect(criterion).toMatch(/acceptable to publish/i)
    expect(criterion).toMatch(/not checking whether the code is correct/i)
  })

  it('NEVER renders an evidence location (OD-B)', async () => {
    // The declaration is structurally incapable of carrying one — but a future hand that
    // "helpfully" passed the evidence document through would break this, which is the
    // point of asserting it rather than trusting the shape.
    h.listApps.mockResolvedValue([{
      ...PENDING,
      declaration: {
        ...declaration({
          citizen: ALL_NO,
          reviewAnswers: { credentials_secrets: 'yes' },
          reasons: { credentials_secrets: 'A password was written directly into the app.' },
          merged: { ...ALL_NO, credentials_secrets: true },
          differences: { credentials_secrets: ['review_yes_over_citizen_no'] },
        }),
        evidence: { questions: { credentials_secrets: [{ path: 'src/app/api/login/route.ts', kind: 'file' }] } },
      },
    }])
    render(<AppRegistryPanel onToast={() => {}} />)
    await openReview()

    // Liveness first: the screen really rendered the finding it is about.
    expect(screen.getByTestId('dispute-credentials_secrets')).toBeTruthy()
    expect(document.body.textContent).not.toContain('src/app/api/login/route.ts')
    expect(document.body.textContent).not.toContain('route.ts')
  })
})

describe('the review screen without a review, and without a declaration', () => {
  it('an item with NO review says so and shows the developers answers', async () => {
    h.listApps.mockResolvedValue([{
      ...PENDING,
      declaration: declaration({
        reviewed: null,
        citizen: { ...ALL_NO, personal_information: true },
        reviewAnswers: {},
        merged: { ...ALL_NO, personal_information: true },
        differences: {},
      }),
    }])
    render(<AppRegistryPanel onToast={() => {}} />)
    await openReview()

    expect(screen.getByTestId('review-no-review').textContent)
      .toMatch(/No automatic check informed this submission/i)
    for (const key of CATEGORY_KEYS) {
      expect(screen.getByTestId(`citizen-answer-${key}`)).toBeTruthy()
    }
    expect(screen.getByTestId('citizen-answer-personal_information').textContent).toContain('Yes')
    expect(screen.queryByTestId('review-disputes')).toBeNull()
    // …and it does NOT claim everyone agreed, which would be a different (false) thing.
    expect(screen.queryByTestId('review-no-dispute')).toBeNull()
    expect(screen.getByTestId('review-explanation').textContent).toContain('badge number')
  })

  it('an app queued BEFORE this feature renders fine and says its declaration is unavailable', async () => {
    h.listApps.mockResolvedValue([{ ...PENDING, approvalRoute: 'runbook', declaration: null }])
    render(<AppRegistryPanel onToast={() => {}} />)
    await openReview()

    expect(screen.getByTestId('review-no-declaration').textContent).toMatch(/no data declaration/i)
    expect(screen.queryByTestId('review-disputes')).toBeNull()
    expect(screen.queryByTestId('review-citizen-answers')).toBeNull()
    expect(screen.getByTestId('approve-btn')).toBeTruthy()
    expect(screen.getByTestId('review-submission-id').textContent).toContain('sub-1')
  })
})

describe('the drift-routed item (a version the developer never saw)', () => {
  it('names BOTH commits and marks the newly-raised categories as unexplained', async () => {
    h.listApps.mockResolvedValue([{
      ...PENDING,
      declaration: declaration({
        shipping: SHA,
        reviewed: OLDER_SHA,
        citizen: ALL_NO,
        reviewAnswers: { credentials_secrets: 'yes' },
        reasons: { credentials_secrets: 'A password was written directly into the app.' },
        merged: { ...ALL_NO, credentials_secrets: true },
        differences: { credentials_secrets: ['review_yes_over_citizen_no'] },
      }),
    }])
    render(<AppRegistryPanel onToast={() => {}} />)
    await openReview()

    const drift = screen.getByTestId('review-drift').textContent
    expect(drift).toContain(OLDER_SHA.slice(0, 7))
    expect(drift).toContain(SHA.slice(0, 7))
    expect(screen.getByTestId('dispute-unexplained-credentials_secrets').textContent)
      .toMatch(/Not covered by the explanation/i)
  })

  it('does NOT cry drift when the reviewed and shipping commits are the same', async () => {
    h.listApps.mockResolvedValue([{
      ...PENDING,
      declaration: declaration({
        citizen: ALL_NO,
        reviewAnswers: { credentials_secrets: 'yes' },
        merged: { ...ALL_NO, credentials_secrets: true },
        differences: { credentials_secrets: ['review_yes_over_citizen_no'] },
      }),
    }])
    render(<AppRegistryPanel onToast={() => {}} />)
    await openReview()
    expect(screen.getByTestId('dispute-credentials_secrets')).toBeTruthy() // liveness
    expect(screen.queryByTestId('review-drift')).toBeNull()
    expect(screen.queryByTestId('dispute-unexplained-credentials_secrets')).toBeNull()
  })
})

describe('the scroll contract — Approve and Reject stay reachable', () => {
  it('a full six-category dispute plus a long explanation leaves the actions OUTSIDE the scroll region', async () => {
    const everything = declaration({
      citizen: ALL_NO,
      reviewAnswers: Object.fromEntries(CATEGORY_KEYS.map((k) => [k, 'yes'])),
      reasons: Object.fromEntries(CATEGORY_KEYS.map((k) => [k, `A long reason about ${k}. `.repeat(20)])),
      merged: Object.fromEntries(CATEGORY_KEYS.map((k) => [k, true])),
      differences: Object.fromEntries(CATEGORY_KEYS.map((k) => [k, ['review_yes_over_citizen_no']])),
      explanation: 'We handle this carefully. '.repeat(200),
    })
    h.listApps.mockResolvedValue([{ ...PENDING, declaration: everything }])
    render(<AppRegistryPanel onToast={() => {}} />)
    await openReview()

    // All six really are rendered — otherwise the reachability claim is about a short card.
    for (const key of CATEGORY_KEYS) expect(screen.getByTestId(`dispute-${key}`)).toBeTruthy()

    const scroller = screen.getByTestId('review-scroll')
    const approve = screen.getByTestId('approve-btn')
    const reject = screen.getByTestId('reject-btn')
    // THE CONTRACT, structurally: the actions are not descendants of the scrolling block,
    // so no amount of content can move them out of reach. jsdom computes no layout, so a
    // pixel assertion here would be theatre — containment is the real mechanism.
    expect(scroller.contains(approve)).toBe(false)
    expect(scroller.contains(reject)).toBe(false)
    expect(scroller.className).toMatch(/overflow-y-auto/)
    expect(scroller.className).toMatch(/min-h-0/)
    expect(scroller.parentElement.className).toMatch(/max-h-\[90vh\]/)
    expect(scroller.parentElement.className).toMatch(/flex-col/)
  })
})

describe('the rejection note is required, with a floor (P3)', () => {
  it('disables Send rejection below 20 characters and says how far off it is', async () => {
    render(<AppRegistryPanel onToast={() => {}} />)
    await openReview()
    fireEvent.click(screen.getByTestId('reject-btn'))

    expect(screen.getByTestId('reject-confirm').disabled).toBe(true) // empty
    expect(screen.getByTestId('reject-note-help').textContent).toMatch(/at least 20 characters/)

    fireEvent.change(screen.getByTestId('reject-note'), { target: { value: 'too short' } })
    expect(screen.getByTestId('reject-confirm').disabled).toBe(true)
    expect(screen.getByTestId('reject-note-help').textContent).toMatch(/\(9 so far\)/)

    // Whitespace does not count toward the floor, on this side of the wire either.
    fireEvent.change(screen.getByTestId('reject-note'), { target: { value: '                       ' } })
    expect(screen.getByTestId('reject-confirm').disabled).toBe(true)

    fireEvent.change(screen.getByTestId('reject-note'), {
      target: { value: '  Please name a data owner before publishing this.  ' },
    })
    expect(screen.getByTestId('reject-confirm').disabled).toBe(false)
    expect(h.rejectApp).not.toHaveBeenCalled()
  })

  it('sends the TRIMMED note', async () => {
    h.rejectApp.mockResolvedValue({ status: 'rejected' })
    render(<AppRegistryPanel onToast={() => {}} />)
    await openReview()
    fireEvent.click(screen.getByTestId('reject-btn'))
    fireEvent.change(screen.getByTestId('reject-note'), {
      target: { value: '  Please name a data owner before publishing this.  ' },
    })
    fireEvent.click(screen.getByTestId('reject-confirm'))

    await waitFor(() => expect(h.rejectApp).toHaveBeenCalledWith(
      'app-1', 'Please name a data owner before publishing this.',
    ))
  })

  it('the note field is labelled, required, and described by its help text', async () => {
    render(<AppRegistryPanel onToast={() => {}} />)
    await openReview()
    fireEvent.click(screen.getByTestId('reject-btn'))
    const field = screen.getByTestId('reject-note')
    expect(field.getAttribute('id')).toBe('reject-note')
    expect(field.getAttribute('aria-required')).toBe('true')
    expect(field.getAttribute('aria-describedby')).toBe('reject-note-help')
    expect(document.querySelector('label[for="reject-note"]').textContent).toMatch(/required/i)
  })
})

describe('a submission withdrawn while the modal was open', () => {
  it('renders the withdrawal message IN PLACE OF the actions', async () => {
    h.approveApp.mockRejectedValue(new ApiError(
      'The developer withdrew this submission, so there is nothing left to decide.',
      409,
      'submission_withdrawn',
    ))
    render(<AppRegistryPanel onToast={() => {}} />)
    await openReview()
    fireEvent.click(screen.getByTestId('approve-btn'))

    const message = await screen.findByTestId('review-withdrawn')
    expect(message.textContent).toMatch(/withdrew this submission/i)
    // In PLACE OF: neither action survives, so there is nothing left to click twice.
    expect(screen.queryByTestId('approve-btn')).toBeNull()
    expect(screen.queryByTestId('reject-btn')).toBeNull()
    expect(screen.getByTestId('withdrawn-close')).toBeTruthy()
    // It announces: the block is a polite live region, not a silent swap.
    expect(screen.getByTestId('review-status').getAttribute('aria-live')).toBe('polite')
  })

  it('a DIFFERENT 409 leaves the actions alone — only withdrawal replaces them', async () => {
    const copy = 'This app was re-submitted since you reviewed it — please re-review.'
    h.approveApp.mockRejectedValue(new ApiError(copy, 409, null))
    const onToast = vi.fn()
    render(<AppRegistryPanel onToast={onToast} />)
    await openReview()
    fireEvent.click(screen.getByTestId('approve-btn'))

    await waitFor(() => expect(onToast).toHaveBeenCalledWith(copy))
    expect(screen.queryByTestId('review-withdrawn')).toBeNull()
    expect(screen.getByTestId('approve-btn')).toBeTruthy()
  })
})

describe('the self-publish lineage has no runbook (R17a)', () => {
  it('an approved self-publish app shows neither Deploy needed nor Mark deployed', async () => {
    h.listApps.mockResolvedValue([{ ...APPROVED, approvalRoute: 'self_publish', redeployNeeded: false }])
    render(<AppRegistryPanel onToast={() => {}} />)
    await screen.findByText('Live Tool') // liveness: the row rendered

    expect(screen.queryByTestId('redeploy-needed-app-2')).toBeNull()
    expect(screen.queryByTestId('mark-deployed-app-2')).toBeNull()
    expect(screen.getByTestId('audit-app-2')).toBeTruthy() // the row's other controls survive
  })

  it('the review copy tells a self-publish admin NOT to run a runbook', async () => {
    h.listApps.mockResolvedValue([{ ...PENDING, approvalRoute: 'self_publish' }])
    render(<AppRegistryPanel onToast={() => {}} />)
    await openReview()
    expect(screen.getByTestId('review-self-publish-note').textContent)
      .toMatch(/publishes this approved version themselves/i)
    expect(screen.queryByTestId('review-runbook-note')).toBeNull()
    expect(document.body.textContent).not.toMatch(/Mark deployed/)
  })

  it('a runbook-lineage submission keeps the runbook copy', async () => {
    h.listApps.mockResolvedValue([{ ...PENDING, approvalRoute: 'runbook' }])
    render(<AppRegistryPanel onToast={() => {}} />)
    await openReview()
    expect(screen.getByTestId('review-runbook-note').textContent).toMatch(/go-live runbook/i)
    expect(screen.queryByTestId('review-self-publish-note')).toBeNull()
  })
})

describe('the waiting count is mirrored on the pending tab (P1)', () => {
  it('renders the badge with its accessible name', async () => {
    h.fetchAppStatusCounts.mockResolvedValue({
      draft: 0, pending: 4, approved: 0, rejected: 0, disabled: 0,
    })
    render(<AppRegistryPanel onToast={() => {}} />)
    await screen.findByText('Gate Tool')
    await waitFor(() => expect(screen.getByTestId('waiting-count-tab').textContent).toContain('4'))
    expect(screen.getByText('4 apps waiting for review')).toBeTruthy()
  })

  it('drops the badge at zero', async () => {
    h.fetchAppStatusCounts.mockResolvedValue({
      draft: 0, pending: 0, approved: 0, rejected: 0, disabled: 0,
    })
    render(<AppRegistryPanel onToast={() => {}} />)
    await screen.findByText('Gate Tool')
    expect(screen.queryByTestId('waiting-count-tab')).toBeNull()
  })

  it('a failed count leaves the queue working and shows no number', async () => {
    h.fetchAppStatusCounts.mockRejectedValue(new Error('nope'))
    render(<AppRegistryPanel onToast={() => {}} />)
    await screen.findByText('Gate Tool') // the table still renders
    expect(screen.queryByTestId('waiting-count-tab')).toBeNull()
  })
})
