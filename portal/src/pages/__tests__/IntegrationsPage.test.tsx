/**
 * The Integrations page: one card per connector, and behind a disclosure the applications that
 * have it switched on right now.
 *
 * THE FIXTURE CONNECTOR IS `ORBIT`, NOT THE REAL ONE, for the reason the whole feature's suites
 * give: every string this page says about a connector comes off the wire, and a page with the
 * real name compiled into it would still pass a suite that asserted the real name.
 *
 * EVERY ABSENCE ASSERTION IS PAIRED. A page that crashed on mount satisfies "no record count
 * appears" and "there is no disclosure" perfectly, so each of those tests also asserts something
 * positive rendered.
 *
 * THE LAST DESCRIBE IS AN INTEGRATION, NOT A MOCK HANDSHAKE. The page and the per-application
 * settings tab are driven against ONE in-memory store standing in for the server, so "both read
 * the same source" is proved by flipping a switch on one surface and reading it on the other.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, fireEvent, waitFor, act } from '@testing-library/react'
import { MotionGlobalConfig } from 'motion/react'

const h = vi.hoisted(() => ({
  listConnectors: vi.fn(),
  requestConnectorAccess: vi.fn(),
  cancelConnectorRequest: vi.fn(),
  setProjectConnector: vi.fn(),
  listProjectConnectors: vi.fn(),
  patchProject: vi.fn(),
  getDeployment: vi.fn(),
  listShares: vi.fn(),
}))

vi.mock('../../utils/connectorApi', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  listConnectors: h.listConnectors,
  requestConnectorAccess: h.requestConnectorAccess,
  cancelConnectorRequest: h.cancelConnectorRequest,
  setProjectConnector: h.setProjectConnector,
  listProjectConnectors: h.listProjectConnectors,
}))
vi.mock('../../utils/projectApi', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  patchProject: h.patchProject,
}))
vi.mock('../../utils/deployApi', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  getDeployment: h.getDeployment,
}))
vi.mock('../../utils/sharingApi', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  listShares: h.listShares,
}))
vi.mock('../../components/projects/ProjectDescriptionEditor', () => ({
  default: () => <div data-testid="description-editor" />,
}))

import IntegrationsPage from '../IntegrationsPage'
import AppSettingsDialog from '../../components/projects/AppSettingsDialog'
import type { ConnectorEntry, ProjectConnectorEntry } from '../../utils/connectorApi'
import type { Project } from '../../utils/projectApi'

MotionGlobalConfig.skipAnimations = true

const CONSENT = [
  { lead: 'Read-only.', body: 'Nothing you build can change ORBIT data.' },
  { lead: 'One dataset.', body: 'The Flight Fact Report. Nothing else in ORBIT.' },
  { lead: 'Every project you own.', body: 'Including ones you have not made yet.' },
]

function entry(over: Partial<ConnectorEntry> = {}): ConnectorEntry {
  return {
    key: 'orbit',
    displayName: 'ORBIT',
    subtitle: 'Airport operations',
    askSubtitle: 'ORBIT is BIAL’s airport operations data. An administrator decides who reads it.',
    consentLinesRequester: CONSENT,
    state: 'approved',
    askedAt: null,
    approvedAt: '2026-09-02T09:15:00Z',
    approvedByName: 'Rahul Menon',
    onProjectCount: 2,
    onProjects: [
      { projectId: 'p1', name: 'Flight Delay Reason Capture' },
      { projectId: 'p2', name: 'Baggage Belt Downtime Tracker' },
    ],
    decidedAt: null,
    decidedByName: null,
    decisionRemarks: null,
    ...over,
  }
}

const WAITING: Partial<ConnectorEntry> = {
  state: 'pending',
  askedAt: '2026-09-05T03:00:00Z',
  approvedAt: null,
  approvedByName: null,
  onProjectCount: null,
  onProjects: [],
}

const NEVER_ASKED: Partial<ConnectorEntry> = {
  state: 'neverAsked',
  approvedAt: null,
  approvedByName: null,
  onProjectCount: null,
  onProjects: [],
}

const settle = () => act(async () => { await new Promise((r) => setTimeout(r, 0)) })

/** The disclosure's own trigger, which is also the only control that opens the list. */
const disclosure = () => screen.getByTestId('connector-disclosure-orbit')

