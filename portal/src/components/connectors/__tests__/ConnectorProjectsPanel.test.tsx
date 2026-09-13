/**
 * The drill-down: every project, its switch, its chip and its days.
 *
 * THE FIXTURE CONNECTOR IS `ORBIT`, NOT THE REAL ONE — the same choice, for the same reason, as
 * `IntegrationsDialog.test.tsx`: every string this panel says about a connector comes off the
 * wire, and a component carrying the real name would still satisfy a suite that asserted it.
 *
 * NOTHING HERE LETS THE BROWSER RESOLVE A WINDOW. Two tests exist purely to catch a recompute:
 * one feeds a resolved pair deliberately inconsistent with the stored pair, and one has the
 * server answer a `Last 7 days` pick with `Last 14 days`. Both go red the moment anything in the
 * browser derives a window instead of rendering the one it was sent (R13).
 *
 * EVERY ABSENCE ASSERTION IS PAIRED WITH A LIVENESS ONE. "No chip" and "no search box" are both
 * satisfied by a component that crashed on mount, and this repo has been bitten by exactly that.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, render, screen, fireEvent, cleanup, waitFor, within } from '@testing-library/react'

const h = vi.hoisted(() => ({
  listConnectorProjects: vi.fn(),
  setProjectConnector: vi.fn(),
  // The dialog's own three, for the one test that drills in through the real door rather than
  // mounting this panel directly.
  listConnectors: vi.fn(),
  requestConnectorAccess: vi.fn(),
  cancelConnectorRequest: vi.fn(),
}))
vi.mock('../../../utils/connectorApi', () => ({
  listConnectorProjects: h.listConnectorProjects,
  setProjectConnector: h.setProjectConnector,
  listConnectors: h.listConnectors,
  requestConnectorAccess: h.requestConnectorAccess,
  cancelConnectorRequest: h.cancelConnectorRequest,
}))

import ConnectorProjectsPanel from '../ConnectorProjectsPanel'
import IntegrationsDialog from '../IntegrationsDialog'
import { Dialog, DialogContent } from '../../ui/dialog'
import { ApiError } from '../../../utils/apiError'
import type {
  ConnectorEntry,
  ConnectorProjectEntry,
  ConnectorWindow,
  ProjectConnectorEntry,
} from '../../../utils/connectorApi'

const connector: ConnectorEntry = {
  key: 'orbit',
  displayName: 'ORBIT',
  subtitle: 'Airport operations',
  askSubtitle: 'ORBIT is BIAL’s airport operations data.',
  consentLinesRequester: [{ lead: 'Read-only.', body: 'Nothing you build can change it.' }],
  state: 'approved',
  askedAt: null,
  approvedAt: '2026-09-02T12:00:00.000Z',
  approvedByName: 'Rahul Menon',
  onProjectCount: 2,
  decidedAt: null,
  decidedByName: null,
  decisionRemarks: null,
}

const absolute: ConnectorWindow = {
  kind: 'absolute',
  start: '2026-09-01',
  end: '2026-09-30',
  days: 30,
  clamped: false,
  earliestDate: '2026-09-01',
  latestDate: '2026-09-30',
  stored: { days: null, start: '2026-09-01', end: '2026-09-30' },
}

const lastSeven: ConnectorWindow = {
  kind: 'relative',
  start: '2026-09-24',
  end: '2026-09-30',
  days: 7,
  clamped: false,
  earliestDate: '2026-09-01',
  latestDate: '2026-09-30',
  stored: { days: 7, start: null, end: null },
}

const projects: ConnectorProjectEntry[] = [
  { projectId: 'p1', name: 'Terminal 2 Departures', enabled: true, window: absolute },
  { projectId: 'p2', name: 'Bay Occupancy — T1', enabled: true, window: lastSeven },
  { projectId: 'p3', name: 'Turnaround Times', enabled: false, window: null },
  { projectId: 'p4', name: 'Visitor Log — Airport Office', enabled: false, window: null },
  { projectId: 'p5', name: 'Stand Allocation', enabled: false, window: null },
]

/** What the PUT answers with. Only `enabled` and `window` are read by the row. */
function settled(enabled: boolean, window: ConnectorWindow | null): ProjectConnectorEntry {
  return {
    key: 'orbit',
    displayName: 'ORBIT',
    dataNoun: 'flight data',
    state: 'approved',
    askedAt: null,
    enabled,
    effectivelyOn: enabled,
    window,
  }
}

