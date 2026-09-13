/**
 * The administrator's connector queue — the tab, both tables, both empty states, and the two
 * things on this screen that a hard-coded string would fake convincingly.
 *
 * THE FIXTURE CONNECTORS ARE NOT THE REAL ONE, and that is the point rather than a convenience.
 * Every connector string this panel renders comes off the wire (R18), so the suite invents
 * `ORBIT` and `ATLAS`. A panel with a connector's name compiled into it would still pass a test
 * that asserted that name; this one could not tell the difference.
 *
 * THE FIXTURES ARRIVE OUT OF ORDER ON PURPOSE. The board itself draws its waiting rows 4 Sep,
 * 3 Sep, 5 Sep, and the server's ordering guarantee is not something a TanStack table inherits —
 * it renders the array it was handed until a sorting state says otherwise. Feeding scrambled
 * arrays is the only way the "default order is the board's" test can fail when that state is
 * dropped.
 *
 * LIVENESS EVERYWHERE AN ABSENCE IS ASSERTED. Four tests here are about something NOT being on
 * screen — a missing badge, a cut clause, two empty tables that must not appear under an error,
 * and a row that must not grow a third line — and a component that crashed on mount would
 * satisfy every one of them. Each is paired with a positive assertion, because this repo has
 * been bitten by exactly that.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

const h = vi.hoisted(() => ({
  listConnectorRequests: vi.fn(),
  fetchWaitingConnectorCount: vi.fn(),
  approveConnectorRequest: vi.fn(),
  declineConnectorRequest: vi.fn(),
  getStoredUser: vi.fn(),
}))

vi.mock('../../../utils/adminConnectorApi', () => ({
  listConnectorRequests: h.listConnectorRequests,
  fetchWaitingConnectorCount: h.fetchWaitingConnectorCount,
  // The decide dialog this panel now mounts reaches for both. Named here so a stray call from a
  // test in this file is a mock assertion rather than vitest's "no export is defined" throw —
  // the decision's own behaviour is covered in `ConnectorReviewDialog.test.tsx`.
  approveConnectorRequest: h.approveConnectorRequest,
  declineConnectorRequest: h.declineConnectorRequest,
}))
vi.mock('../../../utils/auth', () => ({ getStoredUser: h.getStoredUser }))

// The console's other four panels and its navbar are stubbed for the whole-page tests below:
// this file is about the Integrations tab, and mounting the app registry (which fetches on
// mount) would put an unrelated failure on screen beside the thing under test.
vi.mock('../../layout/Navbar', () => ({ default: () => <nav data-testid="navbar" /> }))
vi.mock('../AppRegistryPanel', () => ({ default: () => <div data-testid="apps-panel" /> }))
vi.mock('../UsersLimitsPanel', () => ({ default: () => <div data-testid="users-panel" /> }))
vi.mock('../GlobalLimitsPanel', () => ({ default: () => <div data-testid="limits-panel" /> }))
vi.mock('../FeedbackPanel', () => ({ default: () => <div data-testid="feedback-panel" /> }))

import IntegrationsPanel from '../IntegrationsPanel'
import WaitingCountBadge from '../WaitingCountBadge'
import AdminPage from '../../../pages/AdminPage'
import type { ConnectorRequestRow } from '../../../utils/adminConnectorApi'
import { ApiError } from '../../../utils/apiError'

/** The signed-in administrator, and the other one. BIAL runs two. */
const ME = '0199a1f0-0000-7000-8000-00000000000a'
const THE_OTHER_ADMIN = '0199a1f0-0000-7000-8000-00000000000b'

const PRIYA_REMARK =
  'I build the stand and turnaround boards for ground ops. All of them need on-block and off-block times.'

/**
 * `WHAT APPROVING GIVES THEM`, as the wire carries it — three `{lead, body}` pairs, never
 * pre-joined. Invented like the connectors above, and for the same reason: a component that
 * spelled the real registry's sentences would still pass a test that asserted the real ones.
 */
