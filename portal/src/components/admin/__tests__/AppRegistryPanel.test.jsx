// `onToast(message, 'problem')` is the severity AdminPage's shared toast channel
// uses to render a failure differently from a confirmation. Every failure-path
// assertion below carries that second argument; success-path ones don't.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, waitFor, fireEvent, within } from '@testing-library/react'
import AppRegistryPanel from '../AppRegistryPanel.jsx'

const h = vi.hoisted(() => ({
  listApps: vi.fn(),
  approveApp: vi.fn(),
  rejectApp: vi.fn(),
  patchApp: vi.fn(),
  disableApp: vi.fn(),
  enableApp: vi.fn(),
  deleteApp: vi.fn(),
  fetchHistory: vi.fn(),
}))
vi.mock('../../../utils/appRegistryApi', () => h)

import { ApiError } from '../../../utils/apiError'

const SHA = 'f0e1d2c3b4a5f0e1d2c3b4a5f0e1d2c3b4a5f0e1'
const OLDER_SHA = '9a8b7c6d5e4f9a8b7c6d5e4f9a8b7c6d5e4f9a8b'
const LIVE_SHA = '7a3c9e0d1f2a7a3c9e0d1f2a7a3c9e0d1f2a7a3c'

const PENDING = {
  appId: 'app-1',
  name: 'Gate Tool',
  ownerUsername: 'alice@bial.com',
  status: 'pending',
  registryStatus: 'waiting_for_review',
  liveVersion: null,
  loginRequired: false,
  hasApprovedSnapshot: false,
  submissionId: 'sub-1',
  commitSha: SHA,
  submittedAt: '2026-07-16T09:00:00Z',
  declaration: null,
  databaseBytes: null,
  updatedAt: '2026-07-16T09:00:00Z',
}

const APPROVED = {
  ...PENDING,
  appId: 'app-2',
  name: 'Live Tool',
  status: 'approved',
  registryStatus: 'live',
  hasApprovedSnapshot: true,
  // Built from local parts, so the day the row prints is the same in every time zone.
  liveVersion: { number: 4, commitSha: LIVE_SHA, since: new Date(2026, 8, 25, 16, 40).toISOString() },
}

const DRAFT = {
  ...PENDING,
  appId: 'app-4',
  name: 'Self Published Tool',
  status: 'draft',
  registryStatus: 'draft',
  submittedAt: null,
}
const REJECTED = { ...PENDING, appId: 'app-5', name: 'Turned Down Tool', status: 'rejected', registryStatus: 'rejected' }
const DISABLED = { ...PENDING, appId: 'app-6', name: 'Switched Off Tool', status: 'disabled', registryStatus: 'disabled' }

const listOf = (...apps) => ({ apps, truncated: false })

const NO_HISTORY = { entries: [], live: null, liveUrl: null, truncated: false }

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
  h.listApps.mockResolvedValue(listOf(PENDING))
  h.fetchHistory.mockResolvedValue(NO_HISTORY)
})

/** Open the side panel for the one pending row from its name. */
const openReview = async () => {
  fireEvent.click(await screen.findByRole('button', { name: 'Gate Tool' }))
}

/** Open a row's ⋯ menu. */
const openMenu = async (appId) => {
  fireEvent.pointerDown(await screen.findByTestId(`actions-${appId}`))
}

const pickMenuItem = async (name) => {
  fireEvent.click(await screen.findByRole('menuitem', { name }))
}

const menuItems = async () => {
  await screen.findByRole('menu')
  return screen.getAllByRole('menuitem').map((item) => item.textContent.trim())
}

