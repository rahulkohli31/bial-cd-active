/**
 * THE RAIL AND THE DIALOG OVER IT — the one claim a component test structurally cannot make.
 *
 * WHY THIS FILE EXISTS. `Manage integrations →` opens the Integrations dialog OVER the rail, and
 * the drill-down inside it can switch THIS project's connector on or off. That write reloads the
 * drill-down and nothing else, so the rail underneath keeps its old switch, its old chip and its
 * old count until something tells it to look again. A component test cannot see the bug: the
 * harness would feed both renders the same fixture and pass either way. So this file runs the
 * whole path against ONE in-memory server that both surfaces read and one of them writes.
 *
 * MUTATION RECEIPT: delete `dataSection.current?.reload()` from `WorkspaceRail`'s `onClose` and
 * the first test here goes red on all three of the switch, the chip and the count.
 *
 * IT IS ALSO WHERE "THE SAME DIALOG" IS PROVED. `AppShell.test.tsx` opens it from the
 * navigation's Integrations entry; this opens the same testid from the rail and finds the same
 * body. One component, two doors, no new route.
 *
 * AND WHERE THE APP PANE'S ABSENCE IS PINNED. The `NoAccess` board draws a paragraph inside the
 * pane about flight data; none of it is built (owner ruling), so the last test here is a SOURCE
 * scan proving the pane and the workspace state machine know nothing about connectors at all —
 * a claim no render assertion can make, because it has to hold on branches no test mounts.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { readFileSync } from 'node:fs'
import { render, screen, fireEvent, cleanup, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

const h = vi.hoisted(() => ({
  listProjectConnectors: vi.fn(),
  listConnectors: vi.fn(),
  listConnectorProjects: vi.fn(),
  setProjectConnector: vi.fn(),
}))
vi.mock('../../../utils/connectorApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../../utils/connectorApi')>()),
  listProjectConnectors: h.listProjectConnectors,
  listConnectors: h.listConnectors,
  listConnectorProjects: h.listConnectorProjects,
  setProjectConnector: h.setProjectConnector,
}))
vi.mock('../../PublishStatusChip', () => ({
  default: () => <span data-testid="publish-chip-stub" />,
}))
vi.mock('../../projects/ProjectDescriptionEditor', () => ({
  default: () => <div data-testid="description-editor" />,
}))
vi.mock('../../../utils/auth', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../../utils/auth')>()),
  getStoredUser: () => ({
    chat_kinds: [
      { value: 'plan', name: 'Plan', description: 'Shape a plan first.' },
      { value: 'build', name: 'Build', description: 'Change the live app.' },
    ],
  }),
}))

import WorkspaceRail from '../WorkspaceRail'
import type { Project } from '../../../utils/projectApi'
import type {
  ConnectorEntry,
  ConnectorProjectEntry,
  ConnectorWindow,
  ProjectConnectorEntry,
  WindowChoice,
} from '../../../utils/connectorApi'

const PROJECT: Project = {
  id: 'p1',
  name: 'VIP Movement',
  description: 'A tracked movement.',
  appId: 'a1',
  appStatus: null,
  hasRelaunchableSnapshot: true,
  hasSavedSnapshot: null,
  isServing: false,
  createdAt: '2026-07-10T00:00:00Z',
  updatedAt: '2026-07-10T00:00:00Z',
  access: 'owner',
}

const september: ConnectorWindow = {
  kind: 'absolute',
  start: '2026-09-01',
  end: '2026-09-30',
  days: 30,
  clamped: false,
  earliestDate: '2026-08-01',
  latestDate: '2026-09-30',
  stored: { days: null, start: '2026-09-01', end: '2026-09-30' },
}

/**
 * THE WHOLE SERVER, in one mutable object: which of this citizen's projects have ORBIT switched
 * on. Every mock below answers OUT of this, and the drill-down's write is what changes it — so
 * the rail's re-read is a genuine second look at a moved fact rather than a second fixture.
 */
const server = {
  approved: true,
  on: new Map<string, boolean>(),
  names: new Map([
    ['p1', 'VIP Movement'],
    ['p2', 'Bay Occupancy — T1'],
  ]),
}

function projectRow(projectId: string): ProjectConnectorEntry {
  const enabled = server.on.get(projectId) === true
  return {
    key: 'orbit',
    displayName: 'ORBIT',
    dataNoun: 'flight data',
    state: server.approved ? 'approved' : 'neverAsked',
    askedAt: null,
    enabled,
    // The SERVER's conjunction, answered once here exactly as `resolve_window` answers it — the
    // browser never recomputes it, which is the point of shipping the field.
    effectivelyOn: enabled && server.approved,
    window: enabled ? september : null,
  }
}

