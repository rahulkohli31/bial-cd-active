/**
 * Integrations — the door, the four person states, and the one dialog behind all of them.
 *
 * WHY THE FIXTURE CONNECTOR IS NOT THE REAL ONE. Every string this dialog renders about a
 * connector comes off the wire, so the suite names its own (`ORBIT`). That is the point rather
 * than a convenience: a component that had the real connector's name compiled into it would still
 * pass a test that asserted the real name, and this suite could not tell the difference.
 *
 * LIVENESS EVERYWHERE AN ABSENCE IS ASSERTED. Two of these tests are about something NOT being on
 * screen — the greyed placeholder row, and any control at all beside a decline — and a component
 * that crashed on mount would satisfy both. Each is paired with a positive assertion that the
 * dialog really rendered, because this repo has been bitten by exactly that.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react'

const h = vi.hoisted(() => ({
  listConnectors: vi.fn(),
  requestConnectorAccess: vi.fn(),
  cancelConnectorRequest: vi.fn(),
}))
vi.mock('../../../utils/connectorApi', () => ({
  listConnectors: h.listConnectors,
  requestConnectorAccess: h.requestConnectorAccess,
  cancelConnectorRequest: h.cancelConnectorRequest,
}))

import IntegrationsDialog from '../IntegrationsDialog'
import type { ConnectorEntry } from '../../../utils/connectorApi'
import { ApiError } from '../../../utils/apiError'

/** A never-asked entry, and the base every other state is spread over. */
const base: ConnectorEntry = {
  key: 'orbit',
  displayName: 'ORBIT',
  subtitle: 'Airport operations',
  // The ask panel's copy, which is the server's and not the component's — invented here for the
  // same reason the name is. `AskAccessPanel.test.tsx` is where the rendering of it is pinned.
  askSubtitle:
    'ORBIT is BIAL’s airport operations data. An administrator decides who may read it — you are asking once, for yourself.',
  consentLinesRequester: [
    { lead: 'Read-only.', body: 'Nothing you build can change ORBIT data.' },
    {
      lead: 'One dataset.',
      body: 'The Flight Fact Report — flight schedules, gates, stands and status. Nothing else in ORBIT.',
    },
    {
      lead: 'Every project you own.',
      body: 'Including ones you have not made yet. You switch it on per project, and pick the days each one reads.',
    },
  ],
  state: 'neverAsked',
  askedAt: null,
  approvedAt: null,
  approvedByName: null,
  onProjectCount: null,
  decidedAt: null,
  decidedByName: null,
  decisionRemarks: null,
}

const pending: ConnectorEntry = {
  ...base,
  state: 'pending',
  // Midday UTC on purpose: every timezone this product is read in still calls it 5 September,
  // so the DATE half of the assertion below is stable while the clock half stays local.
  askedAt: '2026-09-05T12:00:00.000Z',
}

const approved: ConnectorEntry = {
  ...base,
  state: 'approved',
  approvedAt: '2026-09-02T12:00:00.000Z',
  approvedByName: 'Rahul Menon',
  onProjectCount: 2,
}

const DECLINE_REMARK =
  'Nothing you have built needs operational flight data yet. Ask again when something does.'

const declined: ConnectorEntry = {
  ...base,
  state: 'declined',
  decidedAt: '2026-09-02T12:00:00.000Z',
  decidedByName: 'Rahul Menon',
  decisionRemarks: DECLINE_REMARK,
}

const A_GOOD_REASON = 'I build the departures board the duty managers use every shift'

beforeEach(() => {
  vi.clearAllMocks()
  h.listConnectors.mockResolvedValue([base])
})
afterEach(() => cleanup())

const open = (): ReturnType<typeof render> => render(<IntegrationsDialog onClose={() => {}} />)