const CONSENT = [
  { lead: 'Read access to the movements feed.', body: 'and nothing else in this system.' },
  { lead: 'Every project they own.', body: 'including ones they have not made yet.' },
  { lead: 'Up to 30 days of history while they build.', body: 'each project picks its own range.' },
]

/**
 * Midday UTC, deliberately: every timezone this product is read in still calls it the same day,
 * so the DATE half of every assertion below is stable while the clock half stays local.
 */
const waiting: ConnectorRequestRow[] = [
  {
    id: 'req-priya',
    userId: 'user-priya',
    displayName: 'Priya Nair',
    email: 'priya.nair@bial.aero',
    connectorKey: 'orbit',
    connectorDisplayName: 'ORBIT',
    consentLinesApprover: CONSENT,
    requesterRemarks: PRIYA_REMARK,
    askedAt: '2026-09-04T12:00:00.000Z',
    status: 'pending',
    decidedAt: null,
    decidedById: null,
    decidedByName: null,
    decisionRemarks: null,
    usingItIn: null,
  },
  {
    id: 'req-sam',
    userId: 'user-sam',
    displayName: 'Sam Fernandes',
    // The long address the `department` column was never drawn for.
    email: 'sam.fernandes.terminal.duty.manager@bial.aero',
    connectorKey: 'orbit',
    connectorDisplayName: 'ORBIT',
    consentLinesApprover: CONSENT,
    requesterRemarks:
      'The duty manager’s delay board. Without the live schedule it is a spreadsheet somebody retypes each shift.',
    askedAt: '2026-09-03T12:00:00.000Z',
    status: 'pending',
    decidedAt: null,
    decidedById: null,
    decidedByName: null,
    decisionRemarks: null,
    usingItIn: null,
  },
  {
    id: 'req-divya',
    userId: 'user-divya',
    displayName: 'Divya Shetty',
    email: 'divya.shetty@bial.aero',
    connectorKey: 'atlas',
    connectorDisplayName: 'ATLAS',
    consentLinesApprover: CONSENT,
    requesterRemarks: 'Belt planning — how many arriving flights land on each belt per hour, a day ahead.',
    askedAt: '2026-09-05T12:00:00.000Z',
    status: 'pending',
    decidedAt: null,
    decidedById: null,
    decidedByName: null,
    decisionRemarks: null,
    usingItIn: null,
  },
]

const decided: ConnectorRequestRow[] = [
  {
    id: 'req-meera',
    userId: 'user-meera',
    displayName: 'Meera Rao',
    email: 'meera.rao@bial.aero',
    connectorKey: 'orbit',
    connectorDisplayName: 'ORBIT',
    consentLinesApprover: CONSENT,
    requesterRemarks: 'Airside safety walk-arounds.',
    askedAt: '2026-08-26T12:00:00.000Z',
    status: 'approved',
    decidedAt: '2026-08-27T12:00:00.000Z',
    decidedById: ME,
    decidedByName: 'Anita Rao',
    decisionRemarks: null,
    usingItIn: 1,
  },
  {
    id: 'req-anant',
    userId: 'user-anant',
    displayName: 'Anant Gupta',
    email: 'anant.gupta@bial.aero',
    connectorKey: 'orbit',
    connectorDisplayName: 'ORBIT',
    consentLinesApprover: CONSENT,
    requesterRemarks: 'Terminal systems dashboards.',
    askedAt: '2026-09-01T12:00:00.000Z',
    status: 'approved',
    // 13:00Z and 11:00Z below are the same calendar day everywhere this is read, and still sort
    // apart — so `2 Sep` can appear on two rows without the order test depending on a tie-break.
    decidedAt: '2026-09-02T13:00:00.000Z',
    decidedById: ME,
    decidedByName: 'Anita Rao',
    decisionRemarks: null,
    usingItIn: 4,
  },
  {
    id: 'req-rakesh',
    userId: 'user-rakesh',
    displayName: 'Rakesh Iyer',
    email: 'rakesh.iyer@bial.aero',
    connectorKey: 'atlas',
    connectorDisplayName: 'ATLAS',
    consentLinesApprover: CONSENT,
    requesterRemarks: 'Retail footfall by hour.',
    askedAt: '2026-09-01T12:00:00.000Z',
    status: 'declined',
    decidedAt: '2026-09-02T11:00:00.000Z',
    decidedById: THE_OTHER_ADMIN,
    decidedByName: 'Rahul Menon',
    decisionRemarks: 'Nothing you have built needs operational data yet.',
    usingItIn: null,
  },
]