describe('AppRegistryPanel — one list, every app', () => {
  it('loads every app once, with no status filter, and draws the status filters', async () => {
    h.listApps.mockResolvedValue(listOf(PENDING, APPROVED))
    render(<AppRegistryPanel onToast={() => {}} />)
    await screen.findByText('Gate Tool')

    expect(screen.getByText('Live Tool')).toBeTruthy()
    expect(h.listApps).toHaveBeenCalledTimes(1)
    expect(h.listApps).toHaveBeenCalledWith()
    const pills = within(screen.getByRole('group', { name: 'Filter by status' })).getAllByRole('button')
    expect(pills.map((pill) => pill.firstChild.textContent)).toEqual([
      'All', 'Waiting for review', 'Live', 'Not live', 'Draft', 'Rejected', 'Disabled',
    ])
  })

  it('the review modal shows submission METADATA (SHA, submitted-at) and no internal ids', async () => {
    render(<AppRegistryPanel onToast={() => {}} />)
    await openReview()
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
    h.listApps.mockResolvedValue(listOf({ ...PENDING, submittedAt: null }))
    render(<AppRegistryPanel onToast={() => {}} />)
    await openReview()
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
    await openReview()
    fireEvent.click(screen.getByTestId('approve-btn'))
    await waitFor(() => expect(h.approveApp).toHaveBeenCalledWith('app-1', 'sub-1'))
    await waitFor(() => expect(h.listApps).toHaveBeenCalledTimes(2)) // initial + reload
  })

  it('after Approve the reloaded row reads Publishing, with no other page needed', async () => {
    h.approveApp.mockResolvedValue({ status: 'approved' })
    h.listApps
      .mockResolvedValueOnce(listOf(PENDING))
      .mockResolvedValue(listOf({ ...PENDING, status: 'approved', registryStatus: 'publishing' }))
    render(<AppRegistryPanel onToast={() => {}} />)
    await openReview()
    fireEvent.click(screen.getByTestId('approve-btn'))

    const row = screen.getByTestId('row-app-1')
    await waitFor(() => expect(within(row).getByText('Publishing')).toBeTruthy())
    expect(within(row).queryByText('Waiting for review')).toBeNull()
  })

  it('an approve 409 surfaces the re-submitted-since-review copy, not a generic failure', async () => {
    const copy = 'This app was re-submitted since you reviewed it — please re-review.'
    h.approveApp.mockRejectedValue(new Error(copy))
    const onToast = vi.fn()
    render(<AppRegistryPanel onToast={onToast} />)
    await openReview()
    fireEvent.click(screen.getByTestId('approve-btn'))
    await waitFor(() => expect(onToast).toHaveBeenCalledWith(copy, 'problem'))
    // The panel stays OPEN on the 409: act() reports failure, so onApprove never closes it.
    expect(screen.getByTestId('approve-btn')).toBeTruthy()
  })

  it('renders the advisory database size column, human-formatted, and "—" when null', async () => {
    h.listApps.mockResolvedValue(listOf(
      { ...PENDING, appId: 'app-sized', databaseBytes: 2 * 1024 * 1024 },
      { ...PENDING, appId: 'app-null', name: 'No DB', databaseBytes: null },
    ))
    render(<AppRegistryPanel onToast={() => {}} />)
    await screen.findByText('No DB')
    // The backend surfaces AdminAppOut.databaseBytes — the column must actually show it.
    expect(screen.getByTestId('db-bytes-app-sized').textContent).toBe('2.0 MB')
    // Null is "no number to show" (never provisioned / not ready / cluster unreachable), not 0 B.
    expect(screen.getByTestId('db-bytes-app-null').textContent).toBe('—')
  })

  it('toggling login PATCHes the inverse loginRequired', async () => {
    h.patchApp.mockResolvedValue({})
    render(<AppRegistryPanel onToast={() => {}} />)
    await screen.findByText('Gate Tool')
    fireEvent.click(screen.getByTitle('Toggle required login'))
    await waitFor(() => expect(h.patchApp).toHaveBeenCalledWith('app-1', { loginRequired: true }))
  })

  it('shows the cap notice when the server stopped short of every app', async () => {
    h.listApps.mockResolvedValue({ apps: [PENDING], truncated: true })
    render(<AppRegistryPanel onToast={() => {}} />)
    await screen.findByText('Gate Tool')

    expect(screen.getByText(/Only the first 1 rows are loaded/)).toBeTruthy()
  })
})