function personRow(): ConnectorEntry {
  return {
    key: 'orbit',
    displayName: 'ORBIT',
    subtitle: 'Airport operations',
    askSubtitle: 'ORBIT is BIAL’s airport operations data.',
    consentLinesRequester: [{ lead: 'Read-only.', body: 'Nothing you build can change it.' }],
    state: 'approved',
    askedAt: null,
    approvedAt: '2026-09-02T12:00:00.000Z',
    approvedByName: 'Rahul Menon',
    onProjectCount: [...server.on.values()].filter(Boolean).length,
    decidedAt: null,
    decidedByName: null,
    decisionRemarks: null,
  }
}

function drillDownRows(): { projects: ConnectorProjectEntry[]; truncated: boolean } {
  return {
    projects: [...server.names].map(([projectId, name]) => {
      const entry = projectRow(projectId)
      return { projectId, name, enabled: entry.enabled, window: entry.window }
    }),
    truncated: false,
  }
}

function renderRail() {
  return render(
    <MemoryRouter>
      <WorkspaceRail project={PROJECT} save={null} onProjectUpdate={() => {}} />
    </MemoryRouter>,
  )
}

const railSwitch = () => screen.getByRole('switch', { name: 'Read ORBIT in this project' })
const railChip = () => screen.queryByRole('button', { name: /Days ORBIT reads in this project/ })
const count = () => screen.getByTestId('data-on-count').textContent

beforeEach(() => {
  vi.clearAllMocks()
  server.approved = true
  server.on = new Map([
    ['p1', true],
    ['p2', false],
  ])
  h.listProjectConnectors.mockImplementation((projectId: string) =>
    Promise.resolve([projectRow(projectId)]),
  )
  h.listConnectors.mockImplementation(() => Promise.resolve([personRow()]))
  h.listConnectorProjects.mockImplementation(() => Promise.resolve(drillDownRows()))
  h.setProjectConnector.mockImplementation(
    (projectId: string, _key: string, update: { enabled: boolean; window?: WindowChoice }) => {
      server.on.set(projectId, update.enabled)
      return Promise.resolve(projectRow(projectId))
    },
  )
})
afterEach(() => cleanup())

/** Rail → `Manage integrations →` → the connector list → the drill-down. */
async function drillIn() {
  fireEvent.click(await screen.findByRole('button', { name: /Manage integrations/ }))
  await screen.findByTestId('integrations-dialog')
  fireEvent.click(await screen.findByTestId('connector-projects-orbit'))
  return screen.findByTestId('connector-projects-panel')
}

describe('the rail re-reads when the dialog closes over it', () => {
  it('★ switch, chip and count all follow a toggle made inside the dialog', async () => {
    renderRail()
    await screen.findByTestId('data-section-list')
    expect(railSwitch().getAttribute('data-state')).toBe('checked')
    expect(railChip()).toBeTruthy()
    expect(count()).toBe('On')

    await drillIn()
    fireEvent.click(screen.getByRole('switch', { name: 'Read ORBIT in VIP Movement' }))
    await waitFor(() => expect(server.on.get('p1')).toBe(false))

    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    await waitFor(() => expect(screen.queryByTestId('integrations-dialog')).toBeNull())

    // All three moved, and none of them could have without the reload the rail wires to `onClose`.
    await waitFor(() => expect(railSwitch().getAttribute('data-state')).toBe('unchecked'))
    expect(railChip()).toBeNull()
    expect(count()).toBe('none on')
    expect(screen.getByText('Switch it on when a chat needs flight data')).toBeTruthy()
  })

  it('★ …and back the other way, so the assertion is not passing on a one-way default', async () => {
    server.on = new Map([['p1', false], ['p2', false]])
    renderRail()
    await screen.findByTestId('data-section-list')
    expect(count()).toBe('none on')

    await drillIn()
    fireEvent.click(screen.getByRole('switch', { name: 'Read ORBIT in VIP Movement' }))
    await waitFor(() => expect(server.on.get('p1')).toBe(true))
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    await waitFor(() => expect(screen.queryByTestId('integrations-dialog')).toBeNull())

    await waitFor(() => expect(count()).toBe('On'))
    expect(railSwitch().getAttribute('data-state')).toBe('checked')
    expect(screen.getByText('Reading 30 days of flight data')).toBeTruthy()
  })

  it('a toggle on ANOTHER project changes the dialog\'s count and leaves this rail alone', async () => {
    renderRail()
    await screen.findByTestId('data-section-list')

    await drillIn()
    fireEvent.click(screen.getByRole('switch', { name: 'Read ORBIT in Bay Occupancy — T1' }))
    await waitFor(() => expect(server.on.get('p2')).toBe(true))
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    await waitFor(() => expect(screen.queryByTestId('integrations-dialog')).toBeNull())

    // This project was never touched, so the rail reads exactly as it did — and it re-read to
    // find that out rather than assuming it.
    expect(count()).toBe('On')
    expect(railSwitch().getAttribute('data-state')).toBe('checked')
    expect(h.listProjectConnectors).toHaveBeenCalledTimes(2)
  })
})

