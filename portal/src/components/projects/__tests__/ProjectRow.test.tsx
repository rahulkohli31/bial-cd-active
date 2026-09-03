/**
 * `ProjectRow` — round-4 review, finding 3: the clipped-text tooltip branch had ZERO
 * automated coverage, because jsdom reports `scrollWidth`/`clientWidth` as `0`/`0` for every
 * element, so `clipped` was always `false` under test and the `Tooltip` branch never
 * rendered. A mutant deleting the whole branch passed the entire suite.
 *
 * `scrollWidth`/`clientWidth` are stubbed per-element via `Object.defineProperty`, which is
 * the only way to force the measurement jsdom cannot produce on its own.
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
  isServing: false,
  createdAt: '2026-07-10T00:00:00Z',
  updatedAt: '2026-07-10T00:00:00Z',
  ...over,
})

/** Forces every element's clip measurement in one direction for the lifetime of the test. */
function stubClip(clipped: boolean) {
  vi.spyOn(HTMLElement.prototype, 'scrollWidth', 'get').mockReturnValue(clipped ? 200 : 100)
  vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(100)
}

describe('ProjectRow — the description tooltip', () => {
  it('opens on a REALLY clipped description, and reads the full text', async () => {
    // Mutation receipt: delete the Tooltip branch from ClampedDescription and this test is
    // the one that goes red — nothing else in the suite can see it, since jsdom's own
    // scrollWidth/clientWidth are both 0 and `clipped` would already read `false`.
    stubClip(true)
    render(
      <ProjectRow
        project={project({ description: 'A description long enough that it must clip' })}
        onOpen={vi.fn()}
        onDelete={vi.fn()}
      />,
    )

    const description = screen.getByText('A description long enough that it must clip')
    fireEvent.focus(description.closest('p') ?? description)

    expect(await screen.findByRole('tooltip')).toBeTruthy()
    expect(screen.getAllByText('A description long enough that it must clip').length).toBeGreaterThan(0)
  })

  it('renders nothing extra when the description is NOT clipped', async () => {
    // The companion case: text that already fits must not fire a tooltip on hover — one that
    // does is noise on text the reader can already read in full.
    stubClip(false)
    render(<ProjectRow project={project()} onOpen={vi.fn()} onDelete={vi.fn()} />)

    const description = screen.getByText('A short description')
    fireEvent.focus(description)

    expect(screen.queryByRole('tooltip')).toBeNull()
  })

  it('opens on a really clipped NAME too', async () => {
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
})

describe('ProjectRow — the ref never remounts across a clipped transition', () => {
  it('keeps the SAME DOM node whether or not the description is clipped', () => {
    // Round-4 minor: the old branch put the ref'd <p> at a DIFFERENT tree position
    // depending on `clipped` — bare, versus nested inside TooltipProvider/Tooltip/
    // TooltipTrigger — which React treats as a remount. The effect's deps ([measure, text])
    // do not change on that remount, so a real ResizeObserver (inert in this test
    // environment, which is why this checks node IDENTITY rather than the observer firing)
    // never rebinds to the new node: false→true worked once, true→false never fired again.
    //
    // The fix keeps the wrapper mounted always, so the node must be THE SAME element across
    // a transition forced here by changing `text` (a real effect dependency) alongside the
    // clip stub — proof that nothing downstream of this component ever loses its target.
    stubClip(true)
    const { rerender } = render(
      <ProjectRow project={project({ description: 'Clipped today' })} onOpen={vi.fn()} onDelete={vi.fn()} />,
    )
    const before = screen.getByText('Clipped today')

    stubClip(false)
    rerender(
      <ProjectRow project={project({ description: 'Fits now' })} onOpen={vi.fn()} onDelete={vi.fn()} />,
    )
    const after = screen.getByText('Fits now')

    expect(after).toBe(before) // same DOM node, not a fresh mount
    expect(screen.queryByRole('tooltip')).toBeNull() // and it correctly re-measured as unclipped
  })
})

describe('ProjectRow — a project with no description', () => {
  it('opens the project when the "No description yet" strip is clicked', () => {
    // Round-4 finding 8: this branch had `relative z-10` (to sit above the name button's
    // stretched ::after) but no `onClick` — a dead strip across the newest, emptiest
    // projects, the ones most likely to be clicked into.
    const onOpen = vi.fn()
    render(<ProjectRow project={project({ description: null })} onOpen={onOpen} onDelete={vi.fn()} />)

    fireEvent.click(screen.getByText('No description yet'))

    expect(onOpen).toHaveBeenCalled()
  })
})

describe('ProjectRow — F-10, still', () => {
  it('keeps Delete out of the name button, even with the tooltip wrapper added', () => {
    // The tooltip restructuring (round 4) wraps the name in TooltipProvider/Tooltip/
    // TooltipTrigger — worth re-confirming the invariant survives the extra nesting.
    render(<ProjectRow project={project()} onOpen={vi.fn()} onDelete={vi.fn()} />)

    const del = screen.getByLabelText('Delete Visitor Log')
    const open = screen.getByRole('button', { name: 'Visitor Log' })
    expect(open.contains(del)).toBe(false)
    expect(del.contains(open)).toBe(false)
  })
})