describe('each row says where the app stands', () => {
  const EVERY_STATUS = [
    { ...PENDING, appId: 's-waiting', name: 'Visitor ID Pass' },
    { ...APPROVED, appId: 's-live', name: 'Lost and Found Register' },
    { ...APPROVED, appId: 's-live-2', name: 'Feedback Form' },
    { ...APPROVED, appId: 's-publishing', name: 'Flight Sales Dashboard', registryStatus: 'publishing', liveVersion: null },
    { ...APPROVED, appId: 's-failed', name: 'Baggage Assistant', registryStatus: 'publish_failed', liveVersion: null },
    { ...APPROVED, appId: 's-unpublished', name: 'Gate Roster', registryStatus: 'not_published', liveVersion: null },
    { ...DRAFT, appId: 's-offline', name: 'Restaurants Finder', registryStatus: 'taken_offline' },
    { ...DRAFT, appId: 's-draft', name: 'Design Tracker' },
    { ...REJECTED, appId: 's-rejected', name: 'Facility Booking' },
    { ...DISABLED, appId: 's-disabled', name: 'Old Kiosk' },
  ]

  it('names every status in the board’s words', async () => {
    h.listApps.mockResolvedValue(listOf(...EVERY_STATUS))
    render(<AppRegistryPanel onToast={() => {}} />)
    await screen.findByText('Old Kiosk')

    const statusOf = (appId) => within(screen.getByTestId(`row-${appId}`)).getAllByRole('cell')[3].textContent
    expect(EVERY_STATUS.map((app) => statusOf(app.appId))).toEqual([
      'Waiting for review', 'Live', 'Live', 'Publishing', 'Publish failed', 'Not published',
      'Taken offline', 'Draft', 'Rejected', 'Disabled',
    ])
  })

  it('counts each filter from the loaded list, and each filter shows exactly its count', async () => {
    h.listApps.mockResolvedValue(listOf(...EVERY_STATUS))
    render(<AppRegistryPanel onToast={() => {}} />)
    await screen.findByText('Old Kiosk')

    const counts = {
      all: 10, waiting_for_review: 1, live: 2, not_live: 4, draft: 1, rejected: 1, disabled: 1,
    }
    for (const [key, count] of Object.entries(counts)) {
      expect(screen.getByTestId(`filter-count-${key}`).textContent).toBe(String(count))
      fireEvent.click(screen.getByTestId(`filter-${key}`))
      expect(screen.getByTestId(`filter-${key}`).getAttribute('aria-pressed')).toBe('true')
      expect(screen.getAllByRole('row')).toHaveLength(count + 1) // + the header row
    }
  })

  it('Not live gathers publishing, failed, never-published and taken-offline apps', async () => {
    h.listApps.mockResolvedValue(listOf(...EVERY_STATUS))
    render(<AppRegistryPanel onToast={() => {}} />)
    await screen.findByText('Old Kiosk')

    fireEvent.click(screen.getByTestId('filter-not_live'))

    const shown = screen.getAllByRole('row').slice(1).map((row) => row.getAttribute('data-testid'))
    expect(shown.sort()).toEqual(['row-s-failed', 'row-s-offline', 'row-s-publishing', 'row-s-unpublished'])
  })

  it('shows the live version’s number, short commit and the day it went live, and a dash when nothing is live', async () => {
    const unnumbered = { ...APPROVED, appId: 'app-3', name: 'Old Tool', liveVersion: { ...APPROVED.liveVersion, number: null } }
    h.listApps.mockResolvedValue(listOf(PENDING, APPROVED, unnumbered))
    render(<AppRegistryPanel onToast={() => {}} />)
    await screen.findByText('Live Tool')

    const liveCell = (appId) => within(screen.getByTestId(`row-${appId}`)).getAllByRole('cell')[4].textContent
    expect(liveCell('app-2')).toBe('v4 · 7a3c9e0since 25 Sep')
    expect(liveCell('app-3')).toBe('7a3c9e0since 25 Sep')
    expect(liveCell('app-1')).toBe('—')
  })

  it('opens sorted by last activity, newest first', async () => {
    h.listApps.mockResolvedValue(listOf(
      { ...APPROVED, appId: 'older', name: 'Older', updatedAt: '2026-09-20T09:00:00Z' },
      { ...APPROVED, appId: 'newer', name: 'Newer', updatedAt: '2026-09-26T09:00:00Z' },
    ))
    render(<AppRegistryPanel onToast={() => {}} />)
    await screen.findByText('Newer')

    expect(screen.getAllByRole('row').slice(1).map((row) => row.getAttribute('data-testid'))).toEqual(['row-newer', 'row-older'])
    expect(screen.getByTestId('sort-updatedAt').closest('th').getAttribute('aria-sort')).toBe('descending')
  })

  it('shows the last activity as day, month and time, and a dash for an app with no declaration', async () => {
    h.listApps.mockResolvedValue(listOf({ ...APPROVED, updatedAt: new Date(2026, 8, 26, 14, 10).toISOString() }))
    render(<AppRegistryPanel onToast={() => {}} />)
    await screen.findByText('Live Tool')

    const cells = within(screen.getByTestId('row-app-2')).getAllByRole('cell')
    expect(cells[6].textContent).toBe('26 Sep, 14:10')
    expect(cells[5].textContent).toBe('—')
  })

  it('reads each row’s classification from its own declaration', async () => {
    const classes = [
      { key: 'pii', title: 'PII', kind: 'hard_block', weight: null },
      { key: 'integrations', title: 'Integrations', kind: 'scored', weight: 20 },
    ]
    const judged = (overrides) => ({
      version: 2,
      classes,
      reviewerAnswers: { pii: false, integrations: false },
      reviewerScore: 0,
      score: 0,
      reason: null,
      ...overrides,
    })
    h.listApps.mockResolvedValue(listOf(
      { ...PENDING, appId: 'blocked', name: 'Visitor ID Pass', declaration: judged({ reviewerAnswers: { pii: true, integrations: false }, reason: 'hard_block' }) },
      { ...APPROVED, appId: 'scored', name: 'Vendor Rates Board', declaration: judged({ reviewerAnswers: { pii: false, integrations: true }, reviewerScore: 60, score: 60 }) },
      { ...APPROVED, appId: 'unfinished', name: 'Half Checked', declaration: judged({ reviewerAnswers: null, reviewerScore: null, score: null, reason: 'review_unfinished' }) },
      { ...APPROVED, appId: 'legacy', name: 'Six Questions', declaration: declaration({ citizen: { ...ALL_NO, personal_information: true } }) },
    ))
    render(<AppRegistryPanel onToast={() => {}} />)
    await screen.findByText('Visitor ID Pass')

    const classification = (appId) => within(screen.getByTestId(`row-${appId}`)).getAllByRole('cell')[5]
    expect(classification('blocked').textContent).toBe('PII')
    expect(classification('scored').textContent).toBe('60/100')
    expect(classification('unfinished').textContent).toBe('—')
    expect(classification('legacy').textContent).toBe('—')
  })

  it('names an owner by the part of their address before the @', async () => {
    h.listApps.mockResolvedValue(listOf({ ...PENDING, ownerUsername: 'meera.k@bial.com' }))
    render(<AppRegistryPanel onToast={() => {}} />)
    await screen.findByText('Gate Tool')

    const owner = within(screen.getByTestId('row-app-1')).getByText('meera.k')
    expect(owner.getAttribute('title')).toBe('meera.k@bial.com')
  })
})

