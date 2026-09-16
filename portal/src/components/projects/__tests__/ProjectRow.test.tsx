/**
 * `ProjectRow` — the clipped-text tooltip branch had ZERO automated coverage, because jsdom
 * reports `scrollWidth`/`clientWidth` as `0`/`0` for every element, so `clipped` was always
 * `false` under test and the `Tooltip` branch never rendered. A mutant deleting the whole
 * branch passed the entire suite.
 *
 * `scrollWidth`/`clientWidth` are stubbed per-element via `Object.defineProperty`, which is
 * the only way to force the measurement jsdom cannot produce on its own.
 *
 * THE DESCRIPTION'S TOOLTIP IS GONE and its tests went with it, in the same change. What
 * replaced them is the opposite assertion — that the description offers no hover affordance
 * and no pointer cursor, PAIRED with the row still opening by its name, because an absence
 * assertion on its own passes just as well on a render that crashed. The NAME keeps its
 * tooltip: a clipped name has no other route to its full value, whereas the description now
 * reads two lines of itself and is complete in the DOM either way.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, cleanup, fireEvent } from '@testing-library/react'
import ProjectRow from '../ProjectRow'
import type { Project } from '../../../utils/projectApi'

afterEach(() => cleanup())

const project = (over: Partial<Project> = {}): Project => ({
  id: 'p1',
  name: 'Visitor Log',
  description: 'A short description',
  appId: null,
  appStatus: null,
  hasRelaunchableSnapshot: null,
  hasSavedSnapshot: null,
  isServing: false,
  createdAt: '2026-07-10T00:00:00Z',
  updatedAt: '2026-07-10T00:00:00Z',
  access: 'owner',
  ...over,
})

/** Forces every element's clip measurement in one direction for the lifetime of the test. */
function stubClip(clipped: boolean) {
  vi.spyOn(HTMLElement.prototype, 'scrollWidth', 'get').mockReturnValue(clipped ? 200 : 100)
  vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(100)
}

/**
 * The description stops pretending to be clickable, and becomes readable.
 *
 * It carried a pointer cursor with no keyboard route and hid most of itself behind a hover
 * tooltip, which touch and keyboard readers never reach. What ships is the complete text in the
 * DOM, clipped to two lines by CSS.
 */