/** The mock server: honours `connector`, ignores `q` — the client-side narrowing has to be able
 *  to work on rows the server did NOT narrow, or the test proves nothing about it. */
const serve = (waitingRows = waiting, decidedRows = decided): void => {
  h.listConnectorRequests.mockImplementation(
    async (state: string, filters?: { connector?: string | null }) => {
      const rows = state === 'waiting' ? waitingRows : decidedRows
      const key = filters?.connector
      return {
        requests: key ? rows.filter((row) => row.connectorKey === key) : rows,
        truncated: false,
      }
    },
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  h.getStoredUser.mockReturnValue({ id: ME, isAdmin: true })
  h.fetchWaitingConnectorCount.mockResolvedValue(3)
  serve()
})
afterEach(() => cleanup())

const onToast = vi.fn()
const openPanel = () => render(<IntegrationsPanel onToast={onToast} />)

const openConsole = () => render(<AdminPage />, { wrapper: MemoryRouter })

/** The ids of the rows one table is currently showing, top to bottom. */
const rowOrder = (kind: 'waiting' | 'decided'): string[] =>
  within(screen.getByTestId(`queue-table-${kind}`))
    .getAllByTestId(/^queue-row-/)
    .map((row) => row.getAttribute('data-testid') ?? '')

describe('the tab, the badge and both tables', () => {
  it('puts Integrations SECOND, badges it with the server’s count, and renders both tables', async () => {
    openConsole()

    // The badge is fetched by the page, so wait for it before snapshotting the strip — a strip
    // read one tick early would record the tab without its badge and pass either way.
    await screen.findByTestId('waiting-count-integrations-tab')
    const strip = screen.getByRole('button', { name: 'App Registry' }).parentElement
    expect(strip).not.toBeNull()
    expect([...(strip?.children ?? [])].map((tab) => tab.textContent)).toEqual([
      'App Registry',
      // The badge's own accessible sentence rides inside the tab's text — see the
      // non-regression test at the bottom for the shape of it.
      'Integrations33 people waiting for access',
      'Users & Limits',
      'Global Limits',
      'Feedback',
    ])

    fireEvent.click(screen.getByRole('button', { name: /^Integrations/ }))

    expect(await screen.findByTestId('queue-table-waiting')).toBeTruthy()
    expect(screen.getByTestId('queue-table-decided')).toBeTruthy()
    expect(h.fetchWaitingConnectorCount).toHaveBeenCalledTimes(1)
  })

  it('renders each waiting person’s remark IN FULL, not truncated to a tooltip', async () => {
    openPanel()

    expect(await screen.findByText(PRIYA_REMARK)).toBeTruthy()
  })
})