describe('finding one app among many', () => {
  const MANY = [
    ...Array.from({ length: 34 }, (_, i) => ({
      ...PENDING,
      appId: `queued-${i}`,
      name: `Queued App ${i}`,
      ownerUsername: i % 11 === 0 ? 'rahul.kohli@bial.com' : `owner${i % 5}@bial.com`,
    })),
    { ...PENDING, appId: 'baggage', name: 'Baggage Issue Resolution Assistant', ownerUsername: 'suresh.p@bial.com' },
  ]

  it('typing a name finds its one row, and choosing an owner shows only their apps', async () => {
    h.listApps.mockResolvedValue(listOf(...MANY))
    render(<AppRegistryPanel onToast={() => {}} />)
    await screen.findByText('Queued App 0')
    expect(screen.getByText('Showing 1–10 of 35')).toBeTruthy()

    const search = screen.getByRole('searchbox', { name: 'Search apps' })
    fireEvent.change(search, { target: { value: 'baggage' } })
    expect(screen.getAllByRole('row').slice(1).map((row) => row.getAttribute('data-testid'))).toEqual(['row-baggage'])

    fireEvent.change(search, { target: { value: '' } })
    fireEvent.click(screen.getByTestId('owner-filter'))
    fireEvent.click(await screen.findByRole('option', { name: 'rahul.kohli' }))

    await waitFor(() => expect(screen.getByText('Showing 1–4 of 4')).toBeTruthy())
    const owners = screen.getAllByRole('row').slice(1).map((row) => within(row).getAllByRole('cell')[1].textContent)
    expect(owners).toEqual(['rahul.kohli', 'rahul.kohli', 'rahul.kohli', 'rahul.kohli'])
  })

  it('search matches the owner too', async () => {
    h.listApps.mockResolvedValue(listOf(...MANY))
    render(<AppRegistryPanel onToast={() => {}} />)
    await screen.findByText('Queued App 0')

    fireEvent.change(screen.getByRole('searchbox', { name: 'Search apps' }), { target: { value: 'suresh' } })

    expect(screen.getAllByRole('row').slice(1).map((row) => row.getAttribute('data-testid'))).toEqual(['row-baggage'])
  })
})