describe('the dialog lists the registry, and the registry has one entry', () => {
  it('renders the board title, subtitle and the one connector row', async () => {
    open()

    expect(await screen.findByText('Integrations')).toBeTruthy()
    expect(
      screen.getByText(
        'Data BIAL already holds. An administrator gives you access once — every project you own can then use it.',
      ),
    ).toBeTruthy()
    expect(screen.getByText('ORBIT')).toBeTruthy()
    expect(screen.getByText('Airport operations')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Request access' })).toBeTruthy()
  })

  it('THE MUTANT: renders exactly as many rows as the server sent — a second, greyed placeholder turns this red', async () => {
    // The boards draw a `[ANOTHER BIAL SYSTEM]` / `Not yet` row as a placeholder for a future
    // integration. It is not built (owner ruling, 2026-09-08) and there is no registry entry
    // behind it: both lists render the registry. Add a hard-coded second row and this fails.
    open()

    // Liveness first — the real row is on screen, so the count below is a count of a rendered
    // list rather than of a crashed one.
    expect(await screen.findByTestId('connector-row-orbit')).toBeTruthy()
    expect(screen.getAllByRole('listitem')).toHaveLength(1)
    expect(screen.queryByText('Not yet')).toBeNull()
    expect(screen.queryByText(/nothing else is connected/i)).toBeNull()
  })
})

describe('asking for access', () => {
  it('swaps the body to the ask panel, posts the remarks, and the list re-reads to the waiting row', async () => {
    h.requestConnectorAccess.mockResolvedValue(pending)
    open()

    fireEvent.click(await screen.findByRole('button', { name: 'Request access' }))

    // The ask panel, titled from the wire.
    expect(await screen.findByText('Ask for access to ORBIT')).toBeTruthy()

    fireEvent.change(screen.getByLabelText('Why you need access to ORBIT'), {
      target: { value: A_GOOD_REASON },
    })
    // The list re-reads AFTER the write, so the second answer is the waiting row.
    h.listConnectors.mockResolvedValue([pending])
    fireEvent.click(screen.getByRole('button', { name: 'Ask an administrator' }))

    await waitFor(() => expect(h.requestConnectorAccess).toHaveBeenCalledWith('orbit', A_GOOD_REASON))
    await waitFor(() => expect(h.listConnectors).toHaveBeenCalledTimes(2))

    // Back on the list, in the waiting state, with the only control that state has.
    const row = await screen.findByTestId('connector-row-orbit')
    expect(row.textContent).toMatch(
      /^ORBITAsked 5 Sep, \d{2}:\d{2} · waiting on an administratorCancel$/,
    )
  })

  it('surfaces the server’s own 409, not a generic failure', async () => {
    h.requestConnectorAccess.mockRejectedValue(
      new ApiError(
        'You have already asked for access to this. An administrator is looking at it.',
        409,
        'already_pending',
      ),
    )
    open()

    fireEvent.click(await screen.findByRole('button', { name: 'Request access' }))
    fireEvent.change(await screen.findByLabelText('Why you need access to ORBIT'), {
      target: { value: A_GOOD_REASON },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Ask an administrator' }))

    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toBe(
      'You have already asked for access to this. An administrator is looking at it.',
    )
    // …and it stayed on the panel, so the person can read it beside the box they wrote in.
    expect(screen.getByText('Ask for access to ORBIT')).toBeTruthy()
  })

  it('going forward to the ask panel and back again does NOT unmount the dialog', async () => {
    // ONE Radix dialog, three bodies. A second `<Dialog>` for the ask would unmount one and mount
    // another on every forward and back click — two backdrop fades and `useFocusBackstop()` firing
    // twice on what the boards draw as one continuous panel. The node identity is the only
    // assertion that can tell the two implementations apart.
    open()

    await screen.findByRole('button', { name: 'Request access' })
    const before = document.querySelector('[data-testid="integrations-dialog"]')
    expect(before).not.toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Request access' }))
    expect(await screen.findByText('Ask for access to ORBIT')).toBeTruthy()
    expect(document.querySelector('[data-testid="integrations-dialog"]')).toBe(before)

    fireEvent.click(screen.getByRole('button', { name: 'Back to integrations' }))
    expect(await screen.findByRole('button', { name: 'Request access' })).toBeTruthy()
    expect(document.querySelector('[data-testid="integrations-dialog"]')).toBe(before)
  })
})

describe('waiting, approved and declined', () => {
  it('cancels a waiting request and the row returns to Request access', async () => {
    h.listConnectors.mockResolvedValue([pending])
    h.cancelConnectorRequest.mockResolvedValue(base)
    open()

    fireEvent.click(await screen.findByRole('button', { name: 'Cancel' }))
    h.listConnectors.mockResolvedValue([base])

    await waitFor(() => expect(h.cancelConnectorRequest).toHaveBeenCalledWith('orbit'))
    expect(await screen.findByRole('button', { name: 'Request access' })).toBeTruthy()
  })

  it('names who approved it, when, and how many projects it is on', async () => {
    h.listConnectors.mockResolvedValue([approved])
    open()

    const row = await screen.findByTestId('connector-row-orbit')
    expect(row.textContent).toContain('Approved for you 2 Sep · Rahul Menon')
    expect(screen.getByRole('button', { name: /On in 2 projects/ })).toBeTruthy()
  })

  it('drops the separator rather than dangling it when there is no decider to name', async () => {
    // `null` means the administrator who decided has since been deleted — NOT "look up their
    // email", which the server already did. `Approved for you 2 Sep · ` is the bug this pins.
    h.listConnectors.mockResolvedValue([{ ...approved, approvedByName: null }])
    open()

    const row = await screen.findByTestId('connector-row-orbit')
    expect(row.textContent).toContain('Approved for you 2 Sep')
    expect(row.textContent).not.toContain('·')
  })

  it('THE MUTANT: a decline shows the date, the decider and the remark IN FULL, and no control at all', async () => {
    // `Ask again` is drawn on the board and is not built (owner ruling, 2026-09-08); a decline is
    // final for this pass. Render any control on this row and this goes red.
    h.listConnectors.mockResolvedValue([declined])
    open()

    const row = await screen.findByTestId('connector-row-orbit')
    // Liveness: the row rendered its whole answer, so the absence below is about the control.
    expect(row.textContent).toContain('Declined 2 Sep · Rahul Menon')
    expect(row.textContent).toContain(DECLINE_REMARK)

    expect(row.querySelector('button')).toBeNull()
    expect(screen.queryByRole('button', { name: /ask again/i })).toBeNull()
  })
})

describe('when the list cannot be read', () => {
  it('shows the failure and a retry — never an empty list that reads as "no connectors"', async () => {
    h.listConnectors.mockRejectedValueOnce(new ApiError('Failed to load your integrations (500).', 500))
    open()

    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('Failed to load your integrations (500).')
    // The one absence that matters: no list, and no skeleton pretending to still be loading.
    expect(screen.queryByRole('listitem')).toBeNull()
    expect(screen.queryByTestId('connector-list-loading')).toBeNull()

    h.listConnectors.mockResolvedValue([base])
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))

    expect(await screen.findByTestId('connector-row-orbit')).toBeTruthy()
    expect(screen.queryByRole('alert')).toBeNull()
  })
})