describe('the default order is the board’s', () => {
  it('THE MUTANT: waiting is oldest first and decided is newest first, before any header is clicked', async () => {
    openPanel()
    await screen.findByTestId('queue-table-waiting')

    // The fixtures arrive 4 Sep / 3 Sep / 5 Sep and 27 Aug / 2 Sep 13:00 / 2 Sep 11:00. Drop the
    // panel's initial sorting state and the tables render those arrays as they came, which is
    // neither of the orders below.
    expect(rowOrder('waiting')).toEqual([
      'queue-row-req-sam',
      'queue-row-req-priya',
      'queue-row-req-divya',
    ])
    expect(rowOrder('decided')).toEqual([
      'queue-row-req-anant',
      'queue-row-req-rakesh',
      'queue-row-req-meera',
    ])
  })

  it('clicking a sortable header reorders THAT table and leaves the other where it was', async () => {
    openPanel()
    const waitingTable = await screen.findByTestId('queue-table-waiting')
    const decidedBefore = rowOrder('decided')

    fireEvent.click(within(waitingTable).getByTestId('sort-person'))

    // By name, ascending — a different order from the date one above, so the click did something.
    expect(rowOrder('waiting')).toEqual([
      'queue-row-req-divya',
      'queue-row-req-priya',
      'queue-row-req-sam',
    ])
    expect(rowOrder('decided')).toEqual(decidedBefore)
  })
})

describe('the decided table', () => {
  it('shows the right pill and project count for an approval, and an em dash for a decline', async () => {
    openPanel()
    const table = await screen.findByTestId('queue-table-decided')

    const approved = within(table).getByTestId('queue-row-req-anant')
    expect(within(approved).getByText('Approved')).toBeTruthy()
    expect(within(approved).getByText('4 projects')).toBeTruthy()

    // One project, singular — "1 projects" is the kind of thing that makes a person trust the
    // rest of an authorization screen slightly less.
    expect(within(within(table).getByTestId('queue-row-req-meera')).getByText('1 project')).toBeTruthy()

    const declined = within(table).getByTestId('queue-row-req-rakesh')
    expect(within(declined).getByText('Declined')).toBeTruthy()
    expect(within(declined).getByText('—')).toBeTruthy()
  })

  it('THE MUTANT: WHEN reads `you` only on the signed-in administrator’s OWN decisions', async () => {
    openPanel()
    const table = await screen.findByTestId('queue-table-decided')

    // Same date on both rows, so the only thing that can differ is who decided. Hard-code `you`
    // and the second assertion goes red; drop the id comparison entirely and the first does.
    expect(within(within(table).getByTestId('queue-row-req-anant')).getByText('2 Sep · you')).toBeTruthy()
    expect(
      within(within(table).getByTestId('queue-row-req-rakesh')).getByText('2 Sep · Rahul Menon'),
    ).toBeTruthy()
  })

  it('keeps the date and drops the separator when the administrator who decided has been deleted', async () => {
    serve(waiting, [{ ...decided[2], decidedById: null, decidedByName: null }])
    openPanel()
    const table = await screen.findByTestId('queue-table-decided')

    expect(within(table).getByText('2 Sep')).toBeTruthy()
  })
})

describe('both empty states', () => {
  it('renders the two authored sentences when nobody is waiting and nothing has been decided', async () => {
    serve([], [])
    openPanel()

    expect(await screen.findByText('Nobody is waiting on a decision')).toBeTruthy()
    expect(screen.getByText('No decisions yet')).toBeTruthy()
    // Liveness: the panel really rendered, so the absence below is an absence and not a crash.
    expect(screen.getByPlaceholderText('Search people…')).toBeTruthy()
    expect(screen.queryByTestId('queue-table-waiting')).toBeNull()
  })

  it('shows no badge at all when nobody is waiting', async () => {
    h.fetchWaitingConnectorCount.mockResolvedValue(0)
    openConsole()

    fireEvent.click(screen.getByRole('button', { name: 'Integrations' }))
    // Positive first, and it is also the wait: the queue behind the tab is on screen, which
    // takes more awaited work than the count did — so a missing badge here is a badge that was
    // fetched and rendered as nothing, not one that had yet to arrive.
    expect(await screen.findByTestId('queue-table-waiting')).toBeTruthy()
    expect(h.fetchWaitingConnectorCount).toHaveBeenCalledTimes(1)
    expect(screen.queryByTestId('waiting-count-integrations-tab')).toBeNull()
  })
})

