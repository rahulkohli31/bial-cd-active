/**
 * THE DATA SECTION — the four project states, the derived count, and the two rows that are not
 * drawn.
 *
 * THE FIXTURE CONNECTOR IS `ORBIT`, NOT THE REAL ONE, for the reason `IntegrationsDialog.test`
 * and `ConnectorProjectsPanel.test` both give: every string this section says about a connector
 * comes off the wire, and a component that had the real name written into it would still satisfy
 * a suite that asserted the real name (R18).
 *
 * TWO ASSERTIONS HERE EXIST TO CATCH A HARD-CODED STRING, and both are named at their test:
 * `Reading N days` must count the RESOLVED window (a `Last 7 days` project reads 7), and the
 * on-count must be computed from the entries (`On` with one on, `none on` with none, `1 of 2 on`
 * the day a second connector exists). Freezing either is invisible on the board `Main` draws.
 *
 * EVERY ABSENCE ASSERTION IS PAIRED WITH A LIVENESS ONE. "No chip", "no second row" and "nothing
 * happened" are all satisfied by a component that threw on mount, and this repo has been bitten
 * by exactly that.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor, within, act } from '@testing-library/react'

const h = vi.hoisted(() => ({
  listProjectConnectors: vi.fn(),
  setProjectConnector: vi.fn(),
}))
vi.mock('../../../utils/connectorApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../../utils/connectorApi')>()),
  listProjectConnectors: h.listProjectConnectors,
  setProjectConnector: h.setProjectConnector,
}))

import DataSection, { onCountLabel } from '../DataSection'
import { ApiError } from '../../../utils/apiError'
import { notifyConnectorsChanged } from '../../../utils/connectorApi'
import type { ConnectorWindow, ProjectConnectorEntry } from '../../../utils/connectorApi'

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

const lastSeven: ConnectorWindow = {
  kind: 'relative',
  start: '2026-09-24',
  end: '2026-09-30',
  days: 7,
  clamped: false,
  earliestDate: '2026-08-01',
  latestDate: '2026-09-30',
  stored: { days: 7, start: null, end: null },
}

/** State a — approved, switched on, reading a resolved window. */
const on: ProjectConnectorEntry = {
  key: 'orbit',
  displayName: 'ORBIT',
  dataNoun: 'flight data',
  state: 'approved',
  askedAt: null,
  enabled: true,
  effectivelyOn: true,
  window: september,
}

/** State b — approved, switched off. No window, so nothing to draw a chip from. */
const off: ProjectConnectorEntry = {
  ...on,
  enabled: false,
  effectivelyOn: false,
  window: null,
}

/** State c — never asked. */
const noAccess: ProjectConnectorEntry = {
  ...off,
  state: 'neverAsked',
}

/** State d — asked, waiting on an administrator. */
const waiting: ProjectConnectorEntry = {
  ...off,
  state: 'pending',
  askedAt: '2026-09-05T08:30:00.000Z',
}

const openIntegrations = vi.fn()

function renderSection() {
  return render(
    <DataSection
      projectId="p1"
      label={<h2>DATA</h2>}
      onOpenIntegrations={openIntegrations}
    />,
  )
}

/** Mounts with one fixture row and waits for it to land. */
async function withEntries(...entries: ProjectConnectorEntry[]) {
  h.listProjectConnectors.mockResolvedValue(entries)
  const result = renderSection()
  await screen.findByTestId('data-section-list')
  return result
}

const row = () => screen.getByTestId('data-connector-orbit')
const theSwitch = () => screen.getByRole('switch', { name: 'Read ORBIT in this project' })

beforeEach(() => {
  vi.clearAllMocks()
  h.listProjectConnectors.mockResolvedValue([on])
  h.setProjectConnector.mockResolvedValue(on)
})
afterEach(() => cleanup())