/** A promise this test resolves by hand, so two writes can genuinely overlap. */
function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void; reject: (reason: unknown) => void } {
  let resolve: (value: T) => void = () => {}
  let reject: (reason: unknown) => void = () => {}
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

const onBack = vi.fn()
const onClose = vi.fn()

/**
 * Mounted inside a real `Dialog`, because that is where it lives — `DialogTitle` reads its id off
 * the dialog's context and a bare render throws. Same harness as `AskAccessPanel.test.tsx`.
 */
function mount(): void {
  render(
    <Dialog open>
      <DialogContent hideClose>
        <ConnectorProjectsPanel entry={connector} onBack={onBack} onClose={onClose} />
      </DialogContent>
    </Dialog>,
  )
}

/** The switch for one project, by the accessible name the row composes for it. */
function switchFor(project: string): HTMLElement {
  return screen.getByRole('switch', { name: `Read ORBIT in ${project}` })
}

/** The date chip for one project, or `null` when the row draws an em dash instead. */
function chipFor(project: string): HTMLElement | null {
  return screen.queryByRole('button', {
    name: new RegExp(`^Days ORBIT reads in ${project.replace(/[.*+?^${}()|[\]\\—]/g, '\\$&')}:`),
  })
}

beforeEach(() => {
  h.listConnectorProjects.mockReset()
  h.setProjectConnector.mockReset()
  h.listConnectors.mockReset()
  onBack.mockReset()
  onClose.mockReset()
  h.listConnectorProjects.mockResolvedValue({ projects, truncated: false })
  h.listConnectors.mockResolvedValue([connector])
})

afterEach(cleanup)

describe('ConnectorProjectsPanel', () => {
  it('renders the approval sentence and every project, in the server’s order', async () => {
    mount()

    await screen.findByText('Terminal 2 Departures')
    expect(
      screen.getByText('Approved for you 2 Sep by Rahul Menon. Switch it on where you need it.'),
    ).toBeTruthy()

    // Five rows, in the server's order — the panel does not re-sort somebody's projects.
    const rows = screen.getAllByRole('listitem')
    expect(rows.map((row) => row.getAttribute('data-testid'))).toEqual([
      'project-connector-p1',
      'project-connector-p2',
      'project-connector-p3',
      'project-connector-p4',
      'project-connector-p5',
    ])
    for (const project of projects) {
      expect(within(screen.getByTestId(`project-connector-${project.projectId}`)).getByText(project.name)).toBeTruthy()
    }
    expect(screen.getAllByRole('switch')).toHaveLength(5)
  })

  it('writes with that project’s id and this connector’s key', async () => {
    h.setProjectConnector.mockResolvedValue(settled(true, absolute))
    mount()
    await screen.findByText('Turnaround Times')

    fireEvent.click(switchFor('Turnaround Times'))

    await waitFor(() => expect(h.setProjectConnector).toHaveBeenCalledTimes(1))
    expect(h.setProjectConnector).toHaveBeenCalledWith('p3', 'orbit', { enabled: true })
  })

  it('shows a chip on a project that is on, and an em dash with no chip on one that is off', async () => {
    mount()
    await screen.findByText('Terminal 2 Departures')

    // Liveness: the on row really rendered its control before the off row is checked for absence.
    expect(chipFor('Terminal 2 Departures')).toBeTruthy()
    expect(switchFor('Turnaround Times').getAttribute('aria-checked')).toBe('false')
    expect(chipFor('Turnaround Times')).toBeNull()
    expect(
      within(screen.getByTestId('project-connector-p3')).getByText('—'),
    ).toBeTruthy()
  })

  it('renders each window in its own shape — a preset as words, a fixed range as dates', async () => {
    mount()
    await screen.findByText('Terminal 2 Departures')

    expect(chipFor('Terminal 2 Departures')?.textContent).toBe('1 – 30 Sep')
    expect(chipFor('Bay Occupancy — T1')?.textContent).toBe('Last 7 days')
  })

  it('renders the RESOLVED window even when the stored pair says something else', async () => {
    // Stored `1 – 30 Jun`, resolved to September because June aged out. Anything in the browser
    // that recomputed a window from `stored` would draw `1 – 30 Jun` and fail here.
    const clamped: ConnectorWindow = {
      ...absolute,
      clamped: true,
      stored: { days: null, start: '2026-06-01', end: '2026-06-30' },
    }
    h.listConnectorProjects.mockResolvedValue({
      projects: [{ projectId: 'p1', name: 'Terminal 2 Departures', enabled: true, window: clamped }],
      truncated: false,
    })
    mount()
    await screen.findByText('Terminal 2 Departures')

    expect(chipFor('Terminal 2 Departures')?.textContent).toBe('1 – 30 Sep')
  })

  it('opens the popover with the stored preset ticked, and re-renders the chip from the server’s answer', async () => {
    // The server answers the `Last 7 days` pick with FOURTEEN. Nothing about that is realistic;
    // it is the whole point. A chip drawn from the local pick would read `Last 7 days`.
    h.setProjectConnector.mockResolvedValue(
      settled(true, { ...lastSeven, days: 14, start: '2026-09-17', stored: { days: 14, start: null, end: null } }),
    )
    mount()
    await screen.findByText('Bay Occupancy — T1')

    const chip = chipFor('Bay Occupancy — T1')
    expect(chip).toBeTruthy()
    fireEvent.click(chip as HTMLElement)

    await screen.findByTestId('window-popover')
    expect(screen.getByRole('radio', { name: 'Last 7 days' }).getAttribute('aria-checked')).toBe(
      'true',
    )

    fireEvent.click(screen.getByRole('radio', { name: 'Last 7 days' }))
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }))

    await waitFor(() => expect(h.setProjectConnector).toHaveBeenCalledTimes(1))
    expect(h.setProjectConnector).toHaveBeenCalledWith('p2', 'orbit', {
      enabled: true,
      window: { kind: 'relative', days: 7 },
    })
    await waitFor(() => expect(chipFor('Bay Occupancy — T1')?.textContent).toBe('Last 14 days'))
  })

  it('opens that popover from inside the dialog it is really mounted in', async () => {
    mount()
    await screen.findByText('Terminal 2 Departures')

    fireEvent.click(chipFor('Terminal 2 Departures') as HTMLElement)

    // The popover portals to `document.body`, OUTSIDE the modal dialog's own subtree — so this
    // is not a formality: if the dialog's focus trap or its `aria-hidden` sweep swallowed the
    // portalled layer, the whole control would be unreachable in the one place it is mounted.
    const popover = await screen.findByTestId('window-popover')
    expect(within(popover).getByRole('button', { name: 'Apply' })).toBeTruthy()
  })

  it('renders one line and no search box when there are no projects yet', async () => {
    h.listConnectorProjects.mockResolvedValue({ projects: [], truncated: false })
    mount()

    await screen.findByTestId('connector-projects-empty')
    expect(screen.getByTestId('connector-projects-empty').textContent).toBe(
      'You do not have a project yet — the switch will be here when you make one.',
    )
    expect(screen.queryByRole('searchbox')).toBeNull()
    expect(screen.queryAllByRole('listitem')).toHaveLength(0)
  })

  it('says so above the search when the list was capped', async () => {
    h.listConnectorProjects.mockResolvedValue({ projects, truncated: true })
    mount()

    await screen.findByText(/Showing your 5 most recent projects/)
    // The count is counted, not written down: a hard-coded 200 would read wrong here.
    expect(screen.getByRole('searchbox')).toBeTruthy()
  })

  it('does not claim a cap that did not bite', async () => {
    mount()
    await screen.findByText('Terminal 2 Departures')

    expect(screen.queryByText(/most recent projects/)).toBeNull()
    expect(screen.getByRole('searchbox')).toBeTruthy()
  })

  it('filters by name, case-insensitively, and says when nothing matches', async () => {
    mount()
    await screen.findByText('Terminal 2 Departures')

    fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'bay occ' } })
    expect(screen.getAllByRole('listitem')).toHaveLength(1)
    expect(screen.getByText('Bay Occupancy — T1')).toBeTruthy()

    fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'zzz' } })
    expect(screen.getByTestId('connector-projects-no-match').textContent).toBe(
      'No project matches that search.',
    )
    expect(screen.queryAllByRole('listitem')).toHaveLength(0)
  })

  it('leaves every other project’s switch and chip exactly where they were', async () => {
    h.setProjectConnector.mockResolvedValue(settled(true, absolute))
    mount()
    await screen.findByText('Terminal 2 Departures')

    fireEvent.click(switchFor('Turnaround Times'))
    await waitFor(() => expect(h.setProjectConnector).toHaveBeenCalledTimes(1))

    expect(switchFor('Terminal 2 Departures').getAttribute('aria-checked')).toBe('true')
    expect(chipFor('Terminal 2 Departures')?.textContent).toBe('1 – 30 Sep')
    expect(switchFor('Bay Occupancy — T1').getAttribute('aria-checked')).toBe('true')
    expect(chipFor('Bay Occupancy — T1')?.textContent).toBe('Last 7 days')
    expect(switchFor('Visitor Log — Airport Office').getAttribute('aria-checked')).toBe('false')
  })

  it('locks the switch until its own write settles, so a double press sends one write', async () => {
    const first = deferred<ProjectConnectorEntry>()
    h.setProjectConnector.mockReturnValueOnce(first.promise)
    mount()
    await screen.findByText('Turnaround Times')

    fireEvent.click(switchFor('Turnaround Times'))
    await waitFor(() =>
      expect(switchFor('Turnaround Times').getAttribute('aria-disabled')).toBe('true'),
    )

    fireEvent.click(switchFor('Turnaround Times'))
    expect(h.setProjectConnector).toHaveBeenCalledTimes(1)

    first.resolve(settled(true, absolute))
    await waitFor(() =>
      expect(switchFor('Turnaround Times').getAttribute('aria-disabled')).toBe('false'),
    )
  })

  it('never lets a late answer overwrite a newer desired state', async () => {
    // TWO WRITES ON ONE ROW, genuinely overlapping: the switch's lock is the switch's, and it
    // says nothing about a write the popover's `Apply` started. Drop the sequence stamp in
    // `ProjectConnectorRow.write` and the slow `Apply` below wins — the switch ends ON.
    const slowApply = deferred<ProjectConnectorEntry>()
    h.setProjectConnector.mockReturnValueOnce(slowApply.promise)
    h.setProjectConnector.mockResolvedValue(settled(false, absolute))
    mount()
    await screen.findByText('Terminal 2 Departures')

    fireEvent.click(chipFor('Terminal 2 Departures') as HTMLElement)
    await screen.findByTestId('window-popover')
    fireEvent.click(screen.getByRole('radio', { name: 'Last 7 days' }))
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }))
    await waitFor(() => expect(h.setProjectConnector).toHaveBeenCalledTimes(1))

    // …and now the citizen switches the project OFF while that is still in the air.
    fireEvent.click(switchFor('Terminal 2 Departures'))
    await waitFor(() => expect(h.setProjectConnector).toHaveBeenCalledTimes(2))
    await waitFor(() =>
      expect(switchFor('Terminal 2 Departures').getAttribute('aria-checked')).toBe('false'),
    )

    slowApply.resolve(settled(true, lastSeven))
    // Deterministic, not a `waitFor`: the row's own continuation is queued on this promise BEFORE
    // the test's, so by the time `act` returns the late answer has been through the component. A
    // `waitFor` here would pass on the poll that happened to run before it landed.
    await act(async () => {
      await slowApply.promise
    })

    expect(switchFor('Terminal 2 Departures').getAttribute('aria-checked')).toBe('false')
    expect(chipFor('Terminal 2 Departures')).toBeNull()
  })

  it('rolls a failed toggle back AND says so', async () => {
    h.setProjectConnector.mockRejectedValue(new Error('The network went away.'))
    mount()
    await screen.findByText('Turnaround Times')

    fireEvent.click(switchFor('Turnaround Times'))

    const alert = await screen.findByTestId('connector-projects-toast')
    expect(alert.textContent).toContain('The network went away.')
    // Both halves: a silent rollback looks exactly like a press that missed.
    expect(switchFor('Turnaround Times').getAttribute('aria-checked')).toBe('false')
  })

  it('rolls a failed toggle back to what the SERVER settled, not to what the list loaded with', async () => {
    // THE REGRESSION THIS PINS. The row's failure path restores its props, which is only correct
    // while the props ARE the last settled answer. This panel used to leave them at whatever
    // `load()` fetched, so the second write below rolled the switch back to OFF — the position
    // the citizen had just asked for — while the server held ON. A red banner over a switch that
    // agrees with the failed request is the worst of both: it reads as "your change did not
    // happen" while the change that DID happen is the opposite one.
    //
    // The existing failure test cannot see this: it fails on the FIRST toggle, where the loaded
    // props and the server agree, so rolling back to either gives the same answer.
    h.setProjectConnector.mockResolvedValueOnce(settled(true, lastSeven))
    mount()
    await screen.findByText('Turnaround Times')
    expect(switchFor('Turnaround Times').getAttribute('aria-checked')).toBe('false')

    fireEvent.click(switchFor('Turnaround Times'))
    await waitFor(() =>
      expect(switchFor('Turnaround Times').getAttribute('aria-checked')).toBe('true'),
    )

    h.setProjectConnector.mockRejectedValueOnce(new Error('The network went away.'))
    fireEvent.click(switchFor('Turnaround Times'))

    const alert = await screen.findByTestId('connector-projects-toast')
    expect(alert.textContent).toContain('The network went away.')
    // ON, because that is what the server actually holds — not OFF, the state it loaded with.
    expect(switchFor('Turnaround Times').getAttribute('aria-checked')).toBe('true')
  })

  it('keeps a settled switch through a search that filters its row out and back', async () => {
    // A filtered-out row is REMOVED from the tree, so its local override dies with it. When the
    // query clears it remounts from these props — which is why the panel has to record what the
    // server settled. Without that, typing and clearing a search silently resurrected every
    // toggled row's pre-write position, and the citizen would believe connectors were off that
    // the server holds on.
    h.setProjectConnector.mockResolvedValueOnce(settled(true, lastSeven))
    mount()
    await screen.findByText('Turnaround Times')

    fireEvent.click(switchFor('Turnaround Times'))
    await waitFor(() =>
      expect(switchFor('Turnaround Times').getAttribute('aria-checked')).toBe('true'),
    )

    const search = screen.getByPlaceholderText('Search projects…')
    fireEvent.change(search, { target: { value: 'Terminal' } })
    await waitFor(() => expect(screen.queryByText('Turnaround Times')).toBeNull())
    // Paired positive: the list is filtered, not blank — an absence assertion over a crashed
    // render would otherwise pass here.
    expect(screen.getByText('Terminal 2 Departures')).toBeTruthy()

    fireEvent.change(search, { target: { value: '' } })
    await screen.findByText('Turnaround Times')
    expect(switchFor('Turnaround Times').getAttribute('aria-checked')).toBe('true')
  })

  it('surfaces the server’s own refusal when access is not approved, and stays off', async () => {
    h.setProjectConnector.mockRejectedValue(
      new ApiError(
        'You do not have access to ORBIT yet. Ask for it under Integrations.',
        403,
        'access_not_approved',
      ),
    )
    mount()
    await screen.findByText('Turnaround Times')

    fireEvent.click(switchFor('Turnaround Times'))

    const alert = await screen.findByTestId('connector-projects-toast')
    expect(alert.textContent).toContain(
      'You do not have access to ORBIT yet. Ask for it under Integrations.',
    )
    expect(switchFor('Turnaround Times').getAttribute('aria-checked')).toBe('false')
  })

  it('keeps the popover open and the chip unchanged when Apply is refused', async () => {
    h.setProjectConnector.mockRejectedValue(new Error('Pick one of the ranges ORBIT offers.'))
    mount()
    await screen.findByText('Bay Occupancy — T1')

    fireEvent.click(chipFor('Bay Occupancy — T1') as HTMLElement)
    await screen.findByTestId('window-popover')
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }))

    const popover = await screen.findByTestId('window-popover')
    await within(popover).findByRole('alert')
    expect(within(popover).getByRole('alert').textContent).toBe(
      'Pick one of the ranges ORBIT offers.',
    )
    expect(chipFor('Bay Occupancy — T1')?.textContent).toBe('Last 7 days')
  })

  it('names its project on both controls, for a reader who cannot see the row', async () => {
    mount()
    await screen.findByText('Terminal 2 Departures')

    expect(switchFor('Terminal 2 Departures').getAttribute('role')).toBe('switch')
    expect(switchFor('Terminal 2 Departures').getAttribute('aria-checked')).toBe('true')
    expect(chipFor('Terminal 2 Departures')?.getAttribute('aria-label')).toBe(
      'Days ORBIT reads in Terminal 2 Departures: 1 – 30 Sep',
    )
  })

  it('is what the approved connector row drills into, and coming back re-reads the list', async () => {
    // THE ONE TEST OF THE WIRING ITSELF. `IntegrationsDialog.openProjects` shipped as a
    // documented no-op for this unit to fill in; without this, reverting it to `() => {}` leaves
    // every other test in this file green and the feature unreachable from the product.
    render(<IntegrationsDialog onClose={onClose} />)

    const drillIn = await screen.findByTestId('connector-projects-orbit')
    expect(drillIn.textContent).toContain('On in 2 projects')
    fireEvent.click(drillIn)

    await screen.findByTestId('connector-projects-panel')
    expect(screen.getByText('Terminal 2 Departures')).toBeTruthy()
    expect(h.listConnectors).toHaveBeenCalledTimes(1)

    // Back re-reads, because `On in N projects ›` is exactly the number the switches behind it
    // change. Drop the reload and the dialog's two surfaces disagree about a count one of them
    // just moved.
    fireEvent.click(screen.getByRole('button', { name: 'Back to integrations' }))
    await screen.findByTestId('connector-list-body')
    await waitFor(() => expect(h.listConnectors).toHaveBeenCalledTimes(2))
  })

  it('offers the read again when it fails, rather than showing an empty list', async () => {
    h.listConnectorProjects.mockRejectedValueOnce(new Error('Failed to load your projects (500).'))
    mount()

    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('Failed to load your projects (500).')

    h.listConnectorProjects.mockResolvedValue({ projects, truncated: false })
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    await screen.findByText('Terminal 2 Departures')
  })
})
