// `onToast(message, 'problem')` is the severity AdminPage's shared toast channel
// uses to render a failure differently from a confirmation. Every failure-path
// assertion below carries that second argument; success-path ones don't.
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
  declaration: null,
}

const APPROVED = {
  ...PENDING,
  appId: 'app-2',
  name: 'Live Tool',
  status: 'approved',
  hasApprovedSnapshot: true,
}

/**
 * A declaration in the shape the publish gate writes
 * (`backend/src/api/v1/deploy/router.py::_declaration`) — snake_case keys inside the
 * document, camelCase inside its sub-objects, exactly as stored.
 */
const declaration = ({
  shipping = SHA,
  reviewed = SHA,
  answeredAbout = null,
  citizen = {},
  reviewAnswers = {},
  reasons = {},
  merged = {},
  differences = {},
  explanation = 'The form only stores a staff name and a badge number, both kept in the app’s own database.',
} = {}) => ({
  commits: { shipping, reviewed },
  // This block, present ONLY on the pipeline's drift path — which is the only place the
  // answered-about commit and the shipping commit ever differ. The `commits` pair cannot
  // express drift: the writer sets `reviewed` from the same head_sha as `shipping`.
  ...(answeredAbout === null ? {} : { drift: { answeredAbout, shipping } }),
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
    expect(screen.getByTestId('apps-tab-approved')).toBeTruthy()
    expect(screen.getByTestId('apps-tab-disabled')).toBeTruthy()
    expect(screen.queryByText('Security Flags')).toBeNull()
    expect(screen.getAllByText('Pending Review').length).toBeGreaterThan(0) // tab + badge
  })

  it('the review modal shows submission METADATA (SHA, submitted-at) and no internal ids', async () => {
    render(<AppRegistryPanel onToast={() => {}} />)
    await screen.findByText('Gate Tool')
    fireEvent.click(screen.getByTestId('review-app-1'))
    expect(screen.getByTestId('review-commit-sha').textContent).toContain(SHA.slice(0, 12))
    expect(screen.getByTestId('review-submitted-at').textContent).not.toContain('—')

    // The submission's own id is what the approval PINS, and it is still sent with the
    // approval — but it is an internal identifier no administrator can act on, so it is
    // not read off the screen. The Build above names the version in a form that means
    // something. (Liveness: the modal is genuinely rendered, so this is not a false pass.)
    expect(screen.queryByTestId('review-submission-id')).toBeNull()
    expect(screen.getByTestId('review-criterion')).toBeTruthy()

    // A missing submitted-at must read as missing, never the epoch: folding null into
    // `new Date(0)` rendered "1/1/1970" above the Approve button as if it were a fact.
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
    // The dead bundle-download control is gone too — button and instruction both.
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
    await waitFor(() => expect(onToast).toHaveBeenCalledWith(copy, 'problem'))
    // The modal stays OPEN on the 409: act() reports failure, so onApprove never nulls the review.
    expect(screen.getByTestId('approve-btn')).toBeTruthy()
  })

  it('renders the advisory database size column, human-formatted, and "—" when null', async () => {
    h.listApps.mockResolvedValue([
      { ...PENDING, appId: 'app-sized', databaseBytes: 2 * 1024 * 1024 },
      { ...PENDING, appId: 'app-null', name: 'No DB', databaseBytes: null },
    ])
    render(<AppRegistryPanel onToast={() => {}} />)
    await screen.findByText('Gate Tool')
    // The backend surfaces AdminAppOut.databaseBytes — the column must actually show it.
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
 * The administrator's review screen.
 *
 * What is IN DISPUTE leads, then the automatic check's reason for each, then the
 * developer's explanation. Evidence locations never appear. An item with no
 * review says so rather than rendering blanks. And the whole thing stays operable: the
 * actions sit outside the scroll region, so a full six-category dispute cannot push
 * Approve off the bottom of a card that has no way to scroll to it.
 */
describe('the review screen leads with the dispute', () => {
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

  it('states the criterion — the data, not the code', async () => {
    render(<AppRegistryPanel onToast={() => {}} />)
    await openReview()
    const criterion = screen.getByTestId('review-criterion').textContent
    expect(criterion).toMatch(/acceptable to publish/i)
    expect(criterion).toMatch(/not checking whether the code is correct/i)
  })

  it('NEVER renders an evidence location', async () => {
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
    h.listApps.mockResolvedValue([{ ...PENDING, declaration: null }])
    render(<AppRegistryPanel onToast={() => {}} />)
    await openReview()

    expect(screen.getByTestId('review-no-declaration').textContent).toMatch(/no data declaration/i)
    expect(screen.getByTestId('review-no-declaration').textContent).not.toMatch(/go-live/i)
    expect(screen.queryByTestId('review-disputes')).toBeNull()
    expect(screen.queryByTestId('review-citizen-answers')).toBeNull()
    expect(screen.getByTestId('approve-btn')).toBeTruthy()
    // "Decide from the submission details above" has to mean something: with no
    // declaration, the build is the only fact about the version on the screen.
    expect(screen.getByTestId('review-commit-sha').textContent).toContain(SHA.slice(0, 12))
  })
})

describe('the drift-routed item (a version the developer never saw)', () => {
  it('names BOTH commits and marks the newly-raised categories as unexplained', async () => {
    h.listApps.mockResolvedValue([{
      ...PENDING,
      declaration: declaration({
        shipping: SHA,
        answeredAbout: OLDER_SHA,
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

  it('does not cry drift from the commits pair alone — the shape the backend cannot emit', async () => {
    // THE GUARD ON THE DEAD PATH. Drift used to be derived from
    // `commits.shipping !== commits.reviewed`, and the old test hand-built exactly this
    // record to prove it. The writer cannot produce it: `reviewed` is set from the same
    // head_sha as `shipping`, so the pair is only ever equal or half-null. If this ever
    // goes red, the reader has drifted back to reading the pair.
    h.listApps.mockResolvedValue([{
      ...PENDING,
      declaration: declaration({ shipping: SHA, reviewed: OLDER_SHA, citizen: ALL_NO }),
    }])
    render(<AppRegistryPanel onToast={() => {}} />)
    await openReview()

    expect(screen.queryByTestId('review-drift')).toBeNull()
    // Paired liveness: the modal really did render, so the absence above is a decision
    // rather than a component that threw.
    expect(screen.getByTestId('review-explanation')).toBeTruthy()
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

describe('the rejection note is required, with a floor', () => {
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

    await waitFor(() => expect(onToast).toHaveBeenCalledWith(copy, 'problem'))
    expect(screen.queryByTestId('review-withdrawn')).toBeNull()
    expect(screen.getByTestId('approve-btn')).toBeTruthy()
  })
})

describe('closing the review puts focus somewhere real', () => {
  // THE MODAL IS HAND-ROLLED — no Radix `DialogContent`, so no `FocusScope` capturing the
  // element that had focus and restoring it on unmount. Every route out of it dropped focus on
  // `<body>`, where the next Tab restarts at the top of the document rather than at the queue
  // the administrator is working through.

  it('dismissing it returns focus to the row’s own Review button', async () => {
    render(<AppRegistryPanel onToast={() => {}} />)
    await openReview()
    // LIVENESS FIRST: the modal really opened, so "it is gone" below is a close rather than an
    // assertion that ran before anything rendered.
    expect(screen.getByTestId('approve-btn')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Close' }))

    await waitFor(() => expect(screen.queryByTestId('approve-btn')).toBeNull())
    // The queue is untouched by a dismissal, so the button that opened the review is still
    // there — and three rows down a queue of forty, it is where the administrator belongs.
    expect(screen.getByTestId('app-row-app-1')).toBeTruthy()
    expect(document.activeElement).toBe(screen.getByTestId('review-app-1'))
  })

  it('approving it — which destroys the Review button — lands focus on the queue’s tab', async () => {
    // THE TRIGGER IS DESTROYED BY ITS OWN SUCCESS. An approved app leaves the pending queue, so
    // the reload that follows takes the whole row (and its Review button) with it: restoring to
    // the trigger would focus a detached node and silently do nothing, which is `<body>` again.
    h.approveApp.mockResolvedValue({ status: 'approved' })
    h.listApps.mockResolvedValueOnce([PENDING]).mockResolvedValue([])
    const onToast = vi.fn()
    render(<AppRegistryPanel onToast={onToast} />)
    await openReview()

    fireEvent.click(screen.getByTestId('approve-btn'))

    // LIVENESS: the approve really went through and the panel really re-rendered on the reload —
    // not a component that threw somewhere between the two.
    await waitFor(() => expect(onToast).toHaveBeenCalledWith('“Gate Tool” approved'))
    await waitFor(() => expect(h.listApps).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(screen.queryByTestId('approve-btn')).toBeNull())
    expect(screen.queryByTestId('review-app-1')).toBeNull() // the trigger is genuinely gone

    expect(document.activeElement).toBe(screen.getByTestId('apps-tab-pending'))
  })

  it('…and still does when the reload has taken the whole panel off the screen first', async () => {
    // THE RELOAD IS NOT INSTANT FOR A CITIZEN. `load` raises `loading` before it asks the server,
    // and this panel answers a truthy `loading` with a spinner INSTEAD of itself — so for the
    // length of a real round trip the tab strip, every row and the modal are all out of the DOM,
    // and the landmark this restore aims at is not merely detached but absent. A mock that
    // resolves in a microtask never renders that frame; this one does, so the assertion is about
    // a tab that was rebuilt rather than one that never left.
    h.approveApp.mockResolvedValue({ status: 'approved' })
    let release = () => {}
    h.listApps
      .mockResolvedValueOnce([PENDING])
      .mockReturnValueOnce(new Promise((resolve) => { release = () => resolve([]) }))
      .mockResolvedValue([])
    render(<AppRegistryPanel onToast={() => {}} />)
    await openReview()

    fireEvent.click(screen.getByTestId('approve-btn'))

    // The panel really is gone mid-reload — the assertion below is about coming back from that,
    // not about a screen that never moved.
    await waitFor(() => expect(screen.queryByTestId('apps-tab-pending')).toBeNull())
    release()

    await waitFor(() => expect(screen.getByTestId('apps-tab-pending')).toBeTruthy())
    expect(screen.queryByTestId('review-app-1')).toBeNull()
    expect(document.activeElement).toBe(screen.getByTestId('apps-tab-pending'))
  })
})

describe('there is no way to record a deployment by hand', () => {
  it('an approved app carries only the ops controls, and no deploy prompt', async () => {
    h.listApps.mockResolvedValue([APPROVED])
    render(<AppRegistryPanel onToast={() => {}} />)
    await screen.findByText('Live Tool')

    const row = screen.getByTestId('audit-app-2').closest('tr')
    const controls = [...row.querySelectorAll('button[data-testid], span[data-testid]')]
      .map((el) => el.getAttribute('data-testid'))
    expect(controls).toEqual(['disable-app-2', 'audit-app-2', 'delete-app-2'])
    expect(row.textContent).not.toMatch(/deploy needed/i)
  })
})

describe('approving publishes, and the review says so', () => {
  it('a submission is approved and published by one button', async () => {
    h.listApps.mockResolvedValue([PENDING])
    render(<AppRegistryPanel onToast={() => {}} />)
    await openReview()

    expect(screen.getByTestId('approve-btn').textContent).toContain('Approve and publish')
    expect(screen.getByTestId('review-publish-note').textContent).toMatch(/approving publishes it/i)
    // Nothing left over from the manual route or the developer's second click.
    expect(document.body.textContent).not.toMatch(/go-live runbook/i)
    expect(document.body.textContent).not.toMatch(/publishes this approved version themselves/i)
  })
})

describe('the waiting count is mirrored on the pending tab', () => {
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

/**
 * The two surfaces that used to put internal identifiers in front of an administrator: the
 * row under every app name, and the audit trail, which rendered the stored action token
 * verbatim. Neither is something an administrator can act on, and `publish_gate` names a
 * column rather than an event.
 */
describe('internal identifiers stay out of the administrator’s way', () => {
  it('names the app in the table without its internal id', async () => {
    render(<AppRegistryPanel onToast={() => {}} />)
    await screen.findByText('Gate Tool')
    // Liveness first: the row is genuinely rendered, so the absence below is meaningful.
    expect(screen.getByTestId('app-row-app-1')).toBeTruthy()
    expect(screen.queryByText('app-1')).toBeNull()
  })

  it('falls back to a readable name, never to the id, for an untitled app', async () => {
    h.listApps.mockResolvedValue([{ ...PENDING, name: null }])
    render(<AppRegistryPanel onToast={() => {}} />)
    expect(await screen.findByText('(untitled app)')).toBeTruthy()
    expect(screen.queryByText('app-1')).toBeNull()
  })

  it('the audit trail says what happened, not which column it was written to', async () => {
    h.fetchAudit.mockResolvedValue([
      { id: 'e1', action: 'classification_review', username: 'alice', createdAt: '2026-07-16T09:00:00Z', resourceId: 'app-1', count: null },
      { id: 'e2', action: 'publish_gate', username: 'alice', createdAt: '2026-07-16T09:01:00Z', resourceId: 'app-1', count: null },
      { id: 'e3', action: 'reject', username: 'admin', createdAt: '2026-07-16T09:02:00Z', resourceId: 'app-1', count: null },
    ])
    render(<AppRegistryPanel onToast={() => {}} />)
    await screen.findByText('Gate Tool')
    fireEvent.click(screen.getByTestId('audit-app-1'))

    expect(await screen.findByText('Automatic data check')).toBeTruthy()
    expect(screen.getByText('Publish decision')).toBeTruthy()
    expect(screen.getByText('Sent back for changes')).toBeTruthy()
    // The raw tokens are gone, and so is the app id repeated on every single row — every
    // event in this drawer is about the one app already named in the header.
    expect(screen.queryByText('classification_review')).toBeNull()
    expect(screen.queryByText('publish_gate')).toBeNull()
    expect(screen.queryByText(/· app-1/)).toBeNull()
  })

  it('explains each entry rather than leaving the title to carry it', async () => {
    h.fetchAudit.mockResolvedValue([
      { id: 'e1', action: 'publish_gate', username: 'alice', createdAt: '2026-07-16T09:00:00Z', resourceId: 'app-1', count: null },
    ])
    render(<AppRegistryPanel onToast={() => {}} />)
    await screen.findByText('Gate Tool')
    fireEvent.click(screen.getByTestId('audit-app-1'))

    expect(await screen.findByText(/decided whether the app could go live or needed a person/i)).toBeTruthy()
    // The actor is still on record — the trail's whole job — just not the row's id.
    expect(screen.getByText(/by alice/)).toBeTruthy()
  })
})

/**
 * The review queue shows how old the backlog is.
 *
 * The pending list is ordered oldest-submission-first and pinned by a backend test,
 * so the queue already encodes age in a row's POSITION — but nothing on screen said so,
 * and no row said how old. A submission waiting 43 days was drawn identically to one that
 * arrived a minute ago.
 *
 * Two things fix that and one thing must NOT: a Submitted column carrying the visible age
 * (absolute moment underneath as `title`/`datetime`), a caption naming the ordering, and
 * emphatically no sort control — a handle that let someone reorder the queue would turn a
 * reporting gap into a real defect. Both additions are TAB-CONDITIONAL: one <thead>/<tbody>
 * serves all four tabs, and `submittedAt` is null outside pending.
 */
const daysAgo = (n) => new Date(Date.now() - n * 24 * 60 * 60 * 1000).toISOString()

describe('the review queue shows how old the backlog is', () => {
  it('a pending row says how long it has been waiting, with the exact moment underneath', async () => {
    const iso = daysAgo(43)
    h.listApps.mockResolvedValue([{ ...PENDING, submittedAt: iso }])
    render(<AppRegistryPanel onToast={() => {}} />)
    await screen.findByText('Gate Tool')

    // The column exists, and the 43-day-old row reads as 43 days old.
    expect(screen.getByRole('columnheader', { name: 'Submitted' })).toBeTruthy()
    const cell = screen.getByTestId('submitted-app-1')
    expect(cell.textContent).toBe('43 days ago')

    // The age is what an administrator reads; the absolute moment is still on the row,
    // machine-readable and on hover, so "43 days" can be resolved to a date.
    const stamp = cell.querySelector('time')
    expect(stamp).toBeTruthy()
    expect(stamp.getAttribute('datetime')).toBe(iso)
    expect(stamp.getAttribute('title')).toBe(new Date(iso).toLocaleString())
  })

  it('reads in whatever unit the wait actually is, not always days', async () => {
    const minutesAgo = (n) => new Date(Date.now() - n * 60 * 1000).toISOString()
    h.listApps.mockResolvedValue([
      { ...PENDING, appId: 'fresh', name: 'Fresh Tool', submittedAt: minutesAgo(0) },
      { ...PENDING, appId: 'mins', name: 'Minutes Tool', submittedAt: minutesAgo(1) },
      { ...PENDING, appId: 'hours', name: 'Hours Tool', submittedAt: minutesAgo(3 * 60) },
      { ...PENDING, appId: 'oneday', name: 'Day Tool', submittedAt: daysAgo(1) },
    ])
    render(<AppRegistryPanel onToast={() => {}} />)
    await screen.findByText('Fresh Tool')
    expect(screen.getByTestId('submitted-fresh').textContent).toBe('just now')
    // Singular reads as singular — "1 minutes ago" is the tell of a formatter nobody read.
    expect(screen.getByTestId('submitted-mins').textContent).toBe('1 minute ago')
    expect(screen.getByTestId('submitted-hours').textContent).toBe('3 hours ago')
    expect(screen.getByTestId('submitted-oneday').textContent).toBe('1 day ago')
  })

  it('a row with no submittedAt keeps the guarded placeholder — never an age counted from 1970', async () => {
    h.listApps.mockResolvedValue([
      { ...PENDING, appId: 'nowhen', name: 'No Date Tool', submittedAt: null },
      { ...PENDING, appId: 'garbage', name: 'Bad Date Tool', submittedAt: 'not-a-timestamp' },
    ])
    render(<AppRegistryPanel onToast={() => {}} />)
    await screen.findByText('No Date Tool')

    for (const id of ['nowhen', 'garbage']) {
      const cell = screen.getByTestId(`submitted-${id}`)
      expect(cell.textContent).toBe('—')
      // Age-from-null is the "1/1/1970" bug in a different unit: ~56 years of waiting.
      expect(cell.textContent).not.toMatch(/1970|ago|year/i)
      // And no <time> either — there is no moment to point a datetime at.
      expect(cell.querySelector('time')).toBeNull()
    }
  })

  it('labels the ordering on the pending tab, in the words the queue actually guarantees', async () => {
    render(<AppRegistryPanel onToast={() => {}} />)
    await screen.findByText('Gate Tool') // liveness: the queue rendered

    const note = screen.getByTestId('queue-order-note')
    expect(note.textContent).toBe('Oldest first — the next app to review is at the top.')
    // A <caption> is tied to the table it describes, so the claim cannot drift away from
    // the rows it is about.
    expect(note.tagName).toBe('CAPTION')
    expect(note.closest('table')).toBeTruthy()
  })

  it('the other tabs get neither the column nor the ordering label', async () => {
    // Only the pending list is a review queue; every other view is newest-created-first,
    // where "oldest first" would be a plain lie. The APPROVED fixture deliberately CARRIES
    // a submittedAt — the condition under test is the tab, not the row's data.
    h.listApps.mockImplementation((status) =>
      Promise.resolve(status === 'pending' ? [PENDING] : [APPROVED]))
    render(<AppRegistryPanel onToast={() => {}} />)
    await screen.findByText('Gate Tool')
    expect(screen.getByTestId('queue-order-note')).toBeTruthy()

    fireEvent.click(screen.getByTestId('apps-tab-approved'))
    await screen.findByText('Live Tool')
    // Liveness: the approved tab genuinely rendered its table, so the absences below mean
    // something. (APPROVED.submittedAt is set, and still nothing shows it.)
    expect(screen.getByTestId('app-row-app-2')).toBeTruthy()
    expect(screen.getByTestId('db-bytes-app-2')).toBeTruthy()

    expect(screen.queryByTestId('queue-order-note')).toBeNull()
    expect(screen.queryByTestId('submitted-app-2')).toBeNull()
    expect(screen.queryByRole('columnheader', { name: 'Submitted' })).toBeNull()
    expect(document.body.textContent).not.toMatch(/oldest first/i)
  })

  it('renders NO sort control — the ordering is a guarantee, not a preference', async () => {
    h.listApps.mockResolvedValue([
      { ...PENDING, submittedAt: daysAgo(43) },
      { ...PENDING, appId: 'app-3', name: 'Second Tool', submittedAt: daysAgo(2) },
    ])
    render(<AppRegistryPanel onToast={() => {}} />)
    await screen.findByText('Gate Tool')

    // LIVENESS FIRST. Every assertion below is an absence, and a crashed render satisfies
    // all of them — so prove the table, its headers and both rows are actually on screen.
    const headers = screen.getAllByRole('columnheader')
    expect(headers.map((el) => el.textContent)).toContain('Submitted')
    expect(screen.getByTestId('app-row-app-1')).toBeTruthy()
    expect(screen.getByTestId('app-row-app-3')).toBeTruthy()

    // Nothing offers to reorder the queue: no sort affordance anywhere, and every column
    // header is inert text rather than a clickable sort handle.
    expect(screen.queryByRole('button', { name: /sort|order/i })).toBeNull()
    expect(screen.queryByRole('combobox')).toBeNull()
    expect(document.body.textContent).not.toMatch(/sort/i)
    for (const el of headers) {
      expect(el.querySelector('button, select, a, [role="button"]')).toBeNull()
      expect(el.getAttribute('aria-sort')).toBeNull()
    }
  })
})

// --- the kill switch, widened to draft and rejected -----------------------------------

const DRAFT = {
  ...PENDING,
  appId: 'app-4',
  name: 'Self Published Tool',
  status: 'draft',
  submittedAt: null,
}
const REJECTED = { ...PENDING, appId: 'app-5', name: 'Turned Down Tool', status: 'rejected' }

describe('AppRegistryPanel — switching an app off', () => {
  it('offers the kill switch on draft and rejected rows, and still withholds it from pending', async () => {
    // The server widened `STATUS_TRANSITIONS[DISABLED]` to {approved, draft, rejected};
    // this is the half that makes the transition REACHABLE. Pending is excluded on both
    // sides — an app in the review queue is rejected, not switched off — so a control here
    // would only ever produce a 409.
    h.listApps.mockResolvedValue([PENDING, DRAFT, REJECTED, APPROVED])
    render(<AppRegistryPanel onToast={() => {}} />)
    await screen.findByText('Self Published Tool')

    // Liveness first: every row really rendered, so the one absence below means something.
    for (const id of ['app-1', 'app-4', 'app-5', 'app-2']) {
      expect(screen.getByTestId(`app-row-${id}`)).toBeTruthy()
    }
    expect(screen.getByTestId('disable-app-4')).toBeTruthy() // draft
    expect(screen.getByTestId('disable-app-5')).toBeTruthy() // rejected
    expect(screen.getByTestId('disable-app-2')).toBeTruthy() // approved, as before
    expect(screen.queryByTestId('disable-app-1')).toBeNull() // pending — deliberately not
  })

  it('switching off a draft calls the API for that app and confirms by name', async () => {
    h.listApps.mockResolvedValue([DRAFT])
    h.disableApp.mockResolvedValue({ status: 'disabled' })
    const onToast = vi.fn()
    render(<AppRegistryPanel onToast={onToast} />)
    await screen.findByText('Self Published Tool')

    fireEvent.click(screen.getByTestId('disable-app-4'))

    await waitFor(() => expect(h.disableApp).toHaveBeenCalledWith('app-4'))
    // A bare confirmation, not a failure-severity toast — and it names the app, so an
    // administrator with several rows on screen can see which one they just switched off.
    expect(onToast).toHaveBeenCalledWith('“Self Published Tool” disabled')
    expect(h.listApps).toHaveBeenCalledTimes(2) // the list reloads onto the new status
  })

  it('has a Draft tab at all, so the ordinary self-published app has a row to act on', async () => {
    // Draft was hidden as "builder-side", which left the app the kill switch most needs to
    // reach — a one-click deploy never writes a status — with no row on this screen. The
    // widened transition is unreachable without this tab.
    h.listApps.mockImplementation((status) =>
      Promise.resolve(status === 'draft' ? [DRAFT] : [PENDING]))
    render(<AppRegistryPanel onToast={() => {}} />)
    await screen.findByText('Gate Tool') // pending is still the default tab

    fireEvent.click(screen.getByTestId('apps-tab-draft'))
    await screen.findByText('Self Published Tool')
    expect(h.listApps).toHaveBeenCalledWith('draft')
    expect(screen.getByTestId('disable-app-4')).toBeTruthy()
  })
})

describe('★ the admin delete collects a reason', () => {
  // A `window.confirm` stood here and could collect nothing, while the route already REQUIRED a
  // word-bounded justification — so every delete through this panel answered 422. It shipped green
  // because `deleteApp` is mocked wholesale in this file: both halves passed while disagreeing.
  // These tests assert what the panel actually hands the client.
  const REASON = 'Duplicate app created in error during onboarding, owner asked for removal'

  it('will not delete until the reason meets the shared word rule', async () => {
    h.listApps.mockResolvedValue([APPROVED])
    render(<AppRegistryPanel onToast={vi.fn()} />)
    await screen.findByText(APPROVED.name)

    fireEvent.click(screen.getByTestId(`delete-${APPROVED.appId}`))
    const confirm = screen.getByTestId('admin-delete-confirm')

    // Too short — the same 5-word floor the citizen's own delete uses.
    fireEvent.change(screen.getByTestId('admin-delete-reason'), { target: { value: 'because' } })
    // ANNOUNCED, NOT `disabled`. A real `disabled` attribute on the control the citizen is
    // about to press throws focus to the document body; the refusal lives in the handler, so
    // the press below is what proves it holds.
    expect(confirm.getAttribute('aria-disabled')).toBe('true')
    fireEvent.click(confirm)
    expect(h.deleteApp).not.toHaveBeenCalled()

    // LIVENESS, so the refusal above means the gate fired rather than the dialog never opening.
    expect(screen.getByTestId('admin-delete-reason')).toBeTruthy()
  })

  it('sends the reason through to the client, not just a confirmation', async () => {
    h.listApps.mockResolvedValue([APPROVED])
    h.deleteApp.mockResolvedValue({ ok: true })
    const onToast = vi.fn()
    render(<AppRegistryPanel onToast={onToast} />)
    await screen.findByText(APPROVED.name)

    fireEvent.click(screen.getByTestId(`delete-${APPROVED.appId}`))
    fireEvent.change(screen.getByTestId('admin-delete-reason'), { target: { value: REASON } })
    fireEvent.click(screen.getByTestId('admin-delete-confirm'))

    // ★ THE ASSERTION THAT WOULD HAVE CAUGHT THE BREAK: the reason is the second argument.
    await waitFor(() => expect(h.deleteApp).toHaveBeenCalledWith(APPROVED.appId, REASON))
  })

  it('★ Escape closes it and the keyboard lands back on the control that opened it', async () => {
    // It was hand-rolled — a `fixed inset-0` div with `role="dialog"` and nothing else — so
    // Escape did nothing, Tab walked straight out of it, and closing it dropped focus on the
    // document body. The most destructive control on this screen had the weakest keyboard
    // contract on it, in the very file whose review modal carries an explicit focus restore.
    h.listApps.mockResolvedValue([APPROVED])
    render(<AppRegistryPanel onToast={vi.fn()} />)
    await screen.findByText(APPROVED.name)

    const trash = screen.getByTestId(`delete-${APPROVED.appId}`)
    trash.focus()
    fireEvent.click(trash)
    const field = screen.getByTestId('admin-delete-reason')
    // LIVENESS: it really opened and really took focus off the trash, so the restore below is
    // a restore rather than focus that never moved.
    expect(field).toBeTruthy()
    expect(document.activeElement).not.toBe(trash)

    fireEvent.keyDown(document.activeElement || document.body, { key: 'Escape' })

    await waitFor(() => expect(screen.queryByTestId('admin-delete-reason')).toBeNull())
    await waitFor(() => expect(document.activeElement).toBe(trash))
  })

  it('keeps the words on screen when the server refuses them', async () => {
    h.listApps.mockResolvedValue([APPROVED])
    h.deleteApp.mockRejectedValue(new Error('Say why in 2 to 50 words.'))
    render(<AppRegistryPanel onToast={vi.fn()} />)
    await screen.findByText(APPROVED.name)

    fireEvent.click(screen.getByTestId(`delete-${APPROVED.appId}`))
    fireEvent.change(screen.getByTestId('admin-delete-reason'), { target: { value: REASON } })
    fireEvent.click(screen.getByTestId('admin-delete-confirm'))

    // A refusal must not close the dialog and throw the typed words away — there is nothing to
    // fix if the text is gone.
    await waitFor(() => expect(h.deleteApp).toHaveBeenCalled())
    expect(screen.getByTestId('admin-delete-reason').value).toBe(REASON)
  })
})