describe('the filter pills and the people search', () => {
  it('are a single-select ToggleGroup with exactly one pressed item, built from the wire', async () => {
    openPanel()
    await screen.findByTestId('queue-table-waiting')

    // shadcn's ToggleGroup with `type="single"` IS a radiogroup underneath — which is what the
    // earlier hand-written `role="radiogroup"` would have re-implemented.
    const group = screen.getByRole('radiogroup', { name: 'Filter by connector' })
    const pills = within(group).getAllByRole('radio')
    expect(pills.map((pill) => pill.textContent)).toEqual(['All connectors', 'ATLAS', 'ORBIT'])
    expect(pills.filter((pill) => pill.getAttribute('aria-checked') === 'true')).toHaveLength(1)
    expect(within(group).getByRole('radio', { name: 'All connectors' }).getAttribute('aria-checked')).toBe('true')
  })

  it('a pill narrows BOTH tables, through the server', async () => {
    openPanel()
    await screen.findByTestId('queue-table-waiting')

    fireEvent.click(screen.getByRole('radio', { name: 'ATLAS' }))

    await waitFor(() =>
      expect(h.listConnectorRequests).toHaveBeenCalledWith('waiting', { connector: 'atlas', q: null }),
    )
    await waitFor(() => expect(rowOrder('waiting')).toEqual(['queue-row-req-divya']))
    expect(rowOrder('decided')).toEqual(['queue-row-req-rakesh'])
    // …and pressing it did not delete the pills it filtered away.
    expect(within(screen.getByRole('radiogroup')).getAllByRole('radio')).toHaveLength(3)
  })

  it('typing narrows BOTH tables on screen, and then reaches the server as `q`', async () => {
    openPanel()
    await screen.findByTestId('queue-table-waiting')

    fireEvent.change(screen.getByPlaceholderText('Search people…'), { target: { value: 'nair' } })

    // Immediately, with no round trip — the mock server ignores `q` and still returns everybody.
    expect(rowOrder('waiting')).toEqual(['queue-row-req-priya'])
    expect(screen.getByText('1 person wants access to a connector')).toBeTruthy()
    // The other table narrowed too, to nothing — and says so in its own words rather than
    // claiming the administrator is caught up.
    expect(screen.queryByTestId('queue-table-decided')).toBeNull()
    expect(screen.getByText('No decisions match “nair”')).toBeTruthy()

    // …and the same text settles into the server's own filter, which is what bounds the 200-row cap.
    await waitFor(() =>
      expect(h.listConnectorRequests).toHaveBeenCalledWith('waiting', { connector: null, q: 'nair' }),
    )
  })
})

describe('the work email in place of the board’s department', () => {
  it('truncates with an ellipsis, carries the full value in title AND aria-label, and stays two lines', async () => {
    openPanel()
    await screen.findByTestId('queue-table-waiting')

    const email = screen.getByTestId('queue-email-req-sam')
    const full = 'sam.fernandes.terminal.duty.manager@bial.aero'
    expect(email.textContent).toBe(full)
    expect(email.getAttribute('title')).toBe(full)
    expect(email.getAttribute('aria-label')).toBe(full)
    // jsdom computes no layout, so "truncated with an ellipsis" and "never wraps to a third
    // line" are asserted as the classes that produce them plus the shape of the stack: a name
    // and an address, and nothing that could become a third row.
    expect(email.className).toContain('truncate')
    expect(email.className).toContain('whitespace-nowrap')
    const stack = email.parentElement
    expect(stack?.children).toHaveLength(2)
    expect(stack?.firstElementChild?.textContent).toBe('Sam Fernandes')
  })
})

describe('the closing paragraph', () => {
  it('keeps the board’s sentences and drops the clause about remarks on both sides', async () => {
    openPanel()
    await screen.findByTestId('queue-table-waiting')

    // Positive first: the paragraph is on screen and ends where it should.
    expect(
      screen.getByText(/Every decision is written to the audit log with your name\.$/),
    ).toBeTruthy()
    expect(screen.getByText(/Withdrawing it stops their chats and their published apps/)).toBeTruthy()
    // There is no approval remark at all, so the board's final clause would promise a record
    // that is never written.
    expect(screen.queryByText(/remarks on both sides/)).toBeNull()
  })
})