describe('ProjectRow — the description', () => {
  const LONG =
    'A visitor log for the north gate that records arrivals, departures, badge numbers and the ' +
    'host each visitor came to see, with a weekly export for the security desk.'

  it('★ keeps its WHOLE text in the accessible tree, clipped only visually', () => {
    // Mutation receipt: replace `line-clamp-2` with a JavaScript `slice(0, 60) + '…'` — the
    // obvious remedy — and this goes red on the first assertion, because the truncation lands
    // in the DOM and a screen reader loses exactly what a sighted reader loses.
    stubClip(true)
    render(<ProjectRow project={project({ description: LONG })} onOpen={vi.fn()} onSettings={vi.fn()} onDelete={vi.fn()} />)

    const description = screen.getByText(LONG)
    expect(description.textContent).toBe(LONG)
    expect(description.className).toMatch(/line-clamp-/)
    expect(description.className).not.toMatch(/(^|\s)truncate(\s|$)/)
  })

  it('★ carries no pointer cursor and no tooltip — and the row still opens by its name', () => {
    // PAIRED DELIBERATELY. `queryByRole('tooltip') === null` passes just as well on a render
    // that threw, so the absence assertions are worthless without the liveness one beneath
    // them: the row is alive and the affordance it does have works.
    stubClip(true)
    const onOpen = vi.fn()
    render(<ProjectRow project={project({ description: LONG })} onOpen={onOpen} onSettings={vi.fn()} onDelete={vi.fn()} />)

    const description = screen.getByText(LONG)
    expect(description.className).not.toMatch(/cursor-pointer/)
    expect(description.getAttribute('title')).toBeNull()
    fireEvent.focus(description)
    fireEvent.mouseOver(description)
    expect(screen.queryByRole('tooltip')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Visitor Log' }))
    expect(onOpen).toHaveBeenCalledTimes(1)
  })

  it('is still part of the row\'s click target', () => {
    // The dead-strip fix's invariant, kept: the description sits above the name button's
    // stretched ::after, so it wires `onOpen` back explicitly rather than relying on an
    // overlay jsdom cannot see.
    const onOpen = vi.fn()
    render(<ProjectRow project={project({ description: LONG })} onOpen={onOpen} onSettings={vi.fn()} onDelete={vi.fn()} />)

    fireEvent.click(screen.getByText(LONG))

    expect(onOpen).toHaveBeenCalledTimes(1)
  })
})

describe('ProjectRow — the name keeps its tooltip', () => {
  it('opens on a really clipped NAME', async () => {
    // A clipped NAME still has no other route to its full value, unlike the description, which
    // now reads two lines of itself and is complete in the DOM regardless.
    stubClip(true)
    render(
      <ProjectRow
        project={project({ name: 'A Very Long Project Name That Has To Clip' })}
        onOpen={vi.fn()}
        onSettings={vi.fn()}
        onDelete={vi.fn()}
      />,
    )

    const name = screen.getByRole('button', { name: 'A Very Long Project Name That Has To Clip' })
    fireEvent.focus(name)

    expect(await screen.findByRole('tooltip')).toBeTruthy()
  })

  it('opens nothing on a name that already fits', () => {
    stubClip(false)
    render(<ProjectRow project={project()} onOpen={vi.fn()} onSettings={vi.fn()} onDelete={vi.fn()} />)

    fireEvent.focus(screen.getByRole('button', { name: 'Visitor Log' }))

    expect(screen.queryByRole('tooltip')).toBeNull()
  })
})

describe('ProjectRow — the ref never remounts across a clipped transition', () => {
  it('keeps the SAME DOM node whether or not the NAME is clipped', () => {
    // Now carried by the name (the description no longer measures anything). The old branch
    // put the ref'd element at a DIFFERENT tree position depending on `clipped` — bare, versus
    // nested inside TooltipProvider/Tooltip/TooltipTrigger — which React treats as a remount.
    // The effect's deps ([measure, text]) do not change on that remount, so a real
    // ResizeObserver (inert in this environment, which is why this checks node IDENTITY rather
    // than the observer firing) never rebinds: false→true worked once, true→false never fired
    // again.
    stubClip(true)
    const { rerender } = render(
      <ProjectRow project={project({ name: 'Clipped today' })} onOpen={vi.fn()} onSettings={vi.fn()} onDelete={vi.fn()} />,
    )
    const before = screen.getByRole('button', { name: 'Clipped today' })

    stubClip(false)
    rerender(<ProjectRow project={project({ name: 'Fits now' })} onOpen={vi.fn()} onSettings={vi.fn()} onDelete={vi.fn()} />)
    const after = screen.getByRole('button', { name: 'Fits now' })

    expect(after).toBe(before) // same DOM node, not a fresh mount
    expect(screen.queryByRole('tooltip')).toBeNull() // and it correctly re-measured as unclipped
  })
})

describe('ProjectRow — a project with no description', () => {
  it('opens the project when the "No description yet" strip is clicked', () => {
    // This branch had `relative z-10` (to sit above the name button's stretched ::after) but
    // no `onClick` — a dead strip across the newest, emptiest projects, the ones most likely
    // to be clicked into.
    const onOpen = vi.fn()
    render(<ProjectRow project={project({ description: null })} onOpen={onOpen} onSettings={vi.fn()} onDelete={vi.fn()} />)

    fireEvent.click(screen.getByText('No description yet'))

    expect(onOpen).toHaveBeenCalled()
  })
})

describe('ProjectRow — no nested interactive elements, still', () => {
  it('keeps the menu out of the name button, even with the tooltip wrapper added', () => {
    // The tooltip restructuring wraps the name in TooltipProvider/Tooltip/TooltipTrigger —
    // worth re-confirming the invariant survives the extra nesting. A browser would forgive a
    // nested button and jsdom would not notice, so the DOM relationship is what is asserted.
    render(<ProjectRow project={project()} onOpen={vi.fn()} onSettings={vi.fn()} onDelete={vi.fn()} />)

    const menu = screen.getByTestId('app-menu-row')
    const open = screen.getByRole('button', { name: 'Visitor Log' })
    expect(open.contains(menu)).toBe(false)
    expect(menu.contains(open)).toBe(false)
    expect(menu.parentElement?.closest('button')).toBeNull()
  })

  it('reaches Delete through the menu, and not by one click on the row', async () => {
    const onOpen = vi.fn()
    const onDelete = vi.fn()
    render(<ProjectRow project={project()} onOpen={onOpen} onSettings={vi.fn()} onDelete={onDelete} />)

    // There is no bare delete control any more: an irreversible action does not get a
    // one-click route from a list.
    expect(screen.queryByLabelText('Delete Visitor Log')).toBeNull()

    fireEvent.pointerDown(screen.getByTestId('app-menu-row'))
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Delete' }))
    expect(onDelete).toHaveBeenCalledTimes(1)
    expect(onOpen).not.toHaveBeenCalled()
  })

  it('opens the menu by keyboard, without opening the project', async () => {
    const onOpen = vi.fn()
    render(<ProjectRow project={project()} onOpen={onOpen} onSettings={vi.fn()} onDelete={vi.fn()} />)
    const trigger = screen.getByTestId('app-menu-row')
    trigger.focus()
    expect(document.activeElement).toBe(trigger)
    fireEvent.keyDown(trigger, { key: 'Enter' })
    expect(await screen.findByRole('menuitem', { name: 'Open' })).toBeTruthy()
    expect(onOpen).not.toHaveBeenCalled()
  })

})

/**
 * THE TWO PRODUCTION ENTRIES. They exist only while the application is serving — which is the
 * precondition the endpoints enforce anyway, so a control offered here in order to be refused
 * teaches a citizen to distrust the screen.
 */
describe('ProjectRow — Restart and Take down', () => {
  it.each([
    ['Restart', 'menu-restart'],
    ['Take down', 'menu-takedown'],
  ])('offers %s only while the application is serving', async (label, testid) => {
    // Absence PAIRED WITH LIVENESS. The menu really opened and really carries its other
    // entries, so this is the two production entries being withheld — not a menu that failed
    // to render, which is what an unpaired `toBeNull()` would also accept.
    render(<ProjectRow project={project({ isServing: false })} onOpen={vi.fn()} onSettings={vi.fn()} onDelete={vi.fn()} />)
    fireEvent.pointerDown(screen.getByTestId('app-menu-row'))
    expect(await screen.findByRole('menuitem', { name: 'Open' })).toBeTruthy()
    expect(screen.queryByTestId(testid)).toBeNull()
    expect(screen.queryByRole('menuitem', { name: label })).toBeNull()
  })

  it('offers both once it IS serving', async () => {
    render(
      <ProjectRow
        project={project({ isServing: true })}
        onOpen={vi.fn()}
        onSettings={vi.fn()}
        onDelete={vi.fn()}
        live={{ onRestart: vi.fn(), onTakeDown: vi.fn(), busy: false }}
      />,
    )
    fireEvent.pointerDown(screen.getByTestId('app-menu-row'))
    expect(await screen.findByTestId('menu-restart')).toBeTruthy()
    expect(screen.getByTestId('menu-takedown')).toBeTruthy()
  })

  it.each([
    ['menu-restart', 'onRestart'],
    ['menu-takedown', 'onTakeDown'],
  ] as const)('runs %s through its own handler', async (testid, handler) => {
    const live = { onRestart: vi.fn(), onTakeDown: vi.fn(), busy: false }
    render(
      <ProjectRow project={project({ isServing: true })} onOpen={vi.fn()} onSettings={vi.fn()} onDelete={vi.fn()} live={live} />,
    )
    fireEvent.pointerDown(screen.getByTestId('app-menu-row'))
    fireEvent.click(await screen.findByTestId(testid))
    expect(live[handler]).toHaveBeenCalledTimes(1)
  })

  it('★ mid-operation the two entries are UNAVAILABLE, not absent', async () => {
    // A control that vanishes while its own action runs reads as a broken screen — the citizen
    // pressed a thing and the thing left. It stays, announced inert, and refuses the press.
    const live = { onRestart: vi.fn(), onTakeDown: vi.fn(), busy: true }
    render(
      <ProjectRow project={project({ isServing: true })} onOpen={vi.fn()} onSettings={vi.fn()} onDelete={vi.fn()} live={live} />,
    )
    fireEvent.pointerDown(screen.getByTestId('app-menu-row'))
    const restart = await screen.findByTestId('menu-restart')
    expect(restart.getAttribute('aria-disabled')).toBe('true')
    expect(screen.getByTestId('menu-takedown').getAttribute('aria-disabled')).toBe('true')

    fireEvent.click(restart)
    fireEvent.click(screen.getByTestId('menu-takedown'))
    expect(live.onRestart).not.toHaveBeenCalled()
    expect(live.onTakeDown).not.toHaveBeenCalled()
  })

})

describe('ProjectRow — the dates', () => {
  it('renders both dates, each absolute, each in its own column', () => {
    render(
      <ProjectRow
        project={{
          ...project(),
          createdAt: '2026-08-12T09:00:00Z',
          updatedAt: '2026-09-14T09:00:00Z',
        }}
        onOpen={vi.fn()}
        onSettings={vi.fn()}
        onDelete={vi.fn()}
      />,
    )
    expect(screen.getByText('12 Aug 2026')).toBeTruthy()
    expect(screen.getByText('14 Sep 2026')).toBeTruthy()
  })

  it('shows the same date twice for a project created and updated in one minute', () => {
    // Same value in both columns, and the row keeps its height: the failure this guards is a
    // column that collapses or wraps when the two strings are identical.
    const at = '2026-09-14T09:00:00Z'
    render(
      <ProjectRow
        project={{ ...project(), createdAt: at, updatedAt: at }}
        onOpen={vi.fn()}
        onSettings={vi.fn()}
        onDelete={vi.fn()}
      />,
    )
    const both = screen.getAllByText('14 Sep 2026')
    expect(both).toHaveLength(2)
    for (const cell of both) expect(cell.className).toMatch(/whitespace-nowrap/)
  })

  it('★ the status pill is never held narrower than the words inside it', () => {
    // WHAT THIS PREVENTS: the pill carried the column's fixed 104px itself, and `CHANGES
    // REQUESTED` needs about 121px at 10px bold uppercase — so seventeen pixels of red text sat
    // OUTSIDE its own pink background, running to within a few pixels of the `⋯`.
    //
    // jsdom has no layout, so the assertion is structural, and that is the stronger form anyway:
    // the fixed width belongs to the COLUMN and the pill sizes to its own text, which makes the
    // overflow unreachable rather than merely retuned. Paired with the label itself, because
    // "has no width class" passes just as well on a pill that rendered nothing.
    render(
      <ProjectRow
        project={project({ appStatus: 'rejected' })}
        onOpen={vi.fn()}
        onSettings={vi.fn()}
        onDelete={vi.fn()}
      />,
    )
    const pill = screen.getByText('Changes requested')
    expect(pill.className).toMatch(/rounded-full/)
    expect(pill.className).not.toMatch(/\bw-/)
    // …and the column that reserves the space is still there, or the row has simply lost its ruler.
    expect(pill.parentElement?.className).toMatch(/w-\[122px\]/)
  })
})