describe('the four project states, exactly as ConnectorStates draws them', () => {
  it('a · on — the derived sentence, the chip and the switch, all three agreeing', async () => {
    await withEntries(on)

    expect(within(row()).getByText('Reading 30 days of flight data')).toBeTruthy()
    expect(
      within(row()).getByRole('button', { name: /Days ORBIT reads in this project: 1 – 30 Sep/ }),
    ).toBeTruthy()
    expect(theSwitch().getAttribute('data-state')).toBe('checked')
  })

  it('b · off — the invitation, the switch down, and NO chip at all', async () => {
    await withEntries(off)

    expect(within(row()).getByText('Switch it on when a chat needs flight data')).toBeTruthy()
    // The absence…
    expect(screen.queryByRole('button', { name: /Days ORBIT reads/ })).toBeNull()
    // …paired with the liveness that makes it mean something: the row rendered its switch, in
    // the off position, rather than the component having died before it drew anything.
    expect(theSwitch().getAttribute('data-state')).toBe('unchecked')
  })

  it('c · no access — the board sentence naming the connector, and Request →', async () => {
    await withEntries(noAccess)

    expect(within(row()).getByText('You do not have access to ORBIT yet')).toBeTruthy()
    expect(within(row()).getByRole('button', { name: /Request/ })).toBeTruthy()
    // No switch to offer: a row with no access must not draw a control the server would refuse.
    expect(screen.queryByRole('switch')).toBeNull()
    expect(screen.queryByRole('button', { name: /Days ORBIT reads/ })).toBeNull()
  })

  it('d · waiting — the asked-on date, unformatted by any locale, and an inert Waiting', async () => {
    await withEntries(waiting)

    expect(
      within(row()).getByText('You asked for access on 5 Sep — waiting on an administrator'),
    ).toBeTruthy()
    expect(within(row()).getByText('Waiting')).toBeTruthy()
    expect(screen.queryByRole('switch')).toBeNull()
  })

  it('a declined citizen sees the no-access row, whose Request → is the way to the decision', async () => {
    // `declined` is not one of the board's four project states, and this is where it lands: what
    // they have on THIS project is no access. The control points at Integrations, which is where
    // the decline, its date and the administrator's own words are.
    await withEntries({ ...noAccess, state: 'declined' })

    expect(within(row()).getByText('You do not have access to ORBIT yet')).toBeTruthy()
    fireEvent.click(within(row()).getByRole('button', { name: /Request/ }))
    expect(openIntegrations).toHaveBeenCalledTimes(1)
  })
})

describe('the sentences and the count are derived, never written down', () => {
  it('★ a project on Last 7 days reads "Reading 7 days" — the number is the RESOLVED window', async () => {
    // Mutation receipt: hard-code 30 in `stateLine` and this goes red while `Main`'s own
    // 30-day project stays green — which is exactly how a frozen number would ship.
    await withEntries({ ...on, window: lastSeven })

    expect(within(row()).getByText('Reading 7 days of flight data')).toBeTruthy()
    // …and the chip beside it says the same thing, from the same emitter.
    expect(
      within(row()).getByRole('button', { name: /Days ORBIT reads in this project: Last 7 days/ }),
    ).toBeTruthy()
  })

  it('★ the count reads On, and none on, off the entries this section already holds', async () => {
    // Mutation receipt: write `On` down as a literal and the second case goes red.
    await withEntries(on)
    expect(screen.getByTestId('data-on-count').textContent).toBe('On')

    cleanup()
    await withEntries(off)
    expect(screen.getByTestId('data-on-count').textContent).toBe('none on')
  })

  it('★ …and the boards\' "1 of 2 on" is the same expression, once a second connector exists', () => {
    // The registry has one entry, so the list has one row and the section cannot render this
    // today. The RULE is what is asserted: `Main` draws `1 of 2 on` only because it also draws a
    // placeholder connector that is not built, and the day a real second one joins the registry
    // that string comes back with nothing here to edit.
    expect(onCountLabel(1, 2)).toBe('1 of 2 on')
    expect(onCountLabel(2, 2)).toBe('2 of 2 on')
    expect(onCountLabel(0, 2)).toBe('none on')
    expect(onCountLabel(1, 1)).toBe('On')
  })

  it('says nothing about the count until it has read one — no "none on" over a skeleton', () => {
    h.listProjectConnectors.mockReturnValue(new Promise(() => {}))
    renderSection()

    expect(screen.queryByTestId('data-on-count')).toBeNull()
    // Liveness: the section rendered, it just has nothing to count yet.
    expect(screen.getByTestId('data-section-loading')).toBeTruthy()
  })
})

