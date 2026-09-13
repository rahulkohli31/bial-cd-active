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
    render(<ProjectRow project={project({ description: LONG })} onOpen={vi.fn()} onDelete={vi.fn()} />)

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
    render(<ProjectRow project={project({ description: LONG })} onOpen={onOpen} onDelete={vi.fn()} />)

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
    render(<ProjectRow project={project({ description: LONG })} onOpen={onOpen} onDelete={vi.fn()} />)

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
        onDelete={vi.fn()}
      />,
    )

    const name = screen.getByRole('button', { name: 'A Very Long Project Name That Has To Clip' })
    fireEvent.focus(name)

    expect(await screen.findByRole('tooltip')).toBeTruthy()
  })

  it('opens nothing on a name that already fits', () => {
    stubClip(false)
    render(<ProjectRow project={project()} onOpen={vi.fn()} onDelete={vi.fn()} />)

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
      <ProjectRow project={project({ name: 'Clipped today' })} onOpen={vi.fn()} onDelete={vi.fn()} />,
    )
    const before = screen.getByRole('button', { name: 'Clipped today' })

    stubClip(false)
    rerender(<ProjectRow project={project({ name: 'Fits now' })} onOpen={vi.fn()} onDelete={vi.fn()} />)
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
    render(<ProjectRow project={project({ description: null })} onOpen={onOpen} onDelete={vi.fn()} />)

    fireEvent.click(screen.getByText('No description yet'))

    expect(onOpen).toHaveBeenCalled()
  })
})

describe('ProjectRow — no nested interactive elements, still', () => {
  it('keeps Delete out of the name button, even with the tooltip wrapper added', () => {
    // The tooltip restructuring wraps the name in TooltipProvider/Tooltip/TooltipTrigger —
    // worth re-confirming the invariant survives the extra nesting.
    render(<ProjectRow project={project()} onOpen={vi.fn()} onDelete={vi.fn()} />)

    const del = screen.getByLabelText('Delete Visitor Log')
    const open = screen.getByRole('button', { name: 'Visitor Log' })
    expect(open.contains(del)).toBe(false)
    expect(del.contains(open)).toBe(false)
  })
})