async function openDisclosure(): Promise<void> {
  fireEvent.click(await screen.findByTestId('connector-disclosure-orbit'))
  await settle()
}

beforeEach(() => {
  vi.clearAllMocks()
  h.listConnectors.mockResolvedValue([entry()])
  h.setProjectConnector.mockResolvedValue({
    key: 'orbit',
    displayName: 'ORBIT',
    dataNoun: 'flight data',
    state: 'approved',
    askedAt: null,
    enabled: false,
    effectivelyOn: false,
    window: null,
  })
})
afterEach(() => cleanup())

describe('the card states what access this person has', () => {
  it('shows the connector once, the access pill, and the disclosure with its count', async () => {
    render(<IntegrationsPage />)

    expect(await screen.findByTestId('connector-card-orbit')).toBeTruthy()
    // ONCE. Four states stacked as repeated rows would read as four connectors.
    expect(screen.getAllByText('ORBIT')).toHaveLength(1)
    expect(screen.getByTestId('connector-granted-orbit').textContent).toContain('You have access')
    expect(screen.getByText('Approved for you 2 Sep · Rahul Menon')).toBeTruthy()
    expect(disclosure().textContent).toContain('Applications using this data')
    expect(disclosure().textContent).toContain('2')
  })

  it('offers the ask, and nothing to disclose, to somebody who has never asked', async () => {
    h.listConnectors.mockResolvedValue([
      entry({ state: 'neverAsked', approvedAt: null, approvedByName: null, onProjectCount: null, onProjects: [] }),
    ])
    render(<IntegrationsPage />)

    // Liveness first: the card really rendered and really offers the ask, so the absence below
    // is the disclosure being withheld rather than a page that failed to mount.
    expect(await screen.findByTestId('connector-ask-orbit')).toBeTruthy()
    expect(screen.getByText('You do not have access yet')).toBeTruthy()
    expect(screen.queryByTestId('connector-disclosure-orbit')).toBeNull()
    expect(screen.queryByText('Applications using this data')).toBeNull()
  })

  it('shows the wait after asking, and the way to take the request back', async () => {
    h.requestConnectorAccess.mockResolvedValue(entry(WAITING))
    h.listConnectors
      .mockResolvedValueOnce([entry(NEVER_ASKED)])
      .mockResolvedValue([entry(WAITING)])
    render(<IntegrationsPage />)

    fireEvent.click(await screen.findByTestId('connector-ask-orbit'))
    const remarks = await screen.findByLabelText('Why you need access to ORBIT')
    fireEvent.change(remarks, {
      target: { value: 'I build the departures board the duty managers use every shift' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Ask an administrator' }))

    expect(await screen.findByTestId('connector-waiting-orbit')).toBeTruthy()
    expect(h.requestConnectorAccess).toHaveBeenCalledWith(
      'orbit',
      'I build the departures board the duty managers use every shift',
    )
    // The ask panel is gone and the card is showing, so this is the waiting card's own control.
    expect(screen.getByTestId('connector-cancel-orbit').textContent).toBe('Cancel this request')
    expect(screen.queryByRole('button', { name: 'Ask an administrator' })).toBeNull()
  })
})

describe('a waiting request can be taken back', () => {
  it('withdraws it, and the card comes back offering the ask', async () => {
    h.cancelConnectorRequest.mockResolvedValue(entry(NEVER_ASKED))
    h.listConnectors
      .mockResolvedValueOnce([entry(WAITING)])
      .mockResolvedValue([entry(NEVER_ASKED)])
    render(<IntegrationsPage />)

    fireEvent.click(await screen.findByTestId('connector-cancel-orbit'))
    await waitFor(() => expect(h.cancelConnectorRequest).toHaveBeenCalledWith('orbit'))
    // THE STATE THE SERVER REPORTS, NOT ONE THE PRESS ASSUMED — which is why the card is read
    // back rather than patched: `neverAsked` is a derivation over the person's remaining rows.
    expect(await screen.findByTestId('connector-ask-orbit')).toBeTruthy()
    expect(screen.queryByTestId('connector-waiting-orbit')).toBeNull()
  })

  it('states the server’s reason when the request was answered first, and lands on the truth', async () => {
    // `409 nothing_pending`: an administrator approved it between the press and the write. The
    // press arrived late rather than failed, so the card must not stay on the wait it hoped to
    // end — it has to show what actually holds now.
    h.cancelConnectorRequest.mockRejectedValue(
      new Error('There is no waiting request to cancel.'),
    )
    h.listConnectors.mockResolvedValueOnce([entry(WAITING)]).mockResolvedValue([entry()])
    render(<IntegrationsPage />)

    fireEvent.click(await screen.findByTestId('connector-cancel-orbit'))
    await waitFor(() =>
      expect(screen.getByTestId('integrations-write-failure').textContent).toContain(
        'There is no waiting request to cancel.',
      ),
    )
    expect(await screen.findByTestId('connector-granted-orbit')).toBeTruthy()
    expect(screen.queryByTestId('connector-waiting-orbit')).toBeNull()
  })

  it.each([
    ['neverAsked', NEVER_ASKED, 'connector-ask-orbit'],
    ['approved', {}, 'connector-granted-orbit'],
    [
      'declined',
      {
        state: 'declined' as const,
        approvedAt: null,
        approvedByName: null,
        onProjectCount: null,
        onProjects: [],
        decidedAt: '2026-09-02T09:15:00Z',
        decidedByName: 'Rahul Menon',
        decisionRemarks: 'Ask again once the safety review closes.',
      },
      'connector-card-orbit',
    ],
  ])('is offered in no other state — %s', async (_state, over, liveness) => {
    h.listConnectors.mockResolvedValue([entry(over)])
    render(<IntegrationsPage />)

    // Liveness first: the card really rendered in this state, so the absence below is the
    // control being withheld rather than a page that failed to mount.
    expect(await screen.findByTestId(liveness)).toBeTruthy()
    expect(screen.queryByTestId('connector-cancel-orbit')).toBeNull()
  })
})

describe('the disclosure lists what is switched on, and only that', () => {
  it('lists exactly the applications the server named, each with a switch', async () => {
    render(<IntegrationsPage />)
    await openDisclosure()

    expect(screen.getByTestId('connector-app-p1').textContent).toContain('Flight Delay Reason Capture')
    expect(screen.getByTestId('connector-app-p2').textContent).toContain('Baggage Belt Downtime Tracker')
    expect(screen.getAllByRole('switch')).toHaveLength(2)
    expect(screen.getByLabelText('Read ORBIT in Flight Delay Reason Capture')).toBeTruthy()
  })

  it('is genuinely absent while closed — not present at zero height', async () => {
    render(<IntegrationsPage />)
    // Liveness: the trigger is there and says there are two, so the rows being missing is the
    // disclosure being shut rather than an empty answer.
    expect((await screen.findByTestId('connector-disclosure-orbit')).textContent).toContain('2')
    expect(screen.queryByTestId('connector-app-p1')).toBeNull()
  })

  it('switching one off writes it, then drops it on the next read', async () => {
    h.listConnectors
      .mockResolvedValueOnce([entry()])
      .mockResolvedValue([
        entry({ onProjectCount: 1, onProjects: [{ projectId: 'p2', name: 'Baggage Belt Downtime Tracker' }] }),
      ])
    render(<IntegrationsPage />)
    await openDisclosure()

    fireEvent.click(screen.getByLabelText('Read ORBIT in Flight Delay Reason Capture'))
    fireEvent.click(await screen.findByTestId('connector-turn-off-confirm'))
    await waitFor(() =>
      expect(h.setProjectConnector).toHaveBeenCalledWith('p1', 'orbit', { enabled: false }),
    )
    await waitFor(() => expect(screen.queryByTestId('connector-app-p1')).toBeNull())
    // Paired: the OTHER application is still listed, so the row left because the read no longer
    // names it and not because the list emptied.
    expect(screen.getByTestId('connector-app-p2')).toBeTruthy()
    expect(disclosure().textContent).toContain('1')
  })

  it('an approval with nothing switched on gets an honest empty line, not a spinner', async () => {
    h.listConnectors.mockResolvedValue([entry({ onProjectCount: 0, onProjects: [] })])
    render(<IntegrationsPage />)
    await openDisclosure()

    expect(screen.getByTestId('connector-none-on-orbit').textContent).toContain(
      'No application has this switched on yet',
    )
    expect(disclosure().textContent).toContain('0')
    // Paired absence: the empty line is showing, so no skeleton means the page settled rather
    // than that nothing mounted.
    expect(screen.queryByTestId('connector-cards-loading')).toBeNull()
  })

  it('keeps the applications listed when the person-level grant is gone', async () => {
    // THE DELIBERATE CONSEQUENCE of filtering on the application's own switch: a withdrawn grant
    // leaves `onProjectCount` null while the applications stay named. The access position is the
    // ONE place that says access is gone; rows disappearing would leave the page short for a
    // reason it never gives.
    h.listConnectors.mockResolvedValue([
      entry({ state: 'neverAsked', approvedAt: null, approvedByName: null, onProjectCount: null }),
    ])
    render(<IntegrationsPage />)

    expect(await screen.findByTestId('connector-ask-orbit')).toBeTruthy()
    await openDisclosure()
    expect(screen.getByTestId('connector-app-p1')).toBeTruthy()
    expect(screen.getByTestId('connector-app-p2')).toBeTruthy()
    // The count comes off the LIST, not off `onProjectCount` — which the server sends as null here.
    expect(disclosure().textContent).toContain('2')
  })
})

describe('the page states who may read the data, and never what was read', () => {
  it('carries no record count, no last-read date and no range anywhere', async () => {
    render(<IntegrationsPage />)
    await openDisclosure()

    // LIVENESS FIRST. The card, the pill and both application rows are on screen, so the
    // absences below are this page's subject rather than a page that failed to render.
    expect(screen.getByTestId('connector-card-orbit')).toBeTruthy()
    expect(screen.getByTestId('connector-granted-orbit')).toBeTruthy()
    expect(screen.getByTestId('connector-app-p1')).toBeTruthy()

    const page = screen.getByTestId('integrations-page').textContent ?? ''
    expect(page).not.toMatch(/records?\b/i)
    expect(page).not.toMatch(/last read|last-read|read on/i)
    expect(page).not.toMatch(/\b\d+\s*days?\b/i)
    expect(page).not.toMatch(/\brange/i)
    expect(page).not.toMatch(/data available/i)
    // …and no date chip: the drill-down's window control has no home on this page.
    expect(screen.queryByRole('button', { name: /^Days ORBIT reads/ })).toBeNull()
  })
})

describe('failures are said out loud, and a failed write puts the switch back', () => {
  it('states a failed read and offers a retry that succeeds', async () => {
    h.listConnectors
      .mockRejectedValueOnce(new Error('Failed to load your integrations (500).'))
      .mockResolvedValue([entry()])
    render(<IntegrationsPage />)

    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('Failed to load your integrations (500).')
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    expect(await screen.findByTestId('connector-card-orbit')).toBeTruthy()
  })

  it('reverts the switch and says why when the write is refused', async () => {
    h.setProjectConnector.mockRejectedValue(new Error('You do not have access to this yet.'))
    render(<IntegrationsPage />)
    await openDisclosure()

    const control = screen.getByLabelText('Read ORBIT in Flight Delay Reason Capture')
    fireEvent.click(control)
    fireEvent.click(await screen.findByTestId('connector-turn-off-confirm'))
    await waitFor(() =>
      expect(screen.getByTestId('integrations-write-failure').textContent).toContain(
        'You do not have access to this yet.',
      ),
    )
    // Back where the server still has it — a switch left in the position the citizen chose would
    // be the page disagreeing with the server it just heard from.
    expect(control.getAttribute('data-state')).toBe('checked')
    expect(screen.getByTestId('connector-app-p1').textContent).toContain('On')
  })

  it('★ asks before cutting an application off, and a cancel leaves it reading', async () => {
    // WHAT THIS FIXES: the row VANISHED on the press — count down, no message, no undo anywhere
    // on the page. For a live application that is a production data source going away under
    // whoever is using it, and the way back is to remember which application it was and find its
    // own Settings. Less friction than renaming it had.
    render(<IntegrationsPage />)
    await openDisclosure()

    const control = screen.getByLabelText('Read ORBIT in Flight Delay Reason Capture')
    fireEvent.click(control)
    expect(await screen.findByTestId('connector-turn-off-confirm')).toBeTruthy()
    expect(h.setProjectConnector).not.toHaveBeenCalled()

    fireEvent.click(screen.getByTestId('connector-turn-off-cancel'))
    await waitFor(() => expect(screen.queryByTestId('connector-turn-off-confirm')).toBeNull())
    // Still on, still listed, still nothing written — a cancel that wrote anyway is precisely
    // what an absence assertion alone would not catch.
    expect(h.setProjectConnector).not.toHaveBeenCalled()
    expect(control.getAttribute('data-state')).toBe('checked')
    expect(screen.getByTestId('connector-app-p1').textContent).toContain('On')
  })

  it('★ writes only an off — this list has no on to give', async () => {
    // The handler ran the write whatever value the switch handed it. Nothing on this page can
    // hand it `true` — a row leaves the list the moment it is switched off — so the branch was
    // unreachable rather than wrong, and unreachable branches stop being unreachable.
    render(<IntegrationsPage />)
    await openDisclosure()

    fireEvent.click(screen.getByLabelText('Read ORBIT in Flight Delay Reason Capture'))
    fireEvent.click(await screen.findByTestId('connector-turn-off-confirm'))

    await waitFor(() => expect(h.setProjectConnector).toHaveBeenCalledTimes(1))
    expect(h.setProjectConnector).toHaveBeenCalledWith('p1', 'orbit', { enabled: false })
  })

  it('★ …and flipping it back before the list catches up writes nothing at all', async () => {
    // THE WINDOW THE GUARD IS FOR, and it is not hypothetical: the flip is optimistic, so between
    // the write settling and the next read dropping the row, the switch sits OFF on a row still on
    // screen. Flipping it there hands the handler `true` — and the handler used to run its write
    // regardless of the value, sending a second `enabled: false` for a press that asked for on.
    render(<IntegrationsPage />)
    await openDisclosure()

    const control = screen.getByLabelText('Read ORBIT in Flight Delay Reason Capture')
    fireEvent.click(control)
    fireEvent.click(await screen.findByTestId('connector-turn-off-confirm'))
    await waitFor(() => expect(control.getAttribute('data-state')).toBe('unchecked'))

    fireEvent.click(control)

    // No second question, and no second write.
    expect(screen.queryByTestId('connector-turn-off-confirm')).toBeNull()
    expect(h.setProjectConnector).toHaveBeenCalledTimes(1)
  })
})

describe('the disclosure is reachable by keyboard, and focus does not look like open', () => {
  it('is a native button that takes focus and toggles, with a ring that is not a ground', async () => {
    render(<IntegrationsPage />)
    const trigger = await screen.findByTestId('connector-disclosure-orbit')

    trigger.focus()
    expect(document.activeElement).toBe(trigger)
    // ENTER AND SPACE ARE THE BUTTON'S OWN ACTIVATION, and jsdom performs no default action for
    // a key — so what is pinned is that this really is a native `<button type="button">`, for
    // which the browser fires both, rather than a div wearing a role, for which it fires
    // neither. The toggle itself is exercised below through the activation those keys produce.
    expect(trigger.tagName).toBe('BUTTON')
    expect(trigger.getAttribute('type')).toBe('button')

    expect(trigger.getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(trigger)
    await waitFor(() => expect(trigger.getAttribute('aria-expanded')).toBe('true'))
    fireEvent.click(trigger)
    await waitFor(() => expect(trigger.getAttribute('aria-expanded')).toBe('false'))

    // THREE MECHANISMS, NOT TWO. The closed ground and the open ground already differ; focus
    // must differ from BOTH, which a ring does and a third background would not.
    const cls = trigger.className
    expect(cls).toContain('bg-surface-muted')
    expect(cls).toContain('data-[state=open]:bg-white')
    expect(cls).toMatch(/focus-visible:ring-2/)
    expect(cls).not.toMatch(/focus-visible:bg-/)
  })
})

/**
 * ONE STORE BEHIND BOTH SURFACES. `setProjectConnector` writes it and both reads answer from it,
 * so a switch flipped on one screen is genuinely what the other screen's next read returns —
 * rather than two mocks that were separately told to agree.
 */
describe('the page and Settings › Integrations read one source', () => {
  const PROJECT: Project = {
    id: 'p1',
    name: 'Flight Delay Reason Capture',
    description: '',
    appId: null,
    appStatus: null,
    hasRelaunchableSnapshot: null,
    hasSavedSnapshot: null,
    isServing: false,
    createdAt: '2026-07-10T00:00:00Z',
    updatedAt: '2026-07-10T00:00:00Z',
    access: 'owner',
  }

  const WINDOW = {
    kind: 'relative' as const,
    start: '2026-09-01',
    end: '2026-09-15',
    days: 15,
    clamped: false,
    earliestDate: '2026-08-17',
    latestDate: '2026-09-15',
    stored: { days: 15, start: null, end: null },
  }

  /** The server's `project_connectors` rows, for this one connector. */
  let switches: Record<string, boolean>

  const projectRow = (projectId: string): ProjectConnectorEntry => ({
    key: 'orbit',
    displayName: 'ORBIT',
    dataNoun: 'flight data',
    state: 'approved',
    askedAt: null,
    enabled: switches[projectId] ?? false,
    effectivelyOn: switches[projectId] ?? false,
    window: WINDOW,
  })

  beforeEach(() => {
    switches = { p1: true, p2: true }
    h.listShares.mockResolvedValue([])
    h.getDeployment.mockResolvedValue(null)
    h.listConnectors.mockImplementation(() =>
      Promise.resolve([
        entry({
          onProjects: [
            { projectId: 'p1', name: 'Flight Delay Reason Capture' },
            { projectId: 'p2', name: 'Baggage Belt Downtime Tracker' },
          ].filter((p) => switches[p.projectId]),
          onProjectCount: Object.values(switches).filter(Boolean).length,
        }),
      ]),
    )
    h.listProjectConnectors.mockImplementation((projectId: string) =>
      Promise.resolve([projectRow(projectId)]),
    )
    h.setProjectConnector.mockImplementation(
      (projectId: string, _key: string, update: { enabled: boolean }) => {
        switches[projectId] = update.enabled
        return Promise.resolve(projectRow(projectId))
      },
    )
  })

  const openSettingsIntegrations = async () => {
    render(
      <AppSettingsDialog
        project={PROJECT}
        onProjectUpdate={vi.fn()}
        onClose={vi.fn()}
        onDelete={vi.fn()}
      />,
    )
    fireEvent.mouseDown(await screen.findByTestId('settings-tab-integrations'))
    fireEvent.click(screen.getByTestId('settings-tab-integrations'))
    return screen.findByTestId('app-connector-orbit')
  }

  it('a switch flipped on the page is off in that application’s settings tab', async () => {
    render(<IntegrationsPage />)
    await openDisclosure()
    fireEvent.click(screen.getByLabelText('Read ORBIT in Flight Delay Reason Capture'))
    fireEvent.click(await screen.findByTestId('connector-turn-off-confirm'))
    await waitFor(() => expect(switches.p1).toBe(false))
    await waitFor(() => expect(screen.queryByTestId('connector-app-p1')).toBeNull())
    cleanup()

    await openSettingsIntegrations()
    expect(screen.getByLabelText('Read ORBIT in this application').getAttribute('data-state')).toBe(
      'unchecked',
    )
    expect(screen.getByText('Switch it on when a chat needs flight data')).toBeTruthy()
  })

  it('and the reverse: a switch flipped in the settings tab is listed on the page', async () => {
    switches = { p1: false, p2: true }

    await openSettingsIntegrations()
    fireEvent.click(screen.getByLabelText('Read ORBIT in this application'))
    await waitFor(() => expect(switches.p1).toBe(true))
    cleanup()

    render(<IntegrationsPage />)
    await openDisclosure()
    expect(screen.getByTestId('connector-app-p1')).toBeTruthy()
    expect(disclosure().textContent).toContain('2')
  })
})