describe('the list is the registry, and the registry has one entry', () => {
  it('★ renders exactly one row, and no greyed placeholder beside it', async () => {
    // Mutation receipt: add the boards' `[ANOTHER BIAL SYSTEM]` row and this goes red. It is
    // drawn on `Main`, `NoAccess` and all four `ConnectorStates` panels, and it is not built
    // (owner ruling, 2026-09-08) — there is no registry entry behind it.
    await withEntries(on)

    expect(within(screen.getByTestId('data-section-list')).getAllByRole('listitem')).toHaveLength(1)
    expect(screen.queryByText(/ANOTHER BIAL SYSTEM/i)).toBeNull()
    expect(screen.queryByText(/Nothing else is connected/i)).toBeNull()
    // …and the one row that IS there is the real one.
    expect(within(row()).getByText('ORBIT')).toBeTruthy()
  })

  it('renders a row per entry when the registry grows, without a second component', async () => {
    await withEntries(on, { ...off, key: 'meteor', displayName: 'METEOR' })

    expect(within(screen.getByTestId('data-section-list')).getAllByRole('listitem')).toHaveLength(2)
    expect(screen.getByTestId('data-on-count').textContent).toBe('1 of 2 on')
  })

  it('says so plainly when nothing is connected at all', async () => {
    h.listProjectConnectors.mockResolvedValue([])
    renderSection()

    expect(await screen.findByText('Nothing is connected to the platform yet.')).toBeTruthy()
    expect(screen.queryByTestId('data-section-list')).toBeNull()
    // Liveness for that absence: the section is alive and still offering its own way out.
    expect(screen.getByRole('button', { name: /Manage integrations/ })).toBeTruthy()
  })
})

describe('the two controls that leave, and the one that does nothing', () => {
  it('Manage integrations → asks the rail to open the dialog', async () => {
    await withEntries(on)

    fireEvent.click(screen.getByRole('button', { name: /Manage integrations/ }))

    expect(openIntegrations).toHaveBeenCalledTimes(1)
  })

  it('★ Request → opens Integrations, and never asks from the project screen', async () => {
    await withEntries(noAccess)

    fireEvent.click(within(row()).getByRole('button', { name: /Request/ }))

    expect(openIntegrations).toHaveBeenCalledTimes(1)
    // It asks for nothing itself: the ask is a person-level act with a consent panel and a
    // required remark in front of it, and none of that lives on this screen.
    expect(h.setProjectConnector).not.toHaveBeenCalled()
  })

  it('★ Waiting is inert — it is not a control, and pressing it calls nothing', async () => {
    await withEntries(waiting)

    const readOut = within(row()).getByText('Waiting')
    // Not a button, so there is nothing for a keyboard or a screen reader to reach for either.
    expect(screen.queryByRole('button', { name: 'Waiting' })).toBeNull()
    fireEvent.click(readOut)

    expect(openIntegrations).not.toHaveBeenCalled()
    expect(h.setProjectConnector).not.toHaveBeenCalled()
    // Liveness: the press changed nothing because there was nothing to change — the row is still
    // rendering its waiting sentence.
    expect(
      within(row()).getByText('You asked for access on 5 Sep — waiting on an administrator'),
    ).toBeTruthy()
  })
})