describe('a load that fails', () => {
  it('shows the error and a retry, never two empty tables reading as caught up', async () => {
    h.listConnectorRequests.mockRejectedValue(
      new ApiError('Super-admin privileges required.', 403),
    )
    openPanel()

    expect(await screen.findByText('Super-admin privileges required.')).toBeTruthy()
    expect(screen.getByRole('button', { name: /Retry/ })).toBeTruthy()
    // The two sentences that would tell an administrator they are caught up are NOT on screen —
    // paired with the positive above, so a crashed render cannot pass this.
    expect(screen.queryByText('Nobody is waiting on a decision')).toBeNull()
    expect(screen.queryByText('No decisions yet')).toBeNull()

    serve()
    fireEvent.click(screen.getByRole('button', { name: /Retry/ }))
    expect(await screen.findByTestId('queue-table-waiting')).toBeTruthy()
  })
})

describe('the Review control opens the decision dialog', () => {
  it('opens it on the pressed row, and on no other', async () => {
    openPanel()
    await screen.findByTestId('queue-table-waiting')

    // Not open until asked: paired with the positive below so a dialog that failed to mount
    // cannot pass this.
    expect(screen.queryByTestId('connector-review-dialog')).toBeNull()

    fireEvent.click(screen.getByTestId('review-req-priya'))

    const dialog = within(screen.getByTestId('connector-review-dialog'))
    expect(dialog.getByText('Give Priya Nair access to ORBIT?')).toBeTruthy()
    // The row that was pressed, not the first waiting row on screen — the two differ here,
    // because the default order puts Sam on top.
    expect(dialog.queryByText(/Sam Fernandes/)).toBeNull()
    expect(dialog.getByText(new RegExp(PRIYA_REMARK.slice(0, 30)))).toBeTruthy()
  })

  it('closes on Cancel with nothing decided and no reload', async () => {
    openPanel()
    await screen.findByTestId('queue-table-waiting')
    const loadsBefore = h.listConnectorRequests.mock.calls.length

    fireEvent.click(screen.getByTestId('review-req-priya'))
    fireEvent.click(screen.getByTestId('review-cancel'))

    await waitFor(() => expect(screen.queryByTestId('connector-review-dialog')).toBeNull())
    // The queue is still there — the liveness half — and nothing was written or re-read.
    expect(screen.getByTestId('queue-table-waiting')).toBeTruthy()
    expect(h.approveConnectorRequest).not.toHaveBeenCalled()
    expect(h.declineConnectorRequest).not.toHaveBeenCalled()
    expect(h.listConnectorRequests.mock.calls.length).toBe(loadsBefore)
  })
})

describe('WaitingCountBadge — non-regression across its three mounts', () => {
  it('keeps the app sentence on the two mounts that shipped with it', () => {
    render(
      <>
        <WaitingCountBadge count={3} where="nav" />
        <WaitingCountBadge count={1} where="tab" />
      </>,
    )

    expect(
      within(screen.getByTestId('waiting-count-nav')).getByText('3 apps waiting for review'),
    ).toBeTruthy()
    expect(
      within(screen.getByTestId('waiting-count-tab')).getByText('1 app waiting for review'),
    ).toBeTruthy()
  })

  it('announces the new mount as people, once, with the numeral hidden', async () => {
    openConsole()

    const badge = await screen.findByTestId('waiting-count-integrations-tab')
    // Once — not the numeral read out a second time without its meaning.
    expect(within(badge).getAllByText('3 people waiting for access')).toHaveLength(1)
    const numeral = badge.querySelector('[aria-hidden="true"]')
    expect(numeral?.textContent).toBe('3')
  })
})