describe('the row menu', () => {
  it('Open on a waiting app opens its panel on Review', async () => {
    render(<AppRegistryPanel onToast={() => {}} />)
    await openMenu('app-1')
    await pickMenuItem('Open')

    expect(await screen.findByTestId('approve-btn')).toBeTruthy()
    expect(screen.getByRole('tab', { name: 'Review' }).getAttribute('aria-selected')).toBe('true')
  })

  it('Open on any other app opens its panel on History alone', async () => {
    h.listApps.mockResolvedValue(listOf(APPROVED))
    render(<AppRegistryPanel onToast={() => {}} />)
    await openMenu('app-2')
    await pickMenuItem('Open')

    const panel = await screen.findByRole('dialog', { name: 'Live Tool' })
    expect(within(panel).getByRole('heading', { name: 'History' })).toBeTruthy()
    expect(within(panel).queryByRole('tab')).toBeNull()
    expect(within(panel).queryByTestId('approve-btn')).toBeNull()
    expect(h.fetchHistory).toHaveBeenCalledWith('app-2')
  })

  it('a click anywhere on a row opens its panel, and the row stays highlighted while it is open', async () => {
    h.listApps.mockResolvedValue(listOf(APPROVED, { ...APPROVED, appId: 'app-9', name: 'Other Tool' }))
    render(<AppRegistryPanel onToast={() => {}} />)
    const row = await screen.findByTestId('row-app-2')
    expect(row.getAttribute('aria-current')).toBeNull()

    fireEvent.click(within(row).getAllByRole('cell')[1])

    expect(await screen.findByRole('dialog', { name: 'Live Tool' })).toBeTruthy()
    expect(row.getAttribute('aria-current')).toBe('true')
    expect(screen.getByTestId('row-app-9').getAttribute('aria-current')).toBeNull()
  })

  it('the row’s own controls, and its menu’s items, do not open its panel', async () => {
    h.listApps.mockResolvedValue(listOf(APPROVED))
    h.patchApp.mockResolvedValue({})
    h.disableApp.mockResolvedValue({})
    render(<AppRegistryPanel onToast={() => {}} />)
    await screen.findByText('Live Tool')

    fireEvent.click(screen.getByTitle('Toggle required login'))
    fireEvent.click(screen.getByTestId('actions-app-2'))
    await openMenu('app-2')
    await pickMenuItem('Disable')

    // Liveness: each control really acted, so no panel means the row held back.
    await waitFor(() => expect(h.patchApp).toHaveBeenCalled())
    await waitFor(() => expect(h.disableApp).toHaveBeenCalledWith('app-2'))
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('Escape closes the panel and puts focus back on the row that opened it', async () => {
    h.listApps.mockResolvedValue(listOf(APPROVED))
    render(<AppRegistryPanel onToast={() => {}} />)
    const row = await screen.findByTestId('row-app-2')
    fireEvent.click(within(row).getAllByRole('cell')[1])
    const panel = await screen.findByRole('dialog', { name: 'Live Tool' })

    fireEvent.keyDown(panel, { key: 'Escape' })

    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(row.getAttribute('aria-current')).toBeNull()
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Live Tool' }))
  })

  it('an enabled app offers Disable, a disabled one Enable, and a waiting one neither', async () => {
    // The server widened `STATUS_TRANSITIONS[DISABLED]` to {approved, draft, rejected}; pending is
    // excluded on both sides — an app in the review queue is rejected, not switched off — so an
    // item there would only ever produce a 409.
    h.listApps.mockResolvedValue(listOf(PENDING, DRAFT, REJECTED, APPROVED, DISABLED))
    render(<AppRegistryPanel onToast={() => {}} />)

    const itemsFor = async (appId) => {
      await openMenu(appId)
      const items = await menuItems()
      fireEvent.keyDown(screen.getByRole('menu'), { key: 'Escape' })
      await waitFor(() => expect(screen.queryByRole('menu')).toBeNull())
      return items
    }
    expect(await itemsFor('app-1')).toEqual(['Open', 'Delete'])
    expect(await itemsFor('app-4')).toEqual(['Open', 'Disable', 'Delete'])
    expect(await itemsFor('app-5')).toEqual(['Open', 'Disable', 'Delete'])
    expect(await itemsFor('app-2')).toEqual(['Open', 'Disable', 'Delete'])
    expect(await itemsFor('app-6')).toEqual(['Open', 'Enable', 'Delete'])
  })

  it('Disable calls the API for that app, confirms by name, and reloads', async () => {
    h.listApps.mockResolvedValue(listOf(DRAFT))
    h.disableApp.mockResolvedValue({ status: 'disabled' })
    const onToast = vi.fn()
    render(<AppRegistryPanel onToast={onToast} />)
    await openMenu('app-4')
    await pickMenuItem('Disable')

    await waitFor(() => expect(h.disableApp).toHaveBeenCalledWith('app-4'))
    // A bare confirmation, not a failure-severity toast — and it names the app, so an
    // administrator with several rows on screen can see which one they just switched off.
    expect(onToast).toHaveBeenCalledWith('“Self Published Tool” disabled')
    await waitFor(() => expect(h.listApps).toHaveBeenCalledTimes(2))
  })

  it('Enable calls the API for that app', async () => {
    h.listApps.mockResolvedValue(listOf(DISABLED))
    h.enableApp.mockResolvedValue({ status: 'approved' })
    render(<AppRegistryPanel onToast={() => {}} />)
    await openMenu('app-6')
    await pickMenuItem('Enable')

    await waitFor(() => expect(h.enableApp).toHaveBeenCalledWith('app-6'))
  })

  it('an approved app carries no deploy prompt and no way to record a deployment by hand', async () => {
    h.listApps.mockResolvedValue(listOf(APPROVED))
    render(<AppRegistryPanel onToast={() => {}} />)
    await openMenu('app-2')

    expect(await menuItems()).toEqual(['Open', 'Disable', 'Delete'])
    expect(screen.getByTestId('row-app-2').textContent).not.toMatch(/deploy needed/i)
  })
})

describe('the review screen leads with the dispute', () => {
  it('shows the disputed categories, their reasons, and the explanation IN THAT ORDER', async () => {
    h.listApps.mockResolvedValue(listOf({
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
    }))
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
    h.listApps.mockResolvedValue(listOf({
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
    }))
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
    h.listApps.mockResolvedValue(listOf({
      ...PENDING,
      declaration: declaration({
        reviewed: null,
        citizen: { ...ALL_NO, personal_information: true },
        reviewAnswers: {},
        merged: { ...ALL_NO, personal_information: true },
        differences: {},
      }),
    }))
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
    h.listApps.mockResolvedValue(listOf({ ...PENDING, declaration: null }))
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
    h.listApps.mockResolvedValue(listOf({
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
    }))
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
    h.listApps.mockResolvedValue(listOf({
      ...PENDING,
      declaration: declaration({ shipping: SHA, reviewed: OLDER_SHA, citizen: ALL_NO }),
    }))
    render(<AppRegistryPanel onToast={() => {}} />)
    await openReview()

    expect(screen.queryByTestId('review-drift')).toBeNull()
    // Paired liveness: the modal really did render, so the absence above is a decision
    // rather than a component that threw.
    expect(screen.getByTestId('review-explanation')).toBeTruthy()
  })

  it('does NOT cry drift when the reviewed and shipping commits are the same', async () => {
    h.listApps.mockResolvedValue(listOf({
      ...PENDING,
      declaration: declaration({
        citizen: ALL_NO,
        reviewAnswers: { credentials_secrets: 'yes' },
        merged: { ...ALL_NO, credentials_secrets: true },
        differences: { credentials_secrets: ['review_yes_over_citizen_no'] },
      }),
    }))
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
    h.listApps.mockResolvedValue(listOf({ ...PENDING, declaration: everything }))
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
    expect(scroller.parentElement.contains(approve)).toBe(true)
    expect(scroller.className).toMatch(/overflow-y-auto/)
    expect(scroller.className).toMatch(/min-h-0/)
    expect(scroller.parentElement.className).toMatch(/min-h-0/)
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

describe('a submission withdrawn while the panel was open', () => {
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
  it('dismissing it returns focus to the row that opened it', async () => {
    render(<AppRegistryPanel onToast={() => {}} />)
    await openReview()
    // LIVENESS FIRST: the panel really opened, so "it is gone" below is a close rather than an
    // assertion that ran before anything rendered.
    expect(screen.getByTestId('approve-btn')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Close' }))

    await waitFor(() => expect(screen.queryByTestId('approve-btn')).toBeNull())
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Gate Tool' }))
  })

  it('approving it under the Waiting for review filter, which takes the row away, lands focus on that filter', async () => {
    // THE OPENER IS DESTROYED BY ITS OWN SUCCESS. An approved app is no longer waiting, so the
    // reload takes its row out of the filtered list: restoring to it would focus a detached node
    // and silently do nothing, which is `<body>` again.
    h.approveApp.mockResolvedValue({ status: 'approved' })
    h.listApps
      .mockResolvedValueOnce(listOf(PENDING))
      .mockResolvedValue(listOf({ ...PENDING, status: 'approved', registryStatus: 'publishing' }))
    const onToast = vi.fn()
    render(<AppRegistryPanel onToast={onToast} />)
    await screen.findByText('Gate Tool')
    fireEvent.click(screen.getByTestId('filter-waiting_for_review'))
    await openReview()

    fireEvent.click(screen.getByTestId('approve-btn'))

    // LIVENESS: the approve really went through and the list really reloaded — not a component
    // that threw somewhere between the two.
    await waitFor(() => expect(onToast).toHaveBeenCalledWith('“Gate Tool” approved'))
    await waitFor(() => expect(h.listApps).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(screen.queryByTestId('approve-btn')).toBeNull())
    expect(screen.queryByRole('button', { name: 'Gate Tool' })).toBeNull()

    expect(document.activeElement).toBe(screen.getByTestId('filter-waiting_for_review'))
  })
})


describe('approving publishes, and the review says so', () => {
  it('a submission is approved and published by one button', async () => {
    h.listApps.mockResolvedValue(listOf(PENDING))
    render(<AppRegistryPanel onToast={() => {}} />)
    await openReview()

    expect(screen.getByTestId('approve-btn').textContent).toContain('Approve and publish')
    expect(screen.getByTestId('review-publish-note').textContent).toBe(`Approving publishes exactly ${SHA.slice(0, 7)}.`)
    // Nothing left over from the manual route or the developer's second click.
    expect(document.body.textContent).not.toMatch(/go-live runbook/i)
    expect(document.body.textContent).not.toMatch(/publishes this approved version themselves/i)
  })
})

describe('internal identifiers stay out of the administrator’s way', () => {
  it('names the app in the table without its internal id', async () => {
    render(<AppRegistryPanel onToast={() => {}} />)
    await screen.findByText('Gate Tool')
    // Liveness first: the row is genuinely rendered, so the absence below is meaningful.
    expect(screen.getByTestId('row-app-1')).toBeTruthy()
    expect(screen.queryByText('app-1')).toBeNull()
  })

  it('falls back to a readable name, never to the id, for an untitled app', async () => {
    h.listApps.mockResolvedValue(listOf({ ...PENDING, name: null }))
    render(<AppRegistryPanel onToast={() => {}} />)
    expect(await screen.findByText('(untitled app)')).toBeTruthy()
    expect(screen.queryByText('app-1')).toBeNull()
  })
})

describe('★ the admin delete collects a reason', () => {
  // A `window.confirm` stood here and could collect nothing, while the route already REQUIRED a
  // word-bounded justification — so every delete through this panel answered 422. It shipped green
  // because `deleteApp` is mocked wholesale in this file: both halves passed while disagreeing.
  // These tests assert what the panel actually hands the client.
  const REASON = 'Duplicate app created in error during onboarding, owner asked for removal'

  const openDelete = async () => {
    await openMenu(APPROVED.appId)
    await pickMenuItem('Delete')
  }

  it('will not delete until the reason meets the shared word rule', async () => {
    h.listApps.mockResolvedValue(listOf(APPROVED))
    render(<AppRegistryPanel onToast={vi.fn()} />)
    await openDelete()
    const confirm = await screen.findByTestId('admin-delete-confirm')

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
    h.listApps.mockResolvedValue(listOf(APPROVED))
    h.deleteApp.mockResolvedValue({ ok: true })
    render(<AppRegistryPanel onToast={vi.fn()} />)
    await openDelete()

    fireEvent.change(await screen.findByTestId('admin-delete-reason'), { target: { value: REASON } })
    fireEvent.click(screen.getByTestId('admin-delete-confirm'))

    // ★ THE ASSERTION THAT WOULD HAVE CAUGHT THE BREAK: the reason is the second argument.
    await waitFor(() => expect(h.deleteApp).toHaveBeenCalledWith(APPROVED.appId, REASON))
  })

  it('★ Escape closes it and the keyboard lands back on the menu that opened it', async () => {
    // It was hand-rolled — a `fixed inset-0` div with `role="dialog"` and nothing else — so
    // Escape did nothing, Tab walked straight out of it, and closing it dropped focus on the
    // document body. It opens from a menu item that is gone by the time it closes, so the
    // panel puts focus back on the row's menu button itself.
    h.listApps.mockResolvedValue(listOf(APPROVED))
    render(<AppRegistryPanel onToast={vi.fn()} />)
    await openDelete()
    const trigger = screen.getByTestId(`actions-${APPROVED.appId}`)
    const field = await screen.findByTestId('admin-delete-reason')
    // LIVENESS: it really opened and really took focus off the menu, so the restore below is
    // a restore rather than focus that never moved.
    expect(field).toBeTruthy()
    expect(document.activeElement).not.toBe(trigger)

    fireEvent.keyDown(document.activeElement || document.body, { key: 'Escape' })

    await waitFor(() => expect(screen.queryByTestId('admin-delete-reason')).toBeNull())
    await waitFor(() => expect(document.activeElement).toBe(trigger))
  })

  it('keeps the words on screen when the server refuses them', async () => {
    h.listApps.mockResolvedValue(listOf(APPROVED))
    h.deleteApp.mockRejectedValue(new Error('Say why in 2 to 50 words.'))
    render(<AppRegistryPanel onToast={vi.fn()} />)
    await openDelete()

    fireEvent.change(await screen.findByTestId('admin-delete-reason'), { target: { value: REASON } })
    fireEvent.click(screen.getByTestId('admin-delete-confirm'))

    // A refusal must not close the dialog and throw the typed words away — there is nothing to
    // fix if the text is gone.
    await waitFor(() => expect(h.deleteApp).toHaveBeenCalled())
    expect(screen.getByTestId('admin-delete-reason').value).toBe(REASON)
  })
})
