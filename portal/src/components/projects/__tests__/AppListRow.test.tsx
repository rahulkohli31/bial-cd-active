/**
 * The two presentational halves both application lists are built from.
 *
 * WHAT THESE EXIST TO PIN is the permission boundary the extraction could have broken: the
 * trailing slot is whatever the caller passed and NOTHING ELSE. An owner-aware branch in here
 * would be how a recipient comes to hold an owner's menu, and the composed suites
 * (`ProjectRow`, `ProjectCard`, `SharedApplicationsPage`) cannot see it — each of them only ever
 * exercises one side of that branch.
 *
 * That the slot is REQUIRED is `tsc`'s job, not a test's: it has no default, so a caller that
 * forgets it does not compile.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import AppListRow from '../AppListRow'
import AppTile from '../AppTile'

describe('the extracted list row', () => {
  it('renders the trailing control it was handed, and adds none of its own', () => {
    render(
      <AppListRow
        testId="probe-row"
        name="Apron Fuel Truck Log"
        description="Fuel uplift per stand."
        onOpen={vi.fn()}
        columns={<span>15 Sep 2026</span>}
        trailing={<button type="button">The caller’s own control</button>}
      />,
    )

    const row = screen.getByTestId('probe-row')
    // Liveness beside the count: the row rendered, and it carries exactly two controls — the
    // name that opens it, and the one the caller passed.
    expect(screen.getByText('Apron Fuel Truck Log')).toBeTruthy()
    expect(row.querySelectorAll('button').length).toBe(2)
    expect(screen.getByRole('button', { name: 'The caller’s own control' })).toBeTruthy()

    cleanup()

    // A DIFFERENT caller, a different control — and still nothing of the row's own.
    render(
      <AppListRow
        testId="probe-row"
        name="Apron Fuel Truck Log"
        description={null}
        onOpen={vi.fn()}
        columns={null}
        trailing={<span>Not a control at all</span>}
      />,
    )
    expect(screen.getByTestId('probe-row').querySelectorAll('button').length).toBe(1)
  })

  it('keeps the trailing control out of the name button', () => {
    render(
      <AppListRow
        testId="probe-row"
        name="Apron Fuel Truck Log"
        description={null}
        onOpen={vi.fn()}
        columns={null}
        trailing={<button type="button">Open</button>}
      />,
    )

    const name = screen.getByRole('button', { name: 'Apron Fuel Truck Log' })
    const trailing = screen.getByRole('button', { name: 'Open' })
    // No nested interactive elements: a browser would forgive it and jsdom would not notice,
    // so the DOM relationship is what gets asserted.
    expect(name.contains(trailing)).toBe(false)
    expect(trailing.contains(name)).toBe(false)
  })
})

describe('the extracted tile', () => {
  it('renders the trailing control it was handed, and adds none of its own', () => {
    render(
      <AppTile
        testId="probe-tile"
        name="Apron Fuel Truck Log"
        description="Fuel uplift per stand."
        onOpen={vi.fn()}
        trailing={<button type="button">The caller’s own control</button>}
        foot={<span>shared 15 Sep</span>}
      />,
    )

    const tile = screen.getByTestId('probe-tile')
    expect(screen.getByText('Apron Fuel Truck Log')).toBeTruthy()
    expect(tile.querySelectorAll('button').length).toBe(2)
    expect(screen.getByText('shared 15 Sep')).toBeTruthy()
  })

  it('says so plainly when there is no description yet', () => {
    render(
      <AppTile
        testId="probe-tile"
        name="Apron Fuel Truck Log"
        description="   "
        onOpen={vi.fn()}
        trailing={<button type="button">Open</button>}
        foot={null}
      />,
    )

    // Whitespace is not a description — the shipped tile treated it as absent and still does.
    expect(screen.getByText('No description yet')).toBeTruthy()
  })
})