describe('the write, and what moves with it', () => {
  it('carries the settled server answer into the sentence and the count, not just the switch', async () => {
    await withEntries(off)
    h.setProjectConnector.mockResolvedValue({ ...on, window: lastSeven })

    fireEvent.click(theSwitch())

    expect(await screen.findByText('Reading 7 days of flight data')).toBeTruthy()
    await waitFor(() => expect(screen.getByTestId('data-on-count').textContent).toBe('On'))
    expect(h.setProjectConnector).toHaveBeenCalledWith('p1', 'orbit', { enabled: true })
  })

  it('says a refused write out loud rather than rolling the switch back in silence', async () => {
    await withEntries(off)
    h.setProjectConnector.mockRejectedValue(
      new ApiError('An administrator has not approved you for ORBIT.', 403, 'access_not_approved'),
    )

    fireEvent.click(theSwitch())

    const alert = await screen.findByTestId('data-write-failure')
    expect(alert.textContent).toContain('An administrator has not approved you for ORBIT.')
    // …and the switch went back to what the server still says is true.
    await waitFor(() => expect(theSwitch().getAttribute('data-state')).toBe('unchecked'))
  })
})

describe('the read, before it lands and when it fails', () => {
  it('★ draws a skeleton row rather than a gap that would shift the rail on every open', () => {
    // This read happens on every project navigation and is uncached by design, so the pre-load
    // moment is guaranteed. An empty box between two hairlines also reads as "nothing is
    // connected", which is a different and false statement.
    h.listProjectConnectors.mockReturnValue(new Promise(() => {}))
    renderSection()

    const skeleton = screen.getByTestId('data-section-loading')
    expect(skeleton.getAttribute('aria-busy')).toBe('true')
    expect(within(skeleton).getByText(/Loading this project’s data/)).toBeTruthy()
    expect(screen.queryByTestId('data-section-list')).toBeNull()
  })

  it('shows the failure and a retry, never an empty box reading as "nothing is connected"', async () => {
    h.listProjectConnectors.mockRejectedValueOnce(new ApiError('The network went away.', 503))
    renderSection()

    expect(await screen.findByTestId('data-section-error')).toBeTruthy()
    expect(screen.getByText('The network went away.')).toBeTruthy()
    expect(screen.queryByTestId('data-section-loading')).toBeNull()
    expect(screen.queryByText(/Nothing is connected/)).toBeNull()
    // NOT an `alert`: this read fires on arrival and nobody asked for it, so it reports itself
    // where the answer was going to be — the same weight `AppStatusPanel` gives a failed read one
    // section down. The write failure above is the one that interrupts.
    expect(screen.queryByRole('alert')).toBeNull()

    h.listProjectConnectors.mockResolvedValue([on])
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))

    expect(await screen.findByTestId('data-section-list')).toBeTruthy()
    expect(screen.queryByTestId('data-section-error')).toBeNull()
  })

  it('names the connector’s own data, so a second connector costs a registry entry and no edit here', () => {
    // THE R18 MUTANT. `Reading N days of flight data` is the board's sentence, but "flight data"
    // is the connector's own noun, not the platform's — it rides the wire beside `displayName` for the same
    // reason the ask panel's copy does. Hard-code it back and this goes red.
    h.listProjectConnectors.mockResolvedValue([{ ...on, displayName: 'ORBIT', dataNoun: 'stand allocations' }])
    render(<DataSection projectId="p1" label={<h2>DATA</h2>} onOpenIntegrations={openIntegrations} />)

    return screen.findByText('Reading 30 days of stand allocations')
  })

  it('re-reads when any connector write is announced, which is how BOTH dialog doors are felt', async () => {
    // The section used to expose an imperative `reload()` that only the rail's own door called,
    // so opening Integrations from the profile menu — on this very screen — changed connector
    // state the rail never re-read. The handle is gone; the section subscribes instead, so the
    // door that fired the write no longer has to know this component exists.
    h.listProjectConnectors.mockResolvedValue([off])
    render(<DataSection projectId="p1" label={<h2>DATA</h2>} onOpenIntegrations={openIntegrations} />)
    await screen.findByText('Switch it on when a chat needs flight data')

    h.listProjectConnectors.mockResolvedValue([on])
    act(() => {
      notifyConnectorsChanged()
    })

    expect(await screen.findByText('Reading 30 days of flight data')).toBeTruthy()
  })
})