describe('one dialog, two doors', () => {
  it('opens the SAME dialog the navigation opens — no new route, no navigation', async () => {
    // `AppShell.test.tsx` opens this testid from the navigation's Integrations entry. Same
    // component, same body, reached from the rail.
    renderRail()

    fireEvent.click(await screen.findByRole('button', { name: /Manage integrations/ }))

    const dialog = await screen.findByTestId('integrations-dialog')
    expect(within(dialog).getByTestId('connector-list-body')).toBeTruthy()
    expect(within(dialog).getByRole('heading', { name: 'Integrations' })).toBeTruthy()
    expect(within(dialog).getByText('ORBIT')).toBeTruthy()
    // Still the same screen underneath — queried by TEXT because a modal Radix dialog puts
    // `aria-hidden` over everything outside itself while it is open.
    expect(screen.getByTestId('description-editor')).toBeTruthy()
  })

  it('★ state c\'s Request → lands on the connector LIST, never straight in the ask panel', async () => {
    server.approved = false
    server.on = new Map([['p1', false], ['p2', false]])
    h.listConnectors.mockImplementation(() =>
      Promise.resolve([
        { ...personRow(), state: 'neverAsked' as const, approvedAt: null, approvedByName: null, onProjectCount: null },
      ]),
    )
    renderRail()
    await screen.findByText('You do not have access to ORBIT yet')

    fireEvent.click(screen.getByRole('button', { name: /Request/ }))

    const dialog = await screen.findByTestId('integrations-dialog')
    // The list, with its consent-bearing `Request access` still ahead of them…
    expect(within(dialog).getByTestId('connector-list-body')).toBeTruthy()
    expect(within(dialog).getByRole('button', { name: 'Request access' })).toBeTruthy()
    // …and NOT the ask panel itself, which would have skipped the connector list entirely.
    expect(screen.queryByTestId('ask-access-panel')).toBeNull()
  })
})

describe('the rail\'s shape, with the section in it', () => {
  it('renders START A CHAT → DATA → APP STATUS → DESCRIPTION, with a hairline between each', async () => {
    const { container } = renderRail()
    await screen.findByTestId('data-section-list')

    const labels = Array.from(container.querySelectorAll('h2')).map((node) => node.textContent)
    // The description's own heading belongs to the editor, which this file stubs — its presence
    // stands for the fourth section, exactly as `WorkspaceRail.test.tsx` reads it.
    expect(labels).toEqual(['START A CHAT', 'DATA', 'APP STATUS'])
    expect(screen.getByTestId('description-editor')).toBeTruthy()
    expect(container.querySelectorAll('div.h-px').length).toBe(3)
  })

  it('★ leaves the app pane and the workspace state machine knowing nothing about connectors', () => {
    // THE OWNER'S RULING, GUARDED AT THE SOURCE. `NoAccess` draws `This app has no flight data
    // yet` inside the pane with three lines about the connector under it; none of it ships. The
    // rail row three inches away already carries that message and the control that answers it,
    // and the board's wording claims to know the app NEEDS flight data, which the platform
    // cannot know. A render assertion could not make this claim — it would only cover the states
    // a test happens to mount — so the files are read instead.
    const files = ['components/workspace/AppPane.tsx', 'components/workspace/workspaceState.ts']
    const offending = files.flatMap((file) =>
      readFileSync(`src/${file}`, 'utf8')
        .split('\n')
        .flatMap((line, i) =>
          /\b(?:connector|integrations|flight data)\b/i.test(line) ? [`${file}:${i + 1}`] : [],
        ),
    )
    expect(offending).toEqual([])
    // Liveness for that absence: the files really were read, and they are the ones we think.
    expect(readFileSync('src/components/workspace/AppPane.tsx', 'utf8')).toContain('never-built')
    expect(readFileSync('src/components/workspace/workspaceState.ts', 'utf8')).toContain(
      'PREVIEW_PROBE_MS',
    )
  })
})
